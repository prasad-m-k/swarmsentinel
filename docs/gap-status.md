# Gap status against production AI gateways

A review compared SwarmSentinel with products such as Gravitee and TrueFoundry and listed seven gaps. This page records what was done about each on `feature/mcp-proxy`, and what remains. SwarmSentinel is a research prototype of the ASP proposal; closing these gaps narrows the distance to such products without making it one.

| Gap | Status | Where |
|---|---|---|
| Interception is cooperative (G1) | **Closed for MCP traffic**, under the confinement condition below. Generic SDK calls remain cooperative | `mcp-proxy-design.md`, `tests/test_mcp_gateway.py` |
| No content inspection | **Added**: secrets, PII and injection-marker scanning of arguments (report or deny) and results (report, redact, block or taint), configured per policy | `content-inspection.md`, `tests/test_content_inspection.py` |
| No provider routing, token accounting or cost tracking | **Out of scope**, by design (see below) | This page |
| Prototype-grade state and identity | **Narrowed**: durable sessions with exact replay, eviction instead of refusal, per-tenant quotas; tenancy and viewer, operator and admin roles from Clerk organization claims; engine-issued agent credentials for MCP | `durable-state.md`, `tests/test_persistence.py`, `tests/test_rbac.py` |
| No adapters | **Added**: Claude Agent SDK (with host-level tool confinement), LangChain, Claude Code and generic MCP clients, all through the MCP endpoint | `adapters.md`, `tests/test_adapters.py` |
| Thresholds calibrated on one dataset; one platform documented | **Platforms documented** (Linux x86_64 and macOS arm64 for the engine). **Calibration open** (see below) | `README.md` |
| No compliance mappings | **Added**: OWASP LLM Top 10 2025, OWASP Agentic Top 10 2026, NIST AI RMF 1.0, ISO/IEC 42001 Annex A, with honest statuses | `compliance-mapping.md` |

## What "closed for MCP traffic" means

The engine decides and executes every proxied tool call, assigns identity from its own credentials, and records engine-observed outcomes. That removes the agent's cooperation from the loop only when the agent has no other route to side effects. The Claude Agent SDK adapter removes built-in tools and foreign MCP configuration at the host level; an operating-system boundary (container with egress only to the engine) is still the deployer's job and is the next planned phase.

## Why provider routing and cost tracking are out of scope

Gravitee and TrueFoundry sit between an application and model providers: they route prompts, meter tokens and enforce spend. SwarmSentinel sits between agents and their tools, and between agents. The two are complementary layers, and an LLM gateway in front of the model calls would sit alongside it. What SwarmSentinel lacks inside its own scope is a budget expressed in cost rather than actions. The step budget counts actions per agent; extending it to tokens or cost would need the agent host to report usage, which brings back caller-reported evidence. That trade-off is recorded as future work rather than built.

## Calibration

Tripwire thresholds were tuned on the AI Village dataset, where broadcast chat and assigned collaboration make some patterns normal. They are policy values, not constants, so every deployment can set its own, but there is no second dataset to calibrate against yet. Until there is, treat the defaults as a starting point and expect to tune them per deployment. The synthetic scenarios and the sample MCP policy use deliberately small limits for testing and are not recommendations.

## Still open

- Operating-system confinement recipe and bypass tests (MCP proxy phase 3)
- Agent-to-agent messages through the proxy, so taint propagates (phase 2)
- Credential expiry shorter than the session
- Audit records of access decisions; cross-tenant administration
- Token or cost budgets (see above)
- A second dataset for threshold calibration
