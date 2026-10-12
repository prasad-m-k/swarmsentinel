import asyncio
import os
import hmac
import json
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal, Optional
from uuid import uuid4
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import BaseModel, Field
from asp_policy import ASPPolicy
from live import CallInput, SessionInput, SessionStore
from models import RunInput, CompletionReceipt
from pipeline import replay, summarize
from reporter import markdown_report
from village import store
from village.normalize import PERMA_COMPUTER_USE, pacific_day
from auth import Principal, require_principal, still_valid, village_access
from sandbox import ExecutionInput, SandboxInput
from mcp_proxy import front as mcp_front
from mcp_proxy import broker as mcp_broker
import persistence
from starlette.routing import Route
from pydantic import ValidationError



@asynccontextmanager
async def lifespan(app):
    async with MCP_ENDPOINT.run():
        try:
            yield
        finally:
            await asyncio.gather(*(s.mcp.pool.aclose() for s in list(SESSIONS.sessions.values()) if s.mcp),
                                 return_exceptions=True)


app = FastAPI(title="SwarmSentinel Python Engine", docs_url="/api/swarm/docs",
              openapi_url="/api/swarm/openapi.json", redoc_url=None, lifespan=lifespan)

SYNTHETIC = "Synthetic sample traces. Not AI Village data or a real incident replay."
VILLAGE = "AI Village dataset replay (historical records, report-only). Decisions are counterfactual."
LIVE = "Owner-private Live: authorization, engine-observed outcomes, and caller-reported completion are recorded separately."
# SWARM_STATE_DB makes private sessions durable (persistence.py); unset keeps them in memory only.
SESSIONS = SessionStore(state=persistence.from_env())
DEMOS = SessionStore(limit=20)
# MCP proxy mode (docs/mcp-proxy.md): agents authenticate with engine-issued credentials, not Clerk.
MCP_ENDPOINT = mcp_front.build(lambda session_id: SESSIONS.get(session_id))
app.router.routes.append(Route("/api/swarm/mcp", endpoint=MCP_ENDPOINT))


@app.exception_handler(RequestValidationError)
async def invalid_request(request, error):
    # Default validation responses echo rejected inputs, including capability
    # tokens and unexpected caller error/result text. Never echo receipt bodies.
    if request.url.path.endswith("/completion"):
        return JSONResponse(status_code=422, content={"detail": "Invalid completion receipt"})
    return await request_validation_exception_handler(request, error)


