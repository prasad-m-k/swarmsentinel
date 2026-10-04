# SwarmSentinel

**Description:** AI Swarm Dynamics Hackathon.

**Status:** ASP-inspired policy gateway and multi-agent flight recorder, with synthetic enforcement, optional AI Village historical replay, cooperative live interception, and a real LLM-backed agent showcase.

**ASP paper permission:** The project owner authored the ASP paper and has explicitly permitted its use freely in this project. This does not change the separate access and research-use restrictions on AI Village data.

## Source of truth and startup order

- Review `CLAUDE.md`, current source, and upstream commit history when syncing offline changes. This file translates those instructions into Replit operations; the macOS `.venv` and `npx pnpm` commands in `CLAUDE.md` are not Replit setup instructions.
- Import the latest source and update `replit.md` **before** starting the project, as requested by the user. Stop existing services while applying a source sync.
- Preserve Replit's managed artifact configuration and private workspace material. Do not replace local checkpoint history with GitHub history or push checkpoint history to the public repository.
- Do not download datasets, ingest records, push database schemas, or publish the application merely to start the project.

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9, React, Vite, Tailwind CSS.
- Product engine: Python 3.13+, FastAPI, Pydantic, NetworkX, NumPy.
- Real-agent showcase: bounded LLM tool-calling agents launched from the workspace terminal, with working sandbox files and ledger operations checked by the ASP Gateway. The public dashboard only monitors their live sessions; it must not expose an unauthenticated endpoint that spends model credits.
- Machine-readable ASP declarations in JSON, validated by `ASPPolicy`.
- Optional local AI Village store: SQLite, under `data/ai-village/village.sqlite`; `SWARMSENTINEL_DATA_DIR` can select another storage directory.
- API contract: OpenAPI, with Orval-generated React client and Zod schemas.
- Express/PostgreSQL/Drizzle packages belong to the starter workspace API, not the SwarmSentinel engine. Synthetic and scripted live demos require neither `DATABASE_URL` nor an LLM API key; real LLM agents use the configured Replit OpenAI integration.

## Product modes

### Synthetic replay: enforce mode

- Scenarios: `normal`, `attack`, `injection` (poisoned invoice), and `runaway` (delegation loop and retry storm).
- The original normal and attack samples retain 15 and 41 input events and their exact original decisions, alerts, and revocations.
- The gateway applies admission/lineage, inherited restricted scopes, default-deny tools and network rules, argument constraints, content-trust contamination, and resource budgets.
- Prompt-injection defenses propagate taint and its origin through messages, withdraw payment capabilities from contaminated contexts, deny contaminated writes, and revoke implicated branches through an injection-spread tripwire.
- The mock policy sets depth 4, repeated-intent limit 5 across at least 3 agents, write cap 6, and per-agent step budget 20 within a 10-second window. Blocked in-band attempts count toward the step budget.
- A delegation-loop tripwire detects hand-offs returning to an earlier agent. These new loop, budget, and taint-propagation controls do not change the AI Village policy's historical replay behavior.

### AI Village replay: report-only mode

- The adapter normalizes authorized local exports of `aidigestorg/ai-village`, stores normalized events locally, and scans for candidate episodes.
- The dashboard provides an episode picker, replay speed, binned timeline, provenance, counterfactual decisions, and aggregate summaries.
- This mode needs an approved local dataset and a built store. It remains unavailable when those files are absent; `/api/swarm/sources` reports availability rather than silently substituting synthetic data.
- Historical results are **counterfactual**: say “would drop” or “would have blocked,” never that ASP prevented historical actions.
- The `village` root is a registry node, not an actor, and must never be revoked (`root_is_actor=False`).

### Live sessions: cooperative enforce mode

- Stateful gateways and Sentinel instances evaluate proposed actions before a cooperating caller executes them.
- `sdk/guard.py` supplies `Agent.call()` and the tool decorator; denied calls raise `PolicyViolation` rather than executing the tool. Spawn lineage is tracked and verified.
- The dashboard's Live tab can attach to sessions or launch a paced poisoned-invoice demo, receive decisions via server-sent events, scrub the timeline, and fetch fresh reports/traces.
- The demo executes local Python fixture tools, not external payment transactions.
- Sessions are held in process memory, capped at 200, and serialized per session. Restarting the engine loses live sessions; do not claim durable storage.
- An agent that bypasses the guard is not stopped. Cooperative interception is not an isolated security boundary.

