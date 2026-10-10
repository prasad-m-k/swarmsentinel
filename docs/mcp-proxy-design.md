# MCP proxy mode: design

Technical design for SwarmSentinel's MCP proxy mode. The plan, feasibility study and build status are in `mcp-proxy.md`; this document describes how the built system works and why. Code lives in `artifacts/swarm-sentinel/python/mcp_proxy/`.

ASP is a proposal from the preprint (`source-materials/agent-security-policy-asp.pdf`). This is a prototype implementation of it, not a tamper-proof enforcement point.

## 1. Problem

The gateway decides well, but on the generic path it only decides. An agent using the SDK asks `/evaluate`, then runs the tool itself and reports what happened. A refusal holds only if the agent honours it, and completion evidence is `caller-reported`. Threat model gap G1 records this.

The registered sandbox tools behind `/execute` already avoid the problem for four fixture tools: the engine evaluates and runs the body itself. MCP proxy mode applies that pattern to any tool exposed by an MCP server, so the agent never holds the capability it is asking to use.

## 2. Goals and non-goals

Goals:

1. A tool call that the gateway refuses never reaches the downstream server, whatever the agent does.
2. Agent identity and lineage come from the engine, not from anything the agent sends.
3. Every input the gateway uses (target, write flag, read trust, network destination, resource, intent) is derived by the engine from operator policy and the call arguments.
4. Proxied calls land in the same live session as everything else, so taint, swarm counters, Sentinel tripwires, revocation and the dashboard work unchanged.
5. All changes are additive. Synthetic scenarios keep their v1 decisions (`SyntheticRegression`).

Non-goals for this design:

- Confining the agent host. The proxy is only the sole route to side effects if the deployment makes it so (section 10).
- Inspecting tool output for injected instructions (gap G4 stands; contamination is assigned by source).
- Mediating MCP resources, prompts, sampling or elicitation. They are not offered.
- Durable sessions. Like other live sessions, proxy state is in memory.

## 3. Architecture

```text
             operator                                 agent host (confined)
                │ Clerk JWT                                │ Bearer ssa_… credential
                ▼                                          ▼
   POST /sessions/{id}/mcp/agents              POST /api/swarm/mcp  (Streamable HTTP, JSON)
                │                                          │
                ▼                                          ▼
   ┌──────────────────────────── engine process (FastAPI) ─────────────────────────────┐
   │  server.py ──► LiveSession ◄──────── front.py (Endpoint, MCP Server: tools only)   │
   │                  │  gateway (ASPGateway)                 │                          │
   │                  │  sentinel (Sentinel)                  │                          │
   │                  └─ mcp (ProxyBroker) ── credentials, admission, recording          │
   │                         └─ pool (DownstreamPool) ── one stdio client per server     │
   └──────────────────────────────────────┬─────────────────────────────────────────────┘
                                          │ stdio, launched by the engine
                                          ▼
                       downstream MCP servers in a per-session workspace
```

| Component | File | Responsibility |
|---|---|---|
| Policy schema | `asp_policy.py` (`Mcp`, `McpServer`, `McpTool`) | Validates the optional `mcp` section: servers, launch commands, tool semantics |
| `ProxyBroker` | `mcp_proxy/broker.py` | One per live session whose policy has an `mcp` section. Mints agents, resolves exposed names, admits calls, records outcomes, holds credentials |
| `DownstreamPool` | `mcp_proxy/downstream.py` | Launches declared servers over stdio on first use, shares one client per server, owns the disposable workspace, shuts everything down with the session |
| `Endpoint` | `mcp_proxy/front.py` | The MCP server agents connect to. Rejects requests without a valid credential before MCP parsing, then serves `tools/list` and `tools/call` |
| Minting route | `server.py` (`mint_mcp_agent`) | Owner-authenticated REST endpoint that spawns an agent and returns its credential |

The proxy lives inside the engine rather than in a separate process. A separate process would have to report outcomes back over HTTP as a caller, which reintroduces caller-reported evidence and splits state. Section 12 compares the alternatives.

## 4. Data model

### 4.1 Policy `mcp` section

```json
"mcp": {
  "servers": {
    "<server>": {
      "command": "{python}",
      "args": ["<script>", "--root", "{workspace}"],
      "tools": {
        "<tool>": {
          "target": "mcp:<area>:<verb>",
          "write": true,
          "reads": "untrusted",
          "resource": "mcp:<shared-state>",
          "network-argument": "url"
        }
      }
    }
  }
}
```

