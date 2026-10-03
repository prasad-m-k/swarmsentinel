# SwarmSentinel

**Description:** AI Swarm Dynamics Hackathon.

**Status:** Working synthetic-sample prototype. The user approved building with a small sample set before obtaining AI Village dataset access.

SwarmSentinel combines an ASP-inspired in-band policy gateway with out-of-band monitoring, a directed interaction graph, dynamic tripwire feedback, and a forensic recorder. All agent activity and tools in this prototype are synthetic mocks. It does not enforce policy on real agents or MCP servers.

## Run & Operate

- `pnpm --filter @workspace/swarm-sentinel run dev` — web dashboard (managed artifact workflow).
- `pnpm --filter @workspace/swarm-sentinel run engine` — Python FastAPI engine (managed artifact workflow supplies PORT).
- `python artifacts/swarm-sentinel/python/simulator.py` — regenerate the checked-in 15-event normal and 41-event attack samples.

- `pnpm --filter @workspace/api-server run dev` — run the API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required env: `DATABASE_URL` — Postgres connection string

## Stack

- pnpm workspaces, Node.js 24, TypeScript 5.9
- API: Express 5
- DB: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `docs/aivillage-context.md` — working product context and source findings.
- `docs/source-materials/` — copies of the two PDFs supplied by the user.
- `artifacts/swarm-sentinel/` — React dashboard and Python core modules (`simulator`, `asp_gateway`, `sentinel`, `reporter`).
- `docs/ai-village-access.md` — manual research access steps and proposed use statement.
- `artifacts/api-server/` and `artifacts/mockup-sandbox/` — starter workspace artifacts.

## Architecture decisions

- Python FastAPI + NetworkX/Pydantic/NumPy core with a React interactive dashboard, one of the supplied prompt's permitted approaches.
- No external services, real agents, real MCP tools, or AI Village records are used. Runs are ephemeral; export Markdown or JSON before replacing a run.
- Out-of-band observations cannot be retroactively blocked. Tripwires revoke subsequent in-band calls from implicated agents and descendants.
- The gateway is separated in code, not isolated at an OS/security boundary; never claim tamper-proof production enforcement.
- Treat the AI Security Policy (ASP) paper as a proposal, not as an adopted standard or existing product.

## Product

Normal and swarm-attack sample replays with timeline scrubbing, interactive directed radar, ASP decisions, depth/intent/write policy controls, tripwire history, and forensic Markdown/JSON export. AI Village import remains future scope, contingent on access approval and permitted use.

## User preferences

- Project/app name: SwarmSentinel. Description: AI Swarm Dynamics Hackathon.
- Build with a small sample set first; then establish AI Village dataset access.

## Gotchas

- AI Village dataset access is reviewed and governed by research-use terms; verify current access status and permitted use before retrieval or analysis.
- Dataset agents can misreport events. Treat narration as claims, prefer structured records and screenshots, and consult the dataset changelog before longitudinal comparisons.
- Do not describe ASP as a deployed security control or guarantee unless a separate enforcement layer is actually implemented and verified.

## Pointers

- See the `pnpm-workspace` skill for workspace structure, TypeScript setup, and package details
