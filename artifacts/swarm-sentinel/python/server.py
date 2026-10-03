import json
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI, HTTPException
from asp_policy import ASPPolicy
from models import RunInput
from pipeline import replay, summarize
from reporter import markdown_report
from village import store
from village.normalize import PERMA_COMPUTER_USE, pacific_day

app = FastAPI(title="SwarmSentinel Python Engine", docs_url="/api/swarm/docs",
              openapi_url="/api/swarm/openapi.json", redoc_url=None)

SYNTHETIC = "Synthetic sample traces. Not AI Village data or a real incident replay."
VILLAGE = "AI Village dataset replay (historical records, report-only). Decisions are counterfactual."


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
    run = dict(id=str(uuid4()), scenario=settings.scenario, source="ai-village" if village else "synthetic",
               provenance=VILLAGE if village else SYNTHETIC, mode=gateway.mode,
               rootAgent=gateway.root, startedAt=events[0]["timestamp"], window=window,
               policyName=policy_name, policy=policy.declaration(),
               events=[t.model_dump() for t in gateway.telemetry],
               alerts=sentinel.alerts, policies=sentinel.policy_changes, edges=sentinel.weighted_edges(),
               summary=summarize(gateway.telemetry, sentinel.alerts))
    run["report"] = markdown_report(run, settings)
    return run