| Field | Required | Meaning |
|---|---|---|
| `command`, `args` | yes | How the engine launches the server. Placeholders: `{python}` (engine interpreter), `{fixtures}` (bundled fixtures), `{workspace}` (per-session directory). Callers can never supply these |
| `tools` | yes | The only tools that will ever be exposed or dispatched from this server |
| `target` | yes | Gateway target, matched by allow and deny lists, scopes, argument rules and `contaminated-deny` |
| `write` | yes | Counts against the write cap and is refused from a contaminated context when `contaminated-write` is `deny` |
| `reads` | no | `untrusted` taints the calling span and agent once the call is admitted |
| `resource` | no | Shared state key. The write cap falls back to the target when it is empty; the `shared_state_loop` tripwire only watches writes that name a resource |
| `network-argument` | no | Argument holding a URL or host; its hostname is checked against `network.allow` |

Requiring `write` rather than defaulting it is deliberate. The old convention of inferring a write from a `.write` suffix does not hold for arbitrary MCP tools, and a missing label should fail validation, not silently mean read-only.

### 4.2 Agents and credentials

`ProxyBroker.agents` maps an agent ID to `McpAgent(agentId, parentId, parentSpanId, spanId)`. All four values are generated or recorded by the engine at mint time. The root has a fixed engine-generated span per session.

A credential is `ssa_` plus 32 random URL-safe bytes. Only its SHA-256 digest is stored, in a process-wide index `digest → (session id, agent id)`, so the endpoint can route a request to its session. The plaintext is returned once by the minting endpoint. Closing or deleting the session removes its digests.

### 4.3 How a call becomes an Event

| Event field | Source in proxy mode |
|---|---|
| `agentId`, `parentId`, `spanId`, `parentSpanId` | Credential → `McpAgent` |
| `action`, `channel` | `tool`, `in_band` |
| `target`, `write`, `reads`, `resource` | Policy `mcp` tool entry |
| `network` | Hostname of the `network-argument` value (with or without a scheme); the raw value if no hostname parses |
| `detail` | Canonical JSON of the arguments (sorted keys) |
| `intent` | `<server>__<tool> <detail>` |
| `source` | `mcp` (new enum value, also in `openapi.yaml`) |

Nothing in the table comes from the downstream server, and the agent influences only `detail`, `intent` and `network`, through the arguments the gateway then checks.

## 5. Interfaces

### 5.1 Minting: `POST /api/swarm/sessions/{id}/mcp/agents`

Owner-authenticated with the existing Clerk JWT. Body: `{"agentId", "parentId" (empty means the root), "scope" (tool patterns)}`. The request is evaluated as a `spawn` event from the parent, so `lineage.max_depth`, `lineage.duplicate`, `delegation.scope_expansion`, revocation and the step budget apply.

| Status | When |
|---|---|
| 201 | Spawn allowed. Body has `agentId`, `parentId`, `credential`, `endpoint`, `decision` |
| 403 | Spawn refused; detail names the rule |
| 404 | Unknown session, foreign owner, or a parent that is neither the root nor a minted agent |
| 409 | Session policy has no `mcp` section, session closed, or invalid agent ID |

### 5.2 Agent endpoint: `/api/swarm/mcp`

Streamable HTTP in stateless JSON mode. Every request must carry `Authorization: Bearer ssa_…`; anything else gets HTTP 401 before MCP parsing. The endpoint is MCP, not REST, so it is not in `openapi.yaml`.

`tools/list` returns `<server>__<tool>` names (model tool-name rules allow only `[A-Za-z0-9_-]`, so server names are restricted to letters, digits and `-`) the agent could be allowed to call: declared, not on the deny list, inside the allowlist, inside every scope in its delegation chain, and not revoked. Descriptions and input schemas come from the downstream listing; a declared tool the server does not offer is omitted. The listing is a convenience. Each call is still decided on its own.

`tools/call` returns the downstream result unchanged when allowed. A refusal is a normal tool result with `isError: true` and the text `Refused by the ASP gateway (<rule>): <reason>. The tool was not run.` Returning a tool error rather than a protocol error lets the model see why and move on, as it would with any failed tool.

Two refusals happen before the gateway and are not recorded: `mcp.undeclared` (the name does not resolve to a declared tool) and `mcp.arguments_too_large` (canonical arguments over 4,000 characters, refused rather than truncated so argument rules cannot be evaded by padding).

## 6. Flows

### 6.1 Allowed call

```text
agent ──tools/call──► Endpoint ── credential → (session, agent)
                         │
                         ├─ worker thread, session lock held:
                         │     resolve name → policy entry → Event
                         │     ASPGateway.evaluate_and_log → allow
                         │     Sentinel.ingest
                         │     row: provenance engine-observed, body entered, status unknown
                         │     reads untrusted → pin taint to the agent
                         │   lock released
                         ├─ await downstream call_tool (other agents proceed meanwhile)
                         ├─ worker thread, lock held: row status succeeded or failed
                         ▼
agent ◄── downstream result
```

### 6.2 Refused call

The same path stops after evaluation. The row is recorded as `not-started` with `toolBodyExecuted: false`, the pool is never touched, and the agent receives the refusal result.

