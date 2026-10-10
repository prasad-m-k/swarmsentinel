# MCP proxy mode

Design, feasibility and build plan for routing agent tool calls through an MCP endpoint hosted by the engine, so that enforcement no longer depends on the agent calling the gateway. Status: phase 1 built on `feature/mcp-proxy`. The technical design is in `mcp-proxy-design.md`. The checklist at the end tracks what exists.

ASP is a proposal from the preprint (`source-materials/agent-security-policy-asp.pdf`), and this is a prototype implementation of it. Nothing here makes the engine tamper-proof.

## The claim, stated precisely

Today the generic path is cooperative (threat model gap G1). An agent asks `/evaluate` and is trusted to respect a refusal and to report completion honestly. The registered sandbox tools (`/execute`) are the exception: the engine evaluates and runs the body itself, so skipping the SDK gains nothing.

The MCP proxy extends that exception to any MCP server. It removes agent cooperation only when the proxy is the agent's sole route to side effects. That is a deployment property, not something the proxy code provides:

- The agent host exposes no shell, native file tools or other tool channels outside the proxy.
- The agent has no network route to the downstream servers and does not hold their credentials. The engine launches the servers itself and keeps their credentials.
- The agent's MCP client config, including its proxy credential, is set by the operator, not chosen by the model.

When those hold, the agent controls only the tool name and the arguments it sends. Identity, lineage, write and read semantics, network destination, intent and execution evidence all come from the engine.

## How a call flows

```text
Agent host (confined: no shell, no egress except the engine)
   │  MCP over Streamable HTTP, Authorization: Bearer <agent credential>
   ▼
Engine (FastAPI) ── /api/swarm/mcp ── LiveSession of that credential
   │  1. credential → (session, agentId, lineage), issued by the engine at spawn time
   │  2. tools/call → policy `mcp` map → Event(target, write, reads, network, resource, intent)
   │  3. ASPGateway.evaluate_and_log, Algorithm 1 order unchanged
   │  4. drop or throttle → tool result with isError: true, rule and reason; downstream never called
   │  5. allow → lock released, call the downstream server, re-lock and record the outcome
   │  6. untrusted tool output taints the actor; Sentinel tripwires revoke as today
   ▼
Downstream MCP servers, launched by the engine over stdio per session
```

This is `LiveSession.execute` generalized. `REGISTRY` in `sandbox.py` becomes the policy's `mcp` section, and `Sandbox.dispatch` becomes a call to a real MCP server. Admission, taint pinning and `engine-observed` provenance behave the same way.

## Design decisions

**Hosted inside the engine.** The endpoint runs on the engine's FastAPI app, so taint, counters, revocations, the Sentinel graph and the dashboard stream all share one `LiveSession`. A separate proxy process calling `Guard.remote()` would put completion back in a caller's hands (`caller-reported`), which is the property this mode exists to remove. Clients that only speak stdio can use a thin local bridge to the HTTP endpoint.

**Tool semantics live in policy.** A new optional `mcp` section in the ASP policy JSON, validated by `ASPPolicy`, declares each downstream server and the tools it may expose:

```json
"mcp": {
  "servers": {
    "notes": {
      "command": "{python}",
      "args": ["{fixtures}/notes_server.py", "--root", "{workspace}"],
      "tools": {
        "read_note":  {"target": "mcp:notes:read", "write": false},
        "write_note": {"target": "mcp:notes:write", "write": true, "resource": "mcp:notes"},
        "fetch_page": {"target": "mcp:web:fetch", "write": false, "reads": "untrusted", "network-argument": "url"}
      }
    }
  }
}
```

`{python}` is the engine's interpreter, `{fixtures}` is the bundled fixture directory and `{workspace}` is a disposable per-session directory. Policy files are operator configuration chosen by a fixed name; callers can never supply a command, argument or path. Downstream servers inherit only the SDK's minimal default environment, not the engine's secrets.

