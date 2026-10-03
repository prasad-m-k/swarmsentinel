# SwarmSentinel

**ASP-inspired runtime policies and a multi-agent incident flight recorder.**

SwarmSentinel is an **AI Swarm Dynamics Hackathon prototype** for investigating runaway coordination in multi-agent systems. It combines a policy gateway, graph-based tripwire detection, and an interactive forensic dashboard.

> This version uses deterministic synthetic agent traces and mock tools. It does not execute real agents, connect to external MCP servers, or contain AI Village dataset records. Alerts are heuristic signals, not proof of malicious behavior.

## Live access

| Service | URL |
| --- | --- |
| Dashboard | **https://swarmsentinel.replit.app/** |
| Interactive API documentation | https://swarmsentinel.replit.app/api/swarm/docs |
| Health check | https://swarmsentinel.replit.app/api/swarm/health |
| OpenAPI schema | https://swarmsentinel.replit.app/api/swarm/openapi.json |

The dashboard and API are publicly accessible. The eight-slide presentation is included in this repository; the links above are for the deployed application.

## Purpose

Individual agent logs can miss system-level failures. Recursive delegation can expand a swarm, reciprocal messages can reinforce execution loops, and multiple agents can make excessive writes to shared resources.

SwarmSentinel explores how to:

- Evaluate in-band actions before simulated execution.
- Observe shared-state activity that occurs outside the gateway.
- Detect coordination patterns across a directed interaction graph.
- Feed tripwire findings back into a versioned revocation policy.
- Reconstruct what happened, which resources were affected, and where policy intervened.

The goal is **containment with evidence**, rather than assuming that an agent's narration is a trustworthy incident record.

## Features

- **Two replayable scenarios:** a normal run and a swarm attack, with 15 and 41 input events respectively.
- **Policy gateway:** agent admission and parent/span lineage checks, recursion-depth limits, repeated-intent throttling, mock-tool allowlisting, and write budgets.
- **Graph-based tripwires:** reciprocal communication clusters, coordinated wiki edits, and excessive admitted-child fan-out.
- **Optional policy feedback:** revoke implicated agents and block subsequent in-band calls from them and their descendants.
- **Flight recorder:** distinguish `allow`, `throttle`, `drop`, and out-of-band `observed` events, including policy versions and execution status.
- **Interactive dashboard:** directed agent/resource graph, event details, enforcement feed, tripwire history, policy controls, and timeline replay.
- **Exports:** download a Markdown incident report or a provenance-aware JSON run record after replay completes.
- **Presentation:** eight slides covering the problem, approach, design, architecture, and prototype boundaries.

### Default gateway policy

| Control | Default |
| --- | --- |
| Recursion depth | Maximum 4 hops |
| Repeated intent | Throttle the fifth matching normalized intent and target across at least 3 agent IDs within 10 seconds |
| Mock writes | Maximum 6 allowed writes per target within 10 seconds |
| Allowed tools | `mock:search.read`, `mock:mcp:wiki.write`, `mock:mcp:board.write` |
| Tripwire feedback | Enabled |

The dashboard can change depth, repeated-intent, and write limits for the next run, or disable tripwire feedback while retaining baseline gateway rules.

## Architecture

```text
Synthetic calls -> ASP gateway -> structured telemetry -> Sentinel graph/detection
                                                          |
Shared-state observations --------------------------------+
                                                          |
                                               Reporter + dashboard

Sentinel tripwire -> versioned revocation policy -> subsequent gateway decisions
```

- **Simulator:** generates the checked-in synthetic scenarios.
- **ASP gateway:** evaluates mock calls, maintains admitted identities and lineage, and records decisions.
- **Sentinel:** uses NetworkX to analyze executed interactions and identify coordination patterns.
- **Reporter:** produces a forensic summary with lineage, affected resources, timeline, and interventions.
- **Dashboard:** React + TypeScript + Tailwind CSS, consuming the Python FastAPI engine.
- **Validation:** Pydantic validates engine inputs; the workspace also includes an OpenAPI specification and generated TypeScript client.
- **Intent fingerprints:** NumPy supports deterministic hash-derived fingerprints, not learned semantic embeddings. Throttling uses normalized text equality.

Policy enforcement and recording are separate modules in **one prototype process**, not isolated security services.

### Heterogeneous agent swarms

The architecture is model-agnostic: decisions depend on identities, lineage, actions, targets, and observed relationships rather than model internals. Real heterogeneous swarms would need adapters that normalize these fields and route controllable actions through the gateway before execution.

This prototype has **not** been validated with live heterogeneous models or frameworks. Different wording for equivalent intents is not recognized as semantic equivalence, and production integrations would need trusted identity registration and workload-specific threshold tuning.

## Try the demo

1. Open the [live dashboard](https://swarmsentinel.replit.app/). It loads the normal sample.
2. Select **Swarm Attack**, then **Run Demo**.
3. Use **Play**, **Step**, **Reset**, and the timeline to inspect the replay.
4. Select graph nodes or events and inspect the enforcement feed, tripwires, and policy changes.
5. Change policy limits or the feedback setting, then run the scenario again for comparison.
6. After replay completes, download the JSON traces and Markdown report.

Runs are ephemeral. Export a run before replacing it with another simulation.

![SwarmSentinel dashboard showing a synthetic normal run](artifacts/swarm-sentinel-deck/public/images/dashboard.jpg)

*Dashboard screenshot of the synthetic normal scenario, not an empirical evaluation result.*

## Build and run

### Prerequisites

- **Node.js 24**
- **pnpm 10** — this workspace requires pnpm, not npm or Yarn.
- **Python 3.13 or newer**
- **uv** for installing the locked Python dependencies
- A Linux x86_64 environment, matching the current workspace's platform-specific package overrides

The SwarmSentinel engine does not require an LLM API key or database. The repository also contains starter API/database packages; those are not required to run the SwarmSentinel simulation engine.

### 1. Clone and install

```bash
git clone https://github.com/prasad-m-k/swarmsentinel.git
cd swarmsentinel

pnpm install --frozen-lockfile

# Export the locked Python dependencies, then install them into the
# current Python environment. --system requires an environment where
# system-level package installation is permitted, such as Replit.
uv export --frozen --no-dev --no-emit-project \
  --format requirements-txt --output-file /tmp/swarmsentinel-requirements.txt
uv pip install --system -r /tmp/swarmsentinel-requirements.txt
```

The Python manifest and lockfile are at the repository root: `pyproject.toml` and `uv.lock`.

### 2. Run the engine

From the repository root:

```bash
PORT=8000 pnpm --filter @workspace/swarm-sentinel run engine
```

Alternatively, run Uvicorn directly:

```bash
python -m uvicorn server:app \
  --app-dir artifacts/swarm-sentinel/python \
  --host 0.0.0.0 --port 8000
```

Check it at http://localhost:8000/api/swarm/health and open the API documentation at http://localhost:8000/api/swarm/docs.

### 3. Run the dashboard

**In Replit:** use the managed `artifacts/swarm-sentinel: web` and `artifacts/swarm-sentinel: engine` workflows. Artifact configuration supplies `PORT`, `BASE_PATH`, and same-origin routing.

**Outside Replit:** the frontend requests `/api/swarm/*` on its own origin. The checked-in Vite configuration relies on Replit routing and does not include a local API proxy. Add this property inside the existing `server` object in `artifacts/swarm-sentinel/vite.config.ts`, retaining its other settings:

```ts
proxy: {
  '/api/swarm': {
    target: 'http://127.0.0.1:8000',
    changeOrigin: true,
  },
},
```

If you use Vite's production preview, add the same `proxy` property inside its `preview` object as well.

Then, in a second terminal at the repository root:

```bash
PORT=5173 BASE_PATH=/ pnpm --filter @workspace/swarm-sentinel run dev
```

Open **http://localhost:5173/**. Keep the engine terminal running.

Both Vite configuration variables are required, including during builds. `BASE_PATH=/` places the dashboard at the site root.

### 4. Check and build

```bash
# Build/check shared TypeScript library references.
pnpm run typecheck:libs

# Check and build the dashboard.
pnpm --filter @workspace/swarm-sentinel run typecheck
PORT=5173 BASE_PATH=/ pnpm --filter @workspace/swarm-sentinel run build

# Validate, check, and build the presentation.
pnpm --filter @workspace/swarm-sentinel-deck run validate-slides
pnpm --filter @workspace/swarm-sentinel-deck run typecheck
PORT=25392 BASE_PATH=/swarm-sentinel-deck/ \
  pnpm --filter @workspace/swarm-sentinel-deck run build
```

Dashboard output is in `artifacts/swarm-sentinel/dist/public/`; presentation output is in `artifacts/swarm-sentinel-deck/dist/public/`.

For the whole workspace, `pnpm run typecheck` checks all configured packages and `pnpm run build` checks and builds them. Frontend build processes still need `PORT` and `BASE_PATH`; Replit's managed artifact builds supply each artifact's own values.

### 5. Run the presentation

```bash
PORT=25392 BASE_PATH=/swarm-sentinel-deck/ \
  pnpm --filter @workspace/swarm-sentinel-deck run dev
```

Open **http://localhost:25392/swarm-sentinel-deck/slide1**.

### Production hosting

Serve the dashboard build as static files, with SPA fallback to `index.html`, and route `/api/swarm/*` to the running FastAPI engine. Vite preview is for checking a build, not a production application server.

Replit service build commands, paths, and engine startup configuration are recorded in each artifact's `.replit-artifact/artifact.toml`.

## API example

```bash
curl -X POST http://localhost:8000/api/swarm/simulate \
  -H 'Content-Type: application/json' \
  -d '{
    "scenario": "attack",
    "feedbackEnabled": true,
    "maxDepth": 4,
    "semanticLimit": 5,
    "writeLimit": 6
  }'
```

`scenario` accepts `normal` or `attack`. The response contains the run ID, synthetic provenance, recorded events, alerts, policy changes, and Markdown report.

## Repository layout

```text
artifacts/
  swarm-sentinel/
    src/                         React dashboard
    python/
      asp_gateway.py             Policy evaluation and agent revocation
      sentinel.py                Graph-based tripwire detection
      reporter.py                Markdown forensic reports
      simulator.py               Deterministic sample generation
      models.py                  Pydantic request/event/trace models
      server.py                  FastAPI endpoints
      samples/                   Checked-in normal and attack traces
  swarm-sentinel-deck/           Eight-slide presentation
  api-server/                   Starter workspace API service
  mockup-sandbox/                Component preview workspace
lib/
  api-spec/                     OpenAPI contract and code generation
  api-client-react/             Generated client and React hooks
docs/                           Research context and dataset access constraints
pyproject.toml                  Python dependencies
uv.lock                         Python dependency lock
pnpm-workspace.yaml              JavaScript workspace configuration
pnpm-lock.yaml                   JavaScript dependency lock
```

Regenerate synthetic samples with:

```bash
python artifacts/swarm-sentinel/python/simulator.py
```

Regenerate API client code after changing the OpenAPI contract with:

```bash
pnpm --filter @workspace/api-spec run codegen
```

## Scope and limitations

- **Synthetic only:** no external tool execution, real incident replay, or real AI Village records.
- **In-band containment only:** out-of-band activity is observed; the gateway cannot block or undo it.
- **No tamper-proof boundary:** policy and recording share a process. Production enforcement would require isolation and durable, trustworthy telemetry.
- **Exact normalized intent matching:** differently worded equivalent intents can evade repeated-intent throttling.
- **Heuristic detection:** legitimate collaboration can trigger alerts. Thresholds require validation against real workloads.
- **No persistent run store:** simulations are independent and records must be exported for retention.
- **Not a production security guarantee:** ASP is an inspiration for the prototype, not a claim of conformance to an adopted standard.

AI Village dataset ingestion and empirical evaluation are deferred. Dataset access and any analysis must respect approval and research-use terms; see [access notes](docs/ai-village-access.md) and [research context](docs/aivillage-context.md).