## Run and verify on Replit

Use the existing managed workflows; they supply artifact-specific `PORT`, `BASE_PATH`, and same-origin proxy routing:

- `artifacts/swarm-sentinel: engine` — `pnpm --filter @workspace/swarm-sentinel run engine`; FastAPI on port 8000, routed at `/api/swarm`.
- `artifacts/swarm-sentinel: web` — `pnpm --filter @workspace/swarm-sentinel run dev`; dashboard at `/`.
- `artifacts/swarm-sentinel-deck: web` — optional eight-slide presentation at `/swarm-sentinel-deck/`.
- The starter API and mockup sandbox are not needed for the product dashboard.

Start the engine and dashboard only after this document has been updated. Anchor engine commands through its package script: managed commands do not necessarily run from the workspace root.

Checks from the repository root:

```bash
pnpm run typecheck
PORT=18947 BASE_PATH=/ pnpm --filter @workspace/swarm-sentinel run build
(cd artifacts/swarm-sentinel/python && python -m unittest discover tests)
```

Python dependencies are defined in the root `pyproject.toml` and `uv.lock`. Use Replit's Python environment, not a virtual environment. If dependency installation is needed:

```bash
uv export --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file /tmp/swarmsentinel-requirements.txt
uv pip install --system -r /tmp/swarmsentinel-requirements.txt
```

Outside Replit only, `SWARM_ENGINE_URL=http://127.0.0.1:8000` enables the checked-in Vite API proxy. Leave it unset for Replit's shared routing.

Other commands:

- `pnpm --filter @workspace/api-spec run codegen` — regenerate hooks and Zod schemas after OpenAPI changes.
- `python artifacts/swarm-sentinel/python/simulator.py` — regenerate synthetic samples.
- From `artifacts/swarm-sentinel/python`, `python examples/live_injection_demo.py` runs the local guard example; `--remote <engine-origin>` exercises HTTP interception.
- From that same directory, `python examples/bench_latency.py` benchmarks cooperative guard overhead. Numbers from another machine are not measurements of Replit performance.
- From the repository root, `python artifacts/swarm-sentinel/python/examples/real_agents_demo.py --mode normal` runs three real LLM-backed agents against the `agents` policy. Repeat with `--mode adversarial` for deliberate scope/contamination refusals. Open Live, select the labeled session, then Watch. Details: `docs/real-agent-showcase.md`.
- Optional, **only after authorized local data is supplied**: from that same directory, `python -m village.ingest --raw ../../../download-hugginface`, then `python -m village.scan`. Do not run these as part of normal startup.

API routes include `/api/swarm/health`, `/sources`, `/simulate`, `/sessions`, `/sessions/{id}`, `/sessions/{id}/evaluate`, `/sessions/{id}/stream`, and `/demo/injection` (all under `/api/swarm`). Interactive documentation is at `/api/swarm/docs`.

## Where things live

- `CLAUDE.md` — upstream engineering invariants, research framing, data rules, and offline development conventions.
- `artifacts/swarm-sentinel/python/` — gateway, policy models, detection, replay pipeline, reporting, FastAPI server, and sample generator.
- `artifacts/swarm-sentinel/python/policies/` — `mock.asp.json` and `ai-village.asp.json`.
- `artifacts/swarm-sentinel/python/village/` — normalization, ingestion, episode scanning, and local storage.
- `artifacts/swarm-sentinel/python/live.py`, `sdk/`, `demo.py`, `examples/` — live sessions, cooperative guard, attack demonstration, and benchmark.
- `artifacts/swarm-sentinel/python/agents/` and `policies/agents.asp.json` — bounded real-model tool loops, working sandbox tools, and their separate policy. Outputs/transcripts in `data/agent-demos/` are gitignored.
- `artifacts/swarm-sentinel/python/tests/test_engine.py` — hand-written fixtures, synthetic regression, policy/detector, injection, loop/budget, and live-guard tests.
- `artifacts/swarm-sentinel/src/pages/dashboard.tsx` — synthetic, AI Village, and Live dashboard.
- `lib/api-spec/openapi.yaml`, `lib/api-client-react/`, `lib/api-zod/` — API contract and generated clients.
- `docs/THREAT_MODEL.md` — controls, evidence, and honest enforced/report-only/gap status.
- `docs/ai-village-access.md`, `docs/aivillage-context.md` — access terms, handling rules, and research context.
- `docs/demo/`, `docs/source-materials/` — recorded demonstrations, source material, and exported presentations.
- `artifacts/swarm-sentinel-deck/` — editable presentation; `artifacts/api-server/` and `artifacts/mockup-sandbox/` — starter artifacts.

