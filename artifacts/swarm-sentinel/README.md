# SwarmSentinel

Private live-session authentication and operator setup:
[live-session access](../../docs/live-session-access.md). Public synthetic demos
are separate from owner-only sessions; historical AI Village API access is
disabled unless explicitly approved and configured.

Cooperative external-tool receipts and their evidence boundary:
[completion receipts](../../docs/cooperative-completion.md).

AI Swarm Dynamics Hackathon prototype: an ASP policy gateway and multi-agent flight recorder, run against synthetic scenarios (enforce mode) and locally stored AI Village records (report-only mode).

## Run

Use the managed `artifacts/swarm-sentinel: web` and `artifacts/swarm-sentinel: engine` workflows.

- Frontend: `pnpm --filter @workspace/swarm-sentinel run dev`. Outside Replit, set `SWARM_ENGINE_URL=http://127.0.0.1:8000` to proxy `/api/swarm` to the engine.
- Engine: `pnpm --filter @workspace/swarm-sentinel run engine`.
- Both require their workflow-supplied `PORT`; the frontend also requires `BASE_PATH`.
- Python dependencies are defined in the root `pyproject.toml` and `uv.lock`.
- API documentation is at `/api/swarm/docs`.
- Tests: `cd python && python -m unittest discover tests`.

## Demo

Opening the preview loads the complete normal sample. Select **Swarm Attack** and **Run Demo** to replay the attack sample. If the engine has a local AI Village store, **AI Village** becomes available: pick an indexed episode and **Replay**. Inspect traces, tripwires and the timeline, then download the Markdown report and JSON telemetry once replay completes.

## Modules

- `python/asp_policy.py`: loads and validates ASP JSON declarations (`policies/*.asp.json`).
- `python/asp_gateway.py`: Algorithm 1 interception: lineage admission, deny list, default-deny allowlist, argument constraints, network scope, content-trust contamination, write cap, repeated-intent throttling, revocation. Enforce or report-only.
- `python/sentinel.py`: NetworkX graph with policy-configured windows; reciprocal-consensus, shared-state, fan-out and echo-cascade tripwires.
- `python/pipeline.py`: gateway plus sentinel replay loop and run summary, shared by the API and the scanner.
- `python/reporter.py`: provenance-aware Markdown forensic reports.
- `python/server.py`: FastAPI endpoints (`/simulate`, `/sources`, `/health`).
- `python/simulator.py` and `python/samples/`: deterministic synthetic scenarios.
- `python/village/`: AI Village adapter. `normalize.py` maps dataset rows to events, `ingest.py` builds the local store, `scan.py` indexes candidate episodes, `store.py` reads it.

## Boundaries

Synthetic tools are mocks. AI Village replays are historical: decisions are counterfactual and nothing is blocked. The gateway is a code boundary in one process, not a tamper-proof one. Intent vectors are hash fingerprints; echo detection uses token overlap, not embeddings. Heuristic alerts do not prove compromise. See `docs/ai-village-access.md` at the project root for dataset terms and handling rules.