@app.middleware("http")
async def private_response_headers(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/swarm"):
        response.headers["Cache-Control"] = "no-store"
        response.headers["Vary"] = "Cookie, Authorization"
        response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _village_db():
    db = store.connect()
    if db is None:
        raise HTTPException(503, "AI Village store not built. Run `python -m village.ingest` and `python -m village.scan` locally.")
    return db


@app.get("/api/swarm/health")
def health():
    return {"status": "ok", "provenance": "synthetic"}


@app.get("/api/swarm/sources")
def sources(request: Request):
    policies = {name: ASPPolicy.load(name).declaration() for name in ("mock", "ai-village")}
    # Public discovery must not leak even metadata from a private dataset.
    try:
        approved = village_access(require_principal(request))
    except HTTPException:
        approved = False
    if not approved:
        return {"aiVillage": {"available": False, "episodes": []}, "policies": policies}
    db = store.connect()
    if db is None:
        return {"aiVillage": {"available": False, "episodes": []}, "policies": policies}
    info = store.meta(db)
    return {"aiVillage": {"available": True, "dataset": info.get("dataset"), "exportedAt": info.get("exportedAt"),
                          "citation": info.get("citation"), "range": info.get("range"), "events": info.get("events"),
                          "episodes": store.episodes(db)},
            "policies": policies}


def _village_events(settings):
    db = _village_db()
    if settings.episodeId:
        episode = store.episode(db, settings.episodeId)
        if not episode:
            raise HTTPException(404, f"Unknown episode {settings.episodeId}")
        start, end = episode["start"], episode["end"]
    else:
        start, end = (datetime.fromisoformat(v.replace("Z", "+00:00")).isoformat() for v in (settings.start, settings.end))
        if datetime.fromisoformat(end) - datetime.fromisoformat(start) > timedelta(days=1):
            raise HTTPException(422, "Replay windows are limited to 24 hours")
    events, truncated = store.window(db, start, end, settings.maxEvents)
    if not events:
        raise HTTPException(404, "No AI Village events in that window")
    info = store.meta(db)
    window = dict(dataset=info["dataset"], exportedAt=info["exportedAt"], citation=info["citation"],
                  start=start, end=end, truncated=truncated, episodeId=settings.episodeId,
                  day=pacific_day(start.replace("T", " ")[:26].split("+")[0]),
                  regime="perma-computer-use" if start[:10] >= PERMA_COMPUTER_USE else "discrete-sessions",
                  textWithheld=bool(info.get("snapshot", {}).get("textWithheld")))
    return events, store.registry(db, end), window


@app.post("/api/swarm/simulate")
def simulate(settings: RunInput, request: Request = None):
    village = settings.scenario == "ai-village"
    if village and request is None:
        raise HTTPException(401, "Authentication required")
    if village and not village_access(require_principal(request)):
        raise HTTPException(403, "Private AI Village access is not approved")
    policy_name = "ai-village" if village else "mock"
    policy = ASPPolicy.load(policy_name).with_overrides(settings.maxDepth, settings.semanticLimit, settings.writeLimit)
    if village:
        events, registry, window = _village_events(settings)
        gateway, sentinel = replay(events, settings, policy, mode="report-only", registry=registry,
                                   root="village", root_is_actor=False)
    else:
        samples = Path(__file__).parent / "samples" / f"{settings.scenario}.json"
        events, window = json.loads(samples.read_text()), None
        gateway, sentinel = replay(events, settings, policy)
    return _run(str(uuid4()), settings.scenario, "ai-village" if village else "synthetic", VILLAGE if village else SYNTHETIC,
                gateway, sentinel, policy_name, policy, settings, events[0]["timestamp"], window)


def _run(run_id, scenario, source, provenance, gateway, sentinel, policy_name, policy, settings, started_at, window=None,
         execution_evidence=False, completion_warnings=None, completion_grace=None):
    run = dict(id=run_id, scenario=scenario, source=source, provenance=provenance, mode=gateway.mode,
               rootAgent=gateway.root, startedAt=started_at, window=window,
               policyName=policy_name, policy=policy.declaration(),
               events=[t.recorder_dump() for t in gateway.telemetry],
               alerts=sentinel.alerts, policies=sentinel.policy_changes, edges=sentinel.weighted_edges(),
               summary=summarize(gateway.telemetry, sentinel.alerts))
    if execution_evidence:
        run["executionEvidence"] = True
        run["completionWarnings"] = completion_warnings or []
        run["completionGraceSeconds"] = completion_grace
    run["report"] = markdown_report(run, settings)
    return run


def _session(session_id, principal, permission="session.read"):
    """Foreign and missing sessions both answer 404; a known session the role may not use answers 403."""
    session = SESSIONS.get(session_id)
    if session is None or session.tenant != principal.tenant_id:
        raise HTTPException(404, "Unknown or expired session")
    if permission == "session.delete" and session.owner != principal.subject:
        permission = "session.delete.any"
    elif permission == "session.delete":
        permission = "session.delete.own"
    if not principal.can(permission):
        raise HTTPException(403, f"Role '{principal.role}' does not permit this action")
    return session


def _require(principal, permission):
    if not principal.can(permission):
        raise HTTPException(403, f"Role '{principal.role}' does not permit this action")


@app.get("/api/swarm/me")
def whoami(principal: Principal = Depends(require_principal)):
    """The caller's tenant, role and permissions, as derived from verified claims."""
    return {"subject": principal.subject, "tenant": principal.tenant_id, "role": principal.role,
            "permissions": sorted(principal.permissions)}


@app.get("/api/swarm/sessions")
def list_sessions(principal: Principal = Depends(require_principal)):
    """Live sessions, newest first, so the dashboard can attach to one."""
    return SESSIONS.summaries(principal.tenant_id)


def _summaries(sessions):
    rows = []
    for s in sessions:
        with s.lock:
            rows.append(dict(sessionId=s.id, created=s.created, policyName=s.spec.policy, root=s.spec.root,
                             label=s.label, events=len(s.gateway.telemetry), alerts=len(s.sentinel.alerts)))
    return sorted(rows, key=lambda r: r["created"], reverse=True)


@app.post("/api/swarm/sessions", status_code=201)
def create_session(spec: SessionInput, principal: Principal = Depends(require_principal)):
    _require(principal, "session.create")
    try:
        session = SESSIONS.create(spec, owner=principal.subject, tenant=principal.tenant_id)
    except OverflowError:
        raise HTTPException(429, "Session capacity reached; delete an owned session before creating another")
    return {"sessionId": session.id, "policyName": spec.policy, "root": spec.root,
            "feedbackEnabled": spec.feedbackEnabled, "policy": session.policy.declaration(),
            "completionGraceSeconds": spec.completionGraceSeconds}


@app.post("/api/swarm/sessions/{session_id}/evaluate")
def evaluate(session_id: str, call: CallInput, principal: Principal = Depends(require_principal)):
    """Decide one proposed action before the agent runs it. allowed=false means: do not execute."""
    return _session(session_id, principal, "session.act").evaluate(call)


@app.post("/api/swarm/sessions/{session_id}/sandbox", status_code=201)
def configure_sandbox(session_id: str, spec: SandboxInput, principal: Principal = Depends(require_principal)):
    """Create fixed fictional fixtures only; never accept a directory or launch inference."""
    try:
        return _session(session_id, principal, "session.act").configure_sandbox(spec.mode)
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.get("/api/swarm/sessions/{session_id}/sandbox")
def sandbox_snapshot(session_id: str, principal: Principal = Depends(require_principal)):
    try:
        return _session(session_id, principal).sandbox_snapshot()
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/swarm/sessions/{session_id}/execute")
def execute_tool(session_id: str, spec: ExecutionInput, principal: Principal = Depends(require_principal)):
    """Authenticate even replays; exact keyed retries return the original outcome."""
    try:
        session = _session(session_id, principal, "session.act")
        with session.lock:
            if not still_valid(principal):
                raise HTTPException(401, "Authentication expired")
            return session.execute(spec)
    except ValidationError:
        raise HTTPException(422, "Invalid registered tool arguments")
    except ValueError as e:
        raise HTTPException(409, str(e))


class McpAgentInput(BaseModel):
    model_config = {"extra": "forbid"}
    agentId: str = Field(min_length=1, max_length=80, pattern=r"^[A-Za-z0-9._-]+$")
    parentId: str = Field(default="", max_length=120)
    scope: list[str] = []


@app.post("/api/swarm/sessions/{session_id}/mcp/agents", status_code=201)
def mint_mcp_agent(session_id: str, spec: McpAgentInput, principal: Principal = Depends(require_principal)):
    """Spawn an agent through the gateway and return its MCP credential once. It is never shown again."""
    session = _session(session_id, principal, "session.act")
    if session.mcp is None:
        raise HTTPException(409, "This session's policy declares no MCP servers")
    with session.lock:
        if not still_valid(principal):
            raise HTTPException(401, "Authentication expired")
        try:
            decision, credential = session.mcp.mint(spec.agentId, spec.parentId or session.spec.root, spec.scope)
        except LookupError as e:
            raise HTTPException(404, str(e))
        except ValueError as e:
            raise HTTPException(409, str(e))
    if credential is None:
        raise HTTPException(403, f"Spawn refused by the ASP gateway ({decision['rule']}): {decision['reason']}")
    return {"agentId": spec.agentId, "parentId": spec.parentId or session.spec.root,
            "credential": credential, "endpoint": "/api/swarm/mcp", "decision": decision}


@app.post("/api/swarm/sessions/{session_id}/mcp/agents/{agent_id}/task", status_code=201)
def rotate_mcp_task(session_id: str, agent_id: str, principal: Principal = Depends(require_principal)):
    """Start a new task for an MCP agent: a new credential, the old one retired. Under taint-scope "task"
    the new task starts clean; this is the controller's declassification step, never the agent's."""
    session = _session(session_id, principal, "session.act")
    if session.mcp is None:
        raise HTTPException(409, "This session's policy declares no MCP servers")
    if not still_valid(principal):
        raise HTTPException(401, "Authentication expired")
    try:
        task, credential = session.mcp.rotate_task(agent_id)
    except LookupError as e:
        raise HTTPException(404, str(e))
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
    return {"agentId": agent_id, "taskId": task, "credential": credential, "endpoint": "/api/swarm/mcp"}


# Egress monitor link (docs/egress-monitor.md). A monitor on the model-API path resolves an agent's
# engine-issued MCP credential to its identity, task and confidentiality labels, and reports its decisions
# back to the recorder. Disabled unless SWARM_EGRESS_SECRET is set; loopback callers only unless
# SWARM_EGRESS_REMOTE=true (for a monitor in another container on a private network).
class EgressResolveInput(BaseModel):
    model_config = {"extra": "forbid"}
    credential: str = Field(min_length=1, max_length=200)


class EgressDecisionInput(BaseModel):
    model_config = {"extra": "forbid"}
    sessionId: str = Field(min_length=1, max_length=120)
    agentId: str = Field(min_length=1, max_length=120)
    taskId: str = Field(default="", max_length=200)
    verdict: Literal["allow", "translate", "hold", "block"]
    applied: Literal["allow", "translate", "hold", "block"]
    mode: Literal["report", "enforce"]
    destination: str = Field(min_length=1, max_length=255)
    category: str = Field(default="", max_length=80)
    classes: list[str] = Field(default=[], max_length=40)
    rules: list[str] = Field(default=[], max_length=80)
    model: str = Field(default="", max_length=120)
    timestamp: Optional[str] = None


def _egress_auth(request: Request):
    secret = os.environ.get("SWARM_EGRESS_SECRET", "")
    if not secret:
        raise HTTPException(404, "Not found")
    host = request.client.host if request.client else ""
    if os.environ.get("SWARM_EGRESS_REMOTE") != "true" and host not in ("127.0.0.1", "::1", "localhost", "testclient"):
        raise HTTPException(403, "Egress monitor endpoints accept loopback callers only")
    if not hmac.compare_digest(request.headers.get("x-egress-secret", "").encode(), secret.encode()):
        raise HTTPException(401, "Egress monitor authentication failed")


@app.post("/api/swarm/egress/resolve")
def egress_resolve(body: EgressResolveInput, request: Request):
    """Identity, task, confidentiality labels, taint and revocation for an engine-issued MCP credential."""
    _egress_auth(request)
    bound = mcp_broker.lookup(body.credential)
    session = SESSIONS.get(bound[0]) if bound else None
    if session is None or session.mcp is None or session.closed or bound[1] not in session.mcp.agents:
        raise HTTPException(404, "Unknown or expired agent credential")
    return session.egress_identity(bound[1])


@app.post("/api/swarm/egress/events", status_code=201)
def egress_event(body: EgressDecisionInput, request: Request):
    """Record an egress monitor's content-free decision in the agent's session recorder."""
    _egress_auth(request)
    session = SESSIONS.get(body.sessionId)
    if session is None or session.mcp is None or session.closed or body.agentId not in session.mcp.agents:
        raise HTTPException(404, "Unknown session or agent")
    return {"eventId": session.record_egress(**body.model_dump(exclude={"sessionId"}))}


@app.post("/api/swarm/sessions/{session_id}/completion")
def complete_tool(session_id: str, receipt: CompletionReceipt, principal: Principal = Depends(require_principal)):
    session = _session(session_id, principal, "session.act")
    with session.lock:
        if not still_valid(principal):
            raise HTTPException(401, "Authentication expired")
        try:
            return session.complete(receipt)
        except LookupError:
            raise HTTPException(404, "Unknown evaluated event")
        except ValueError:
            raise HTTPException(409, "Completion receipt is ineligible, mismatched, or already recorded")


@app.get("/api/swarm/sessions/{session_id}")
def session_run(session_id: str, principal: Principal = Depends(require_principal)):
    return _snapshot(_session(session_id, principal))


def _snapshot(s, demo=False):
    with s.lock:
        started = s.gateway.telemetry[0].timestamp if s.gateway.telemetry else s.created
        return _run(s.id, "live", "live", SYNTHETIC if demo else LIVE,
                    s.gateway, s.sentinel, s.spec.policy, s.policy, s.settings, started,
                    execution_evidence=not demo,
                    completion_warnings=s.completion_warnings() if not demo else None,
                    completion_grace=s.spec.completionGraceSeconds if not demo else None)


@app.get("/api/swarm/sessions/{session_id}/stream")
async def stream_session(session_id: str, after: int = 0, principal: Principal = Depends(require_principal)):
    """Server-sent events: each message carries traces after `after`, plus current alerts and revocations."""
    _session(session_id, principal)
    return _stream(SESSIONS, session_id, after, principal)


def _stream(session_store, session_id, after, principal=None):

    async def events():
        sent, idle = max(0, after), 0
        evidence_seen = {}
        warnings_seen = None
        while (s := session_store.get(session_id)) is not None:
            if principal and (not still_valid(principal) or s.tenant != principal.tenant_id):
                yield "event: auth-expired\ndata: {}\n\n"
                return
            with s.lock:
                fresh = [t.recorder_dump() for t in s.gateway.telemetry[sent:]]
                updates = []
                if principal:
                    # Re-send existing evidence on first connection to close the
                    # snapshot/subscribe race; thereafter send only changed rows.
                    for index, trace in enumerate(s.gateway.telemetry):
                        signature = (trace.executionStatus, trace.toolBodyExecuted,
                                     trace.executionError, trace.executionProvenance)
                        if index < sent and signature != evidence_seen.get(trace.id, (None,) * 4):
                            updates.append(trace.recorder_dump())
                        evidence_seen[trace.id] = signature
                alerts, policies = list(s.sentinel.alerts), list(s.sentinel.policy_changes)
                warnings = s.completion_warnings() if principal else None
            warnings_changed = principal is not None and warnings != warnings_seen
            if fresh or updates or warnings_changed:
                sent += len(fresh)
                idle = 0
                payload = {'events': fresh, 'alerts': alerts, 'policies': policies, 'total': sent}
                if principal:
                    payload["updates"] = updates
                    # Full replacement, including [] after late receipts. Resend
                    # on every subscription, even with no new recorder rows.
                    payload["completionWarnings"] = warnings
                    warnings_seen = warnings
                yield f"data: {json.dumps(payload)}\n\n"
            else:
                idle += 1
                if idle % 60 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)
        yield "event: closed\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


