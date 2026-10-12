# Egress monitor link: confidentiality on the model-API path

SwarmSentinel mediates what agents do through tools. It does not see what agents send to model providers, and a confined agent still needs that route to function. A separate egress monitor sits on the model-API path; SwarmSheriff is one, a loopback reverse proxy that classifies outbound LLM requests against company data classes. This page describes how the two share one view of each agent.

## The mechanism

1. **Labels from tool calls.** An ASP policy may declare a `confidentiality` section. Each class lists tool globs and regular expressions over a call's canonical arguments. An executed tool call that matches adds the class to the acting context: the agent, or the (agent, task) pair under `content-trust.taint-scope: task`. Labels only accumulate (a Bell-LaPadula high-water mark), are inherited by children spawned from a labelled context and, with `propagate-via-messages` (default true), follow messages. Recorder rows carry the labels the context held after the action (`labels`, as `asp:<class>`).

   ```json
   "confidentiality": {
     "classes": {
       "credentials": { "arguments": ["(~|\\$HOME|/home/\\w+)/\\.(ssh|netrc|aws|config/gh)\\b"] },
       "customer-data": { "tools": ["mcp:crm:*"] }
     },
     "propagate-via-messages": false
   }
   ```

2. **Identity from the engine.** An agent's model requests carry its engine-issued MCP credential (`X-Swarmsheriff-Credential` for SwarmSheriff). The monitor resolves it with `POST /api/swarm/egress/resolve` and receives the agent, its current task, its labels, whether it is tainted and whether it is revoked. The monitor adds the labels to its own deterministic first step (source labels), so a class such as `credentials` with `source_labels: ["asp:credentials"]` blocks the request whatever its text says. A revoked agent's model requests are refused, so revocation reaches the model path too. An unverifiable credential fails closed.

3. **Decisions back to the recorder.** The monitor posts each decision for an engine-identified agent to `POST /api/swarm/egress/events`. The engine records it as an observed, out-of-band row (`source: egress`, `executionProvenance: egress-monitor-reported`) with the classes and rules as a violation, so one timeline shows both paths. Rows carry labels and counts only.

Opening a new task (`POST /api/swarm/sessions/{id}/mcp/agents/{agentId}/task`) starts with no labels; tasks are opened only by the owner, never the agent.

## Configuration

| Side | Setting |
|---|---|
| Engine | `SWARM_EGRESS_SECRET` enables both endpoints (they answer 404 without it). Callers must send it in `X-Egress-Secret`. Loopback callers only unless `SWARM_EGRESS_REMOTE=true`. |
| SwarmSheriff | `-sentinel http://127.0.0.1:8000 -sentinel-secret-file <file>`; `-require-credential` refuses requests without an engine credential. |

## Evidence

- `tests/test_egress.py`: labels from arguments and tools, refused calls do not label, inheritance and message propagation, task scope, endpoint authentication, recorder rows, durability.
- SwarmSheriff `sentinel_test.go`: engine labels block a request, clean agents are forwarded without the credential header, unknown, unreachable and revoked fail closed, required credentials, content-free events.
- End-to-end with both processes: ten steps, all as designed. An AI Village counterfactual replay measured how often task-scoped labels would hold model requests.

## Limits

- Only requests sent through the monitor are seen; confinement must force model traffic through it.
- Labels say what a context has touched, not what a given request contains. A labelled context may send a harmless request; holding it is the cost of not inspecting content.
- Labels from argument patterns are as good as the patterns.
- The monitor sees a credential in memory while resolving it; it never logs or stores it.
