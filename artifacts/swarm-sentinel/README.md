# SwarmSentinel

AI Swarm Dynamics Hackathon — synthetic-sample prototype.

## Run

Use the managed `artifacts/swarm-sentinel: web` and `artifacts/swarm-sentinel: engine` workflows.

- Frontend: `pnpm --filter @workspace/swarm-sentinel run dev`.
- Engine: `pnpm --filter @workspace/swarm-sentinel run engine`.
- Both require their workflow-supplied `PORT`; frontend also requires `BASE_PATH`.
- Python dependencies are defined in the root `pyproject.toml` and `uv.lock`.
- API documentation is at `/api/swarm/docs`.

## Demo

Opening the preview loads the complete normal sample. Select **Swarm Attack** and **Run Demo** to replay the attack sample. Inspect individual traces, tripwire policy changes, and the timeline. After replay completes, download the Markdown report and JSON telemetry. The policy tab can change hop depth, repeated-intent and MCP write limits; feedback can be disabled for comparison while baseline ASP rules remain active.

## Modules

- `python/simulator.py`: deterministic sample generation.
- `python/asp_gateway.py`: lineage admission, hop cap, identical-intent throttling, tool allowlist and write cap, dynamic agent revocation.
- `python/sentinel.py`: NetworkX graph, ten-second coordination windows, shared wiki edits, reciprocal density and fan-out detection.
- `python/reporter.py`: provenance-aware Markdown forensic reports.
- `python/server.py`: validated FastAPI simulation endpoint.
- `python/samples/`: small preloaded synthetic JSON samples.

## Boundaries

The mock gateway does not execute external tools. Out-of-band events are observations; feedback prevents only subsequent in-band mock calls. The gateway is a deterministic code boundary, not a tamper-proof operating-system boundary. Intent vectors are hash fingerprints, not semantic embeddings; throttling recognizes identical normalized intents. Heuristic alerts do not prove compromise or maliciousness.

No real AI Village data is loaded. See `docs/ai-village-access.md` at the project root for access and research-use constraints.