### 6.3 Untrusted read, then write

`http_get` is declared `reads: untrusted`. When admitted, the gateway marks the span contaminated and the broker pins the taint to the agent, as `/execute` does. The next write by that agent, or by any child minted later, resolves a taint origin and is refused with `trust.contaminated_write`; tools listed in `contaminated-deny` are refused with `trust.contaminated_tool`. Other agents are unaffected.

### 6.4 Revocation

A Sentinel tripwire (for example `shared_state_loop` when several agents write the same resource) revokes the implicated agents and their descendants inside the gateway. The next call from any of them is refused with `tripwire.revoked`, and their `tools/list` becomes empty. A call that was already dispatched when the revocation landed runs to completion. Stateless mode has no channel for `notifications/tools/list_changed`; that waits for phase 2.

### 6.5 Teardown

Deleting a session closes it under its lock. The broker removes its credential digests immediately, so the next request gets 401. The pool signals each server's holder task, waits for the processes to exit, then deletes the workspace. Engine shutdown does the same for every session.

## 7. Enforcement order

The gateway is unchanged; Algorithm 1 order holds. What the proxy contributes is where each input comes from.

| Step | Rule ids | Proxy-provided input |
|---|---|---|
| Admission and lineage | `lineage.unknown`, `lineage.invalid`, `lineage.max_depth`, `lineage.duplicate` | Engine-held lineage per credential; mint is a gateway spawn |
| Revocation | `tripwire.revoked` | Agent from the credential |
| Step budget | `budget.steps` | Agent from the credential |
| Spawn scope | `delegation.scope_expansion` | Scope requested at mint |
| Explicit deny | `tools.deny` | Policy target |
| Default-deny allowlist | `tools.default_deny` | Policy target |
| Delegated scope | `delegation.out_of_scope` | Policy target against minted scopes |
| Argument constraints | `tools.deny_arguments` | Canonical argument JSON |
| Network scope | `network.default_deny` | Hostname from the declared argument |
| Content trust | `trust.contaminated_tool`, `trust.contaminated_write` | Policy `reads` and `write`, taint pinned per agent |
| Write cap | `tools.write_cap` | Policy `write` and `resource` |
| Repeated intent | `semantic.identical_intent` | Derived intent (tool name plus arguments) |

## 8. Concurrency and failure handling

The session lock is a threading `RLock`, and the gateway is synchronous and fast (sub-millisecond per the existing benchmark). MCP handlers are async, so admission and recording run on worker threads via `anyio.to_thread`, and the downstream call runs on the event loop with the lock released. Rate checks count at admission, so concurrent calls cannot jointly exceed a write cap or budget.

| Failure | Behaviour |
|---|---|
| Downstream returns `isError` | Row `failed`, `tool_execution_failed`; result passed to the agent unchanged |
| Downstream raises or is unreachable | Row `failed`; agent gets a fixed message with no internal detail |
| Server process fails to start | Same as unreachable; the next call retries the launch |
| Session deleted mid-call | The call finishes; recording is skipped because the session is closed |
| Two first calls race to launch a server | An asyncio lock in the pool launches it once |

Downstream servers get the MCP SDK's minimal default environment (PATH, HOME and similar), never the engine's own variables, so Clerk or model API keys do not leak into tool processes.

## 8a. Content inspection

When the policy has an `inspection` section, the gateway scans canonical arguments inside the argument-constraint stage (after explicit argument rules) and the proxy scans results before returning them. Arguments can be reported or denied (`inspection.secrets`, `inspection.pii`, `inspection.injection`). Results can be reported, redacted, blocked, or, for injection markers, used to taint the caller as an untrusted read would. Findings record detector names and counts, never the matched text. Detectors are patterns with known false positives and misses; see `content-inspection.md`.

## 9. Security analysis

Trust boundaries: the agent is untrusted, the downstream server is semi-trusted (it runs only what the policy admits, but its output and self-description are not believed), the operator and the policy files are trusted, and the engine is trusted.

| Threat | Mitigation | Residual |
|---|---|---|
| Agent impersonates another agent | Identity only from a hashed, engine-issued credential | The owner can mint any agent in their own session (G7) |
| Agent claims a false parent | Lineage stored at mint, never read from requests | None in proxy mode |
| Agent calls a dangerous tool the server offers | Undeclared tools are never listed or dispatched | Depends on the operator not declaring it |
| Server mislabels its tools as read-only | Server annotations ignored; labels from policy | A wrong policy label is enforced as written |
| Argument smuggling through padding | Over-limit arguments refused, not truncated | Argument rules only catch what their patterns describe (test 17 shows an absolute path slipping past a `../` rule) |
| Injection via fetched content | Taint per span and agent, capability withdrawal | Content is not inspected (G4); cross-agent propagation needs messages through the engine (phase 2) |
| Credential theft | Shown once, stored hashed, killed with the session, bearer over HTTPS in deployment | No expiry shorter than the session yet |
| Agent bypasses the proxy | Out of scope for code; deployment confinement | Unconfined channels are only observable (T7) |
| Engine compromise | None | The engine is one process (G6) |

