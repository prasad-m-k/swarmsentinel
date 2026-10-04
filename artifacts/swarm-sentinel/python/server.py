import asyncio
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
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
from pydantic import ValidationError

app = FastAPI(title="SwarmSentinel Python Engine", docs_url="/api/swarm/docs",
              openapi_url="/api/swarm/openapi.json", redoc_url=None)

SYNTHETIC = "Synthetic sample traces. Not AI Village data or a real incident replay."
VILLAGE = "AI Village dataset replay (historical records, report-only). Decisions are counterfactual."
LIVE = "Owner-private Live: authorization, engine-observed outcomes, and caller-reported completion are recorded separately."
SESSIONS = SessionStore()
DEMOS = SessionStore(limit=20)


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
                  regime="perma-computer-use" if start[:10] >= PERMA_COMPUTER_USE else "discrete-sessions")
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


def _session(session_id, principal):
    session = SESSIONS.get(session_id)
    if session is None or session.owner != principal.subject:
        raise HTTPException(404, "Unknown or expired session")
    return session


@app.get("/api/swarm/sessions")
def list_sessions(principal: Principal = Depends(require_principal)):
    """Live sessions, newest first, so the dashboard can attach to one."""
    with SESSIONS.lock:
        sessions = [s for s in SESSIONS.sessions.values() if s.owner == principal.subject]
    return _summaries(sessions)


def _summaries(sessions):
    rows = []
    for s in sessions:
        with s.lock:
            rows.append(dict(sessionId=s.id, created=s.created, policyName=s.spec.policy, root=s.spec.root,
                             label=s.label, events=len(s.gateway.telemetry), alerts=len(s.sentinel.alerts)))
    return sorted(rows, key=lambda r: r["created"], reverse=True)


@app.post("/api/swarm/sessions", status_code=201)
def create_session(spec: SessionInput, principal: Principal = Depends(require_principal)):
    try:
        session = SESSIONS.create(spec, owner=principal.subject)
    except OverflowError:
        raise HTTPException(429, "Session capacity reached; delete an owned session before creating another")
    return {"sessionId": session.id, "policyName": spec.policy, "root": spec.root,
            "feedbackEnabled": spec.feedbackEnabled, "policy": session.policy.declaration(),
            "completionGraceSeconds": spec.completionGraceSeconds}


@app.post("/api/swarm/sessions/{session_id}/evaluate")
def evaluate(session_id: str, call: CallInput, principal: Principal = Depends(require_principal)):
    """Decide one proposed action before the agent runs it. allowed=false means: do not execute."""
    return _session(session_id, principal).evaluate(call)


@app.post("/api/swarm/sessions/{session_id}/sandbox", status_code=201)
def configure_sandbox(session_id: str, spec: SandboxInput, principal: Principal = Depends(require_principal)):
    """Create fixed fictional fixtures only; never accept a directory or launch inference."""
    try:
        return _session(session_id, principal).configure_sandbox(spec.mode)
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
        session = _session(session_id, principal)
        with session.lock:
            if not still_valid(principal):
                raise HTTPException(401, "Authentication expired")
            return session.execute(spec)
    except ValidationError:
        raise HTTPException(422, "Invalid registered tool arguments")
    except ValueError as e:
        raise HTTPException(409, str(e))


@app.post("/api/swarm/sessions/{session_id}/completion")
def complete_tool(session_id: str, receipt: CompletionReceipt, principal: Principal = Depends(require_principal)):
    session = _session(session_id, principal)
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
            if principal and (not still_valid(principal) or s.owner != principal.subject):
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
    _session(session_id, principal)
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