## Engineering invariants

- Keep synthetic regression expectations unchanged; fix implementation rather than weakening test assertions.
- Preserve ASP gateway check ordering, including deny-list precedence over allowlist, and lineage checks before accepting delegated actions.
- Put new configurable behavior in validated policy JSON rather than Python constants.
- New event/trace fields need defaults so older checked-in samples continue to validate.
- Repeated interactions are a **single directed edge with increasing weight**, never parallel edges. Detection density counts distinct pairs, not edge weight.
- API changes are additive. Pydantic models and OpenAPI schemas are maintained by hand: update both and regenerate the client together.
- Tripwire feedback affects subsequent in-band calls. Out-of-band observations cannot be retroactively blocked or undone.
- Intent fingerprints are deterministic hashes, not semantic embeddings; repeated-intent throttling is normalized-text matching.
- Keep `docs/THREAT_MODEL.md` status claims aligned with actual controls.

## Data handling and research framing

- Never commit or publish raw AI Village files, the SQLite store, or JSON trace exports from AI Village replays. Share code, policy declarations, and permitted aggregates only.
- Dataset access requires approval and research-use compliance. Use the user's signed-in browser or a token they configure through secrets tooling; never ask for a token in chat.
- Never use or echo credentials encountered in dataset content; report suspected leaks to maintainers.
- Human chat participants remain one anonymous `human` actor with text withheld. No re-identification.
- Cite: AI Digest, “AI Village dataset”, 2026, https://theaidigest.org/village.
- Narration and bash comments are claims, not ground truth. Check the dataset CHANGELOG before comparing periods, especially the 2026-02-10 auto-nudger and 2026-03-24 perma-computer-use changes.
- ASP is a proposal from the user's preprint, not an adopted standard. The gateway and recorder share a process; do not claim tamper-proof production enforcement.
- Keep `download-hugginface/`, `data/`, raw exports, credentials, and graph caches out of version control.

## Git and workspace safety

- GitHub source history and Replit checkpoint history are separate. Sync source snapshots while preserving remote history and repository-only files; never force-push or export private workspace history.
- Upstream feature work uses branches, no-fast-forward merges, release tags, and the user's signing configuration. Do not invent signatures or modify Git attribution/settings.
- Project commits must be GPG-signed. The user confirmed this requirement; do not create unsigned GitHub API commits. If no configured signer is available, provide the changed source/documents for a signed offline commit instead.
- Do not add agent co-author trailers or attribution. Ask before any force-push.
- Use schema-validated artifact tooling for metadata or service changes, not direct edits to `artifact.toml`.

## Gotchas

- Vite requires `PORT` and `BASE_PATH` even for builds; use managed workflows for preview startup.
- The committed native-package overrides target Linux x86_64. macOS workarounds in `CLAUDE.md` must not change Replit's lockfile.
- Live-mode Vite hot reload can temporarily orphan an EventSource and exhaust browser HTTP/1.1 connections; reload the page if a stream queues. Production builds are unaffected by that specific hot-reload issue.
- Busy graph hubs can have crowded weight labels; hover a node to isolate edges.
- `graphify-out/` is an optional local cache, not a runtime dependency. Its exclusions must continue to protect raw data, build outputs, and dependencies.
- See the pnpm-workspace instructions for shared-library typechecks, API codegen, and routing conventions.
