import json
from pathlib import Path
from uuid import uuid4
from fastapi import FastAPI
from models import Event, RunInput
from asp_gateway import ASPGateway
from sentinel import Sentinel
from reporter import markdown_report

app = FastAPI(title="SwarmSentinel Python Engine", docs_url="/api/swarm/docs",
              openapi_url="/api/swarm/openapi.json", redoc_url=None)


@app.get("/api/swarm/health")
def health():
    return {"status": "ok", "provenance": "synthetic"}


@app.post("/api/swarm/simulate")
def simulate(settings: RunInput):
    samples = Path(__file__).parent / "samples" / f"{settings.scenario}.json"
    events = [Event.model_validate(e) for e in json.loads(samples.read_text())]
    gateway, sentinel = ASPGateway(settings), Sentinel()
    for event in events:
        trace = gateway.evaluate_and_log(event)
        sentinel.ingest(trace, gateway)
    run = dict(id=str(uuid4()), scenario=settings.scenario,
               provenance="Synthetic sample traces. Not AI Village data or a real incident replay.",
               rootAgent="orchestrator", startedAt=events[0].timestamp,
               events=[t.model_dump() for t in gateway.telemetry],
               alerts=sentinel.alerts, policies=sentinel.policy_changes)
    run["report"] = markdown_report(run, settings)
    return run