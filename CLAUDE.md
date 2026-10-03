# CLAUDE.md: SwarmSentinel

ASP-inspired policy gateway plus multi-agent flight recorder (AI Swarm Dynamics Hackathon). Two data paths:

- **Synthetic scenarios** (`normal`, `attack`): mock tools, gateway runs in **enforce** mode.
- **AI Village replay**: real records from the gated `aidigestorg/ai-village` dataset, normalized into a local store and replayed in ASP **report-only** mode.

Current release: `v2.0.0`. Research basis: `docs/source-materials/agent-security-policy-asp.pdf` (the ASP preprint). `replit.md` is for Replit's agent and may lag behind this file.

## Layout

| Path | What it is |
|---|---|
| `artifacts/swarm-sentinel/python/` | FastAPI engine: `asp_policy.py`, `asp_gateway.py`, `sentinel.py`, `pipeline.py`, `reporter.py`, `server.py`, `models.py`, `simulator.py` |
| `artifacts/swarm-sentinel/python/policies/` | `mock.asp.json` (synthetic), `ai-village.asp.json` (replay). Policy lives here, not in code |
| `artifacts/swarm-sentinel/python/village/` | AI Village adapter: `normalize.py`, `ingest.py`, `scan.py`, `store.py` |
| `artifacts/swarm-sentinel/python/tests/` | `test_engine.py`, hand-written fixtures only |
| `artifacts/swarm-sentinel/src/pages/dashboard.tsx` | The whole React dashboard |
| `lib/api-spec/openapi.yaml` | API contract; `lib/api-client-react` and `lib/api-zod` are generated from it |
| `docs/` | Research context, dataset access and handling rules, source PDFs, decks |
| `download-hugginface/`, `data/ai-village/` | Raw dataset files and the normalized SQLite store. Local only, gitignored |

`artifacts/api-server`, `artifacts/mockup-sandbox`, `artifacts/swarm-sentinel-deck` are starter or presentation apps, not the product.

## Commands

Python (repo root): `uv sync --frozen` creates `.venv`. pnpm is not installed globally on this Mac; use `npx -y pnpm@10`.

```bash
# Engine tests (from artifacts/swarm-sentinel/python)
../../../.venv/bin/python -m unittest discover tests

# Engine on :8000 (from repo root)
.venv/bin/python -m uvicorn server:app --app-dir artifacts/swarm-sentinel/python --host 127.0.0.1 --port 8000

# Dashboard dev server proxying to the engine
SWARM_ENGINE_URL=http://127.0.0.1:8000 PORT=5173 BASE_PATH=/ npx -y pnpm@10 --filter @workspace/swarm-sentinel run dev

# After any OpenAPI change
npx -y pnpm@10 --filter @workspace/api-spec run codegen
npx -y pnpm@10 run typecheck

# AI Village store (from artifacts/swarm-sentinel/python): ingest ~5 min, scan ~3 min
../../../.venv/bin/python -m village.ingest --raw ../../../download-hugginface
../../../.venv/bin/python -m village.scan
```

macOS caveat: `pnpm-workspace.yaml` strips every darwin native binary (Replit is linux-x64), so `vite dev`/`vite build` fail from the committed lockfile here. Typecheck and codegen work. For a local preview, copy the repo to a scratch dir, drop the `"-"` platform overrides, and `pnpm install --no-frozen-lockfile` there. Never commit that lockfile.

## Engine rules

- The synthetic scenarios must keep producing exactly their v1 decisions, alerts and revocations. `SyntheticRegression` in `tests/test_engine.py` pins this. Do not change those assertions to make a test pass; fix the code.
- Gateway check order follows ASP Algorithm 1: lineage and admission, then explicit deny list, default-deny allowlist, argument constraints, network scope, content-trust contamination, then write cap and repeated intent. Keep that order.
- New behaviour goes into the policy JSON (validated by `ASPPolicy`) rather than constants in Python.
- New `Event`/`Trace` fields must have defaults so `samples/*.json` still validate.
- API changes are additive. When a Pydantic model changes, update its twin schema in `openapi.yaml` and run codegen; the two are maintained by hand.
- The `village` root in replays is a registry node, not an actor: it is never revoked (`root_is_actor=False`). The synthetic `orchestrator` is an actor and can be.

## Data handling (hard rules)

- Never commit or publish raw dataset files, the SQLite store, or JSON trace exports from AI Village replays. Share aggregates, policy files and code only.
- Never use or echo a credential found in the data; the dataset's redaction is best-effort. Report it to the maintainers.
- Human chat participants stay one anonymous `human` actor with text withheld. No re-identification.
- Cite: AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village
- Dataset access goes through the user's signed-in browser or a token they configure themselves; never ask for a token in chat.

## Framing (in code, docs, reports, decks)

- AI Village results are **counterfactual**: "would drop", "would have blocked". Never say ASP prevented or contained anything historical.
- ASP is a proposal from the user's preprint, not an adopted standard; the gateway is a code boundary in one process, not tamper-proof.
- Agent narration (chat text, bash comments) is a claim, not ground truth.
- Compare periods only with the dataset CHANGELOG in mind: 2026-03-24 perma-computer-use and 2026-02-10 auto-nudger change the data's shape.

## Git

- Branch for feature work, merge with `--no-ff`, tag releases (`vX.Y.Z`). Commits and tags are GPG-signed by the user's config.
- Ask before force-pushing; `main` is published at github.com/prasad-m-k/swarmsentinel.

## Knowledge graph (graphify)

`graphify-out/` (gitignored) holds a cached graph of the core code and docs: `GRAPH_REPORT.md`, `graph.json`, `graph.html`. For broad "how does X connect to Y" questions, read `GRAPH_REPORT.md` or use `/graphify query` before reading many files.

- Scope is fixed by `.graphifyignore` (90 core files). It replaces `.gitignore` for graphify, so it must keep excluding `download-hugginface/`, `data/`, `node_modules/`, `dist/` and `graphify-out/`.
- Git hooks rebuild the code graph after each commit and checkout (log: `~/.cache/graphify-rebuild.log`). Doc or paper changes need `/graphify . --update`.
- AST node ids carry the parent folder: `python_asp_gateway_aspgateway`, `village_normalize_normalizer`, `pages_dashboard_dashboard`.

## Known issues

- Dashboard replay of large AI Village episodes runs well below its nominal 10x/50x speed (feed and graph re-render per tick).
- Office apps are sandboxed: to export a .pptx to PDF via AppleScript, work inside `~/Library/Containers/com.microsoft.Powerpoint/Data/`, not `/tmp`.