A tool the policy does not declare is never listed and never dispatched, even if the downstream server offers it. Server annotations such as `readOnlyHint` are ignored because the server sets them about itself. Targets still go through the normal allowlist, deny list, argument rules and network scope.

**Identity is issued, not claimed.** MCP carries no agent identity or lineage. The owner mints an agent through `POST /api/swarm/sessions/{id}/mcp/agents`. That request is evaluated as a `spawn` from the named parent, so inherit-and-restrict scopes, the depth cap and revocation apply. The response holds a random bearer credential bound to that session and agent; the engine stores only its hash. Every MCP request resolves the caller from that credential, so an agent cannot act under another agent's ID. This narrows gap G7 for the proxy path. It does not stop the owner from minting agents.

**Intent and detail are derived.** Repeated-intent detection used to read free text supplied by the agent. In proxy mode the intent is the exposed tool name plus canonical JSON arguments, as `/execute` already does. Calls whose canonical arguments exceed the 4,000 character detail limit are refused rather than truncated, since truncation could hide text from argument rules.

**Admission and dispatch are split.** `/execute` holds the session lock across the tool body, which is fine for local fixtures but would queue every agent behind one slow downstream call. The proxy admits under the lock, releases it for the downstream call, then re-acquires it to record `succeeded` or `failed`. Write caps and repeated intent already count at admission, so concurrent calls cannot slip past them. While a call is in flight its row shows the body as entered with no outcome yet; the stream sends the outcome as an update. A revocation that lands mid-call stops the next call, not the current one.

**Outcome evidence.** The engine sees the downstream response, so every proxied tool row carries `executionProvenance: engine-observed`. A result with `isError: true` and a transport failure are both recorded as `failed` with the fixed `tool_execution_failed` code. The downstream result is returned to the agent unchanged. The engine records that the server answered; it cannot verify what the server did.

**Taint.** A tool declared `reads: untrusted` taints the calling span and, as in `/execute`, the actor and its descendants. Content is not inspected (gap G4 still applies). Cross-agent propagation needs messages to pass through the engine; a proxy-hosted `swarm.send_message` tool is planned for that (phase 2).

## Feasibility

| Area | Verdict | Notes |
|---|---|---|
| Decision logic | Reused unchanged | `ASPGateway`, Sentinel and revocation need no changes. Every change is additive, so `SyntheticRegression` is unaffected |
| Execution pattern | Generalized | Reservation, provenance and failure handling follow `/execute` |
| Policy `mcp` section | New, moderate | Most of the design work is labelling real servers correctly |
| Identity | New, moderate | Credential minting, hashing, binding to session and lineage |
| Concurrency | Main technical risk | Admit, dispatch outside the lock, record. Covered by the split above |
| Sync and async | Moderate | The MCP SDK is async; the gateway is synchronous and fast, so evaluation runs on a worker thread |
| Resources, prompts, sampling, elicitation | Not offered | The front server does not implement them, so clients see no such capability. Listed as a gap rather than passed through unchecked |
| Confinement | Outside the code | Needs a documented container recipe and a bypass test (phase 3) |
| Dependency | Small | `mcp` Python SDK (2.3.x) |

## Coverage and limits

What the proxy supports claiming, for an agent confined to it: every tool action is decided by the gateway before it runs, identity and lineage come from the engine, and outcome evidence is engine-observed.

What it does not support claiming:

- Tamper resistance. The engine is one process; anything with host access can bypass or alter it (G6).
- Correct semantics for a mislabelled tool. If the policy calls a write tool read-only, the gateway judges it as read-only.
- Knowledge of what a downstream server actually did beyond its response.
- Coverage of anything not routed through the proxy. Unconfined tool channels stay out-of-band (T7).
- Rollback of a call that was already admitted when a revocation landed.

## Running it