## 10. Deployment and confinement

The guarantees in section 2 hold for an agent whose only tool route is the endpoint. A deployment that earns that needs three things, planned for phase 3:

1. The agent runs in a container or VM whose only network egress is the engine's MCP endpoint.
2. The agent host is configured to expose MCP tools only, with built-in shell and file tools disabled.
3. Downstream servers run on the engine side, holding any credentials they need; the agent never sees them.

A bypass test suite should then show that direct access to a downstream server, the filesystem or the network fails from inside the agent's environment.

## 11. Testing

Two suites, both under `artifacts/swarm-sentinel/python/tests/`:

`test_mcp_proxy.py` covers the policy schema, the broker in isolation, and an end-to-end path against the bundled notes fixture, including credential handling and session teardown.

`test_mcp_gateway.py` reaches every gateway check through the proxy using the sample server in `tests/mcp_sample/`. The sample, `workspace_server.py`, is a small offline team workspace with eight tools: read, write and delete file, an HTTP fetch, email, tickets, a shell, and a `drop_database` tool that the policy never declares. Its policy, `workspace.asp.json`, uses small limits (depth 2, write cap 3, step budget 10, repeated intent 3 across 3 agents, shared-state tripwire at 3 agents) so each check is reached in a few calls. Every executed body appends to `audit.jsonl` in the session workspace.

Each refusal is proven three ways: the client receives an error result naming the rule, the recorder row is `not-started` with `engine-observed` provenance and `source: mcp`, and the server's audit log is unchanged.

| Test | Check exercised |
|---|---|
| 01 | Missing, forged or Clerk-only credentials get 401 and never reach the gateway |
| 02 | `lineage.max_depth` on a third-level mint |
| 03 | `delegation.scope_expansion`, `lineage.duplicate` |
| 04 | `shared_state_loop` tripwire revokes three agents and a descendant; `tripwire.revoked`; empty tool list |
| 05 | `budget.steps` on the eleventh call |
| 06 | `tools.deny` beats an allowlist wildcard; tool hidden |
| 07 | `tools.default_deny`; tool hidden |
| 08 | `delegation.out_of_scope` and a scope-filtered tool list |
| 09 | `tools.deny_arguments`: traversal, secret files, external mail including a look-alike domain |
| 10 | `network.default_deny` with and without a scheme and for a look-alike host; allowed host recorded |
| 11 | `trust.contaminated_tool` after an untrusted fetch, with the taint origin recorded |
| 12 | `trust.contaminated_write` for the agent and for a child minted after the read; a clean agent still writes |
| 13 | `tools.write_cap` on the fourth write to one resource |
| 14 | `semantic.identical_intent` across three agents; different arguments pass |
| 15 | Undeclared tools hidden and refused without a gateway row |
| 16 | Allowed calls reach the server, rows succeed, Sentinel graph edges match |
| 17 | Server-side failure after admission recorded as `failed` with body entered |

Run from `artifacts/swarm-sentinel/python`:

```bash
../../../.venv/bin/python -m unittest discover tests -p "test_mcp_*.py"
```

The fixture servers print a traceback to stderr for the deliberate failure cases; that output is expected.

## 12. Alternatives considered

From the 2026-10-04 discussion (`source-materials/SwarmSentinel-MCP-Proxy-Discussion-2026-10-04.pdf`), scored out of 100 with taint and swarm-wide state weighted highest:

| Option | Score | Why not, or why |
|---|---|---|
| Gateway inside each MCP server | 48 | Splits taint, counters and revocation per server; servers police themselves; no third-party coverage |
| One proxy, semantics from policy | 78 | Chosen. Single state, any server, check outside the constrained component |
| Proxy plus server-supplied manifests | 84 | Better labels for servers we own, but two layers to keep in sync. Can be added later without redesign |

Within the proxy option, a standalone process calling the engine through `Guard.remote()` was rejected because outcomes would be caller-reported and state split across processes.

## 13. Open questions and future work

- `swarm.send_message` as a proxy-hosted tool, so agent-to-agent messages become in-band events and taint can propagate (T2).
- Stateful MCP sessions so revocation can push `tools/list_changed`.
- Credential expiry and rotation independent of session lifetime.
- Whether argument rules should gain structured matchers (per-argument patterns, path normalisation) instead of regexes over canonical JSON. Test 17 is the motivating case.
- Manifests for servers we own (the "both" option), once a real tool's policy labels prove wrong.
- A full threat model update after confinement exists (phase 4).
