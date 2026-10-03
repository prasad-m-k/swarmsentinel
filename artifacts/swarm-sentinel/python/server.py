import asyncio
import json
import threading
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from asp_policy import ASPPolicy
from live import CallInput, SessionInput, SessionStore
from models import RunInput
from pipeline import replay, summarize
from reporter import markdown_report
from village import store
from village.normalize import PERMA_COMPUTER_USE, pacific_day

app = FastAPI(title="SwarmSentinel Python Engine", docs_url="/api/swarm/docs",
              openapi_url="/api/swarm/openapi.json", redoc_url=None)

SYNTHETIC = "Synthetic sample traces. Not AI Village data or a real incident replay."
VILLAGE = "AI Village dataset replay (historical records, report-only). Decisions are counterfactual."
LIVE = "Live session: each call was decided before the calling agent executed it."
SESSIONS = SessionStore()


def _village_db():
    db = store.connect()
    if db is None:
        raise HTTPException(503, "AI Village store not built. Run `python -m village.ingest` and `python -m village.scan` locally.")
    return db


@app.get("/api/swarm/health")
def health():
    return {"status": "ok", "provenance": "synthetic", "aiVillage": store.DB_PATH.exists()}


@app.get("/api/swarm/sources")
def sources():
    policies = {name: ASPPolicy.load(name).declaration() for name in ("mock", "ai-village")}
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
def simulate(settings: RunInput):
    village = settings.scenario == "ai-village"
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


def _run(run_id, scenario, source, provenance, gateway, sentinel, policy_name, policy, settings, started_at, window=None):
    run = dict(id=run_id, scenario=scenario, source=source, provenance=provenance, mode=gateway.mode,
               rootAgent=gateway.root, startedAt=started_at, window=window,
               policyName=policy_name, policy=policy.declaration(),
               events=[t.model_dump() for t in gateway.telemetry],
               alerts=sentinel.alerts, policies=sentinel.policy_changes, edges=sentinel.weighted_edges(),
               summary=summarize(gateway.telemetry, sentinel.alerts))
    run["report"] = markdown_report(run, settings)
    return run


def _session(session_id):
    session = SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(404, f"Unknown or expired session {session_id}")
    return session


@app.get("/api/swarm/sessions")
def list_sessions():
    """Live sessions, newest first, so the dashboard can attach to one."""
    rows = []
    with SESSIONS.lock:
        sessions = list(SESSIONS.sessions.values())
    for s in sessions:
        with s.lock:
            rows.append(dict(sessionId=s.id, created=s.created, policyName=s.spec.policy, root=s.spec.root,
                             label=s.label, events=len(s.gateway.telemetry), alerts=len(s.sentinel.alerts)))
    return sorted(rows, key=lambda r: r["created"], reverse=True)


@app.post("/api/swarm/sessions", status_code=201)
def create_session(spec: SessionInput):
    session = SESSIONS.create(spec)
    return {"sessionId": session.id, "policyName": spec.policy, "root": spec.root,
            "feedbackEnabled": spec.feedbackEnabled, "policy": session.policy.declaration()}


@app.post("/api/swarm/sessions/{session_id}/evaluate")
def evaluate(session_id: str, call: CallInput):
    """Decide one proposed action before the agent runs it. allowed=false means: do not execute."""
    return _session(session_id).evaluate(call)


@app.get("/api/swarm/sessions/{session_id}")
def session_run(session_id: str):
    s = _session(session_id)
    with s.lock:
        started = s.gateway.telemetry[0].timestamp if s.gateway.telemetry else s.created
        return _run(s.id, "live", "live", LIVE, s.gateway, s.sentinel, s.spec.policy, s.policy, s.settings, started)


@app.get("/api/swarm/sessions/{session_id}/stream")
async def stream_session(session_id: str, after: int = 0):
    """Server-sent events: each message carries traces after `after`, plus current alerts and revocations."""
    _session(session_id)

    async def events():
        sent, idle = max(0, after), 0
        while (s := SESSIONS.get(session_id)) is not None:
            with s.lock:
                fresh = [t.model_dump() for t in s.gateway.telemetry[sent:]]
                alerts, policies = list(s.sentinel.alerts), list(s.sentinel.policy_changes)
            if fresh:
                sent += len(fresh)
                idle = 0
                yield f"data: {json.dumps({'events': fresh, 'alerts': alerts, 'policies': policies, 'total': sent})}\n\n"
            else:
                idle += 1
                if idle % 60 == 0:
                    yield ": keep-alive\n\n"
            await asyncio.sleep(0.25)
        yield "event: closed\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


class DemoInput(BaseModel):
    feedbackEnabled: bool = True
    pause: float = Field(default=1.2, ge=0, le=5)


@app.post("/api/swarm/demo/injection", status_code=202)
def launch_injection_demo(spec: DemoInput):
    """Start the poisoned-invoice attack against a fresh live session, paced for watching."""
    from demo import run_injection
    from sdk.guard import Guard, _Local
    session = SESSIONS.create(SessionInput(feedbackEnabled=spec.feedbackEnabled))
    session.label = "Prompt-injection demo"
    guard = Guard(_Local(SESSIONS), session_id=session.id)
    threading.Thread(target=run_injection, args=(guard, spec.pause), daemon=True).start()
    return {"sessionId": session.id}


@app.delete("/api/swarm/sessions/{session_id}", status_code=204)
def delete_session(session_id: str):
    if not SESSIONS.delete(session_id):
        raise HTTPException(404, f"Unknown or expired session {session_id}")
    return Response(status_code=204)