class DemoInput(BaseModel):
    model_config = {"extra": "forbid"}
    feedbackEnabled: bool = True
    pause: float = Field(default=1.2, ge=0, le=5)


@app.post("/api/swarm/demo/injection", status_code=202)
def launch_injection_demo(spec: DemoInput):
    """Start the poisoned-invoice attack against a fresh live session, paced for watching."""
    from demo import run_injection
    from sdk.guard import Guard, _Local
    session = DEMOS.create(SessionInput(feedbackEnabled=spec.feedbackEnabled))
    session.label = "PUBLIC SYNTHETIC prompt-injection demo"
    guard = Guard(_Local(DEMOS), session_id=session.id)
    threading.Thread(target=run_injection, args=(guard, spec.pause), daemon=True).start()
    return {"sessionId": session.id}


@app.delete("/api/swarm/sessions/{session_id}", status_code=204)
def delete_session(session_id: str, principal: Principal = Depends(require_principal)):
    _session(session_id, principal, "session.delete")
    if not SESSIONS.delete(session_id):
        raise HTTPException(404, f"Unknown or expired session {session_id}")
    return Response(status_code=204)


@app.get("/api/swarm/demo/sessions")
def list_demo_sessions():
    with DEMOS.lock:
        sessions = list(DEMOS.sessions.values())
    return _summaries(sessions)


def _demo_session(session_id):
    session = DEMOS.get(session_id)
    if session is None:
        raise HTTPException(404, "Unknown or expired demonstration")
    return session


@app.get("/api/swarm/demo/sessions/{session_id}")
def demo_snapshot(session_id: str):
    return _snapshot(_demo_session(session_id), demo=True)


@app.get("/api/swarm/demo/sessions/{session_id}/stream")
async def demo_stream(session_id: str, after: int = 0):
    _demo_session(session_id)
    return _stream(DEMOS, session_id, after)