```bash
# Engine (from repo root); create a session with policy "mcp-demo" as the signed-in owner
.venv/bin/python -m uvicorn server:app --app-dir artifacts/swarm-sentinel/python --host 127.0.0.1 --port 8000

# Mint an agent (owner token required); the credential is returned once
curl -s -X POST http://127.0.0.1:8000/api/swarm/sessions/$SESSION/mcp/agents \
  -H "Authorization: Bearer $OWNER_TOKEN" -H "Content-Type: application/json" \
  -d '{"agentId": "writer", "scope": ["mcp:notes:*"]}'

# Point any Streamable HTTP MCP client at the endpoint with that credential
#   URL: http://127.0.0.1:8000/api/swarm/mcp   Header: Authorization: Bearer ssa_...

# Tests (from artifacts/swarm-sentinel/python)
../../../.venv/bin/python -m unittest discover tests -p "test_mcp_*.py"
```

Exposed tool names are `<server>__<tool>`, for example `notes__write_note`. Model tool-name rules (Claude, OpenAI) allow only letters, digits, `_` and `-`, so server names are limited to letters, digits and `-` and the double underscore always splits unambiguously. The MCP endpoint speaks the MCP protocol rather than REST, so it is not in `openapi.yaml`; the minting endpoint is.

## Code map

| Path | Role |
|---|---|
| `python/mcp_proxy/broker.py` | Credentials, admission under the session lock, outcome recording |
| `python/mcp_proxy/downstream.py` | Engine-launched stdio servers per session, disposable workspace |
| `python/mcp_proxy/front.py` | MCP server the agents connect to (tools only), credential check |
| `python/mcp_proxy/fixtures/notes_server.py` | Offline fixture server that logs every executed body |
| `python/policies/mcp-demo.asp.json` | Demo policy with the `mcp` section |
| `python/tests/test_mcp_proxy.py` | Policy, broker and end-to-end tests over real HTTP and stdio |
| `python/tests/mcp_sample/` | Sample workspace MCP server and its test policy |
| `python/tests/test_mcp_gateway.py` | Every gateway check reached through the proxy against the sample server |

## Phases

| Phase | Scope | Status |
|---|---|---|
| 0. Design | This document; `mcp` policy schema; credential model; lock strategy | Done |
| 1. MVP | Engine-hosted Streamable HTTP endpoint; stdio downstream pool per session; policy-mapped events; deny returned as a tool error; engine-observed outcomes; agent minting endpoint; fixture server and tests showing a refused call never reaches the server | Done |
| 2. Swarm | `swarm.send_message` for cross-agent taint; `tools/list_changed` on revocation (needs stateful sessions); dashboard label for proxied rows | Planned |
| 3. Confinement | Container recipe with egress only to the engine; agent-host config exposing only MCP tools; bypass test suite; latency benchmark through the proxy | Planned |
| 4. Threat model | Full rewrite of T7, T8, G1, G6, G7 once confinement exists. Phase 1 added only a short G1 note and gap G10 | Planned |

## Checklist

- [x] `mcp` policy section in `asp_policy.py`
- [x] `mcp-demo` policy and fixture server
- [x] Proxy core: credentials, admission, dispatch, recording
- [x] Downstream stdio pool with per-session workspace
- [x] Engine endpoint `/api/swarm/mcp` and agent minting
- [x] `source: "mcp"` on `Event`, OpenAPI twin, codegen
- [x] Tests: refusal never dispatches, taint withdraws writes, credential binding, downstream failure, end to end over HTTP
- [x] Sample MCP server and gateway suite covering every Algorithm 1 check
- [x] Design document (`mcp-proxy-design.md`)
- [ ] `swarm.send_message` and message taint
- [ ] `tools/list_changed` on revocation
- [ ] Confinement recipe and bypass tests
- [x] Content inspection of arguments and results (`content-inspection.md`)
- [x] Framework adapters (`adapters.md`)
- [x] Durable sessions, tenancy and roles (`durable-state.md`)
- [x] Threat model rows T5, T9, T11 and gaps G4, G7, G9, G10 updated
- [ ] Full threat model rewrite after confinement
- [ ] Credential expiry (credentials currently live as long as their session)
