# SwarmSentinel threat model

What SwarmSentinel defends against, how, with what evidence, and where it stops. Every control named here is a rule in the gateway (`asp_gateway.py`) or a tripwire in the Sentinel (`sentinel.py`), configured from the ASP policy files in `artifacts/swarm-sentinel/python/policies/`.

## System model

- **Agents** are LLM-driven and untrusted. Their reasoning can be steered by anything in their context window, so their own narration is never evidence.
- **The ASP gateway** sits between agents and tools. It sees every in-band call (tool use, spawn, message) before execution and decides from policy, identity and lineage, never from the model's reasoning. This follows the ASP preprint's Algorithm 1.
- **The Sentinel** watches executed interactions as a weighted directed graph and raises tripwires on swarm-level patterns. With feedback enabled, a tripwire revokes the implicated agents and their descendants.
- **Content sources** carry trust levels from the policy (`content-trust`): system prompt high, user input medium, tool responses low, retrieved documents untrusted.

## Adversaries and failure modes

1. **Indirect prompt injection**: adversarial instructions inside content an agent retrieves (a document, web page or tool output).
2. **A compromised or misaligned agent** that tries to exceed its delegated authority or recruit other agents.
3. **Emergent swarm failure** with no adversary at all: recursive delegation, feedback loops between agents, write storms on shared state.

## Coverage

Status key: **Enforced** means calls are blocked before execution, in the synthetic scenarios and in live sessions through the guard wrapper (`sdk/guard.py`; the live demo replays T1 to T3 with real Python tools). **Report-only** means real AI Village records are scored counterfactually; those actions had already happened. **Gap** means not covered.

| # | Threat vector | Controls (rule ids) | Evidence | Status |
|---|---|---|---|---|
| T1 | Prompt injection via retrieved content, single agent | Content-trust contamination of the reading span; `trust.contaminated_tool` withdraws listed capabilities (`contaminated-deny`); `trust.contaminated_write` | `injection` scenario, events 005 and 010; `InjectionDefense` tests | Enforced (synthetic). Report-only on AI Village: 44,430 of 101,332 shared-state writes came from contaminated sessions |
| T2 | Injection propagation across agents | Taint propagation through messages (`propagate-via-messages`), carrying the origin event id; `injection_spread` tripwire plus revocation | `injection` scenario, events 008 to 013: the taint reaches three agents, the tripwire fires, the compromised branch is revoked, the clean orchestrator keeps working | Enforced (synthetic). Off for AI Village; see gap G2 |
| T3 | Confused deputy and privilege escalation through delegation | Inherit-and-restrict scopes on spawn: `delegation.out_of_scope`, `delegation.scope_expansion`; lineage admission (`lineage.unknown`, `lineage.invalid`); depth cap (`lineage.max_depth`) | `injection` events 006 and 007; one `lineage.max_depth` drop in the `attack` scenario | Enforced (synthetic). Not exercised on AI Village (no sub-agent spawns in the export) |
| T4 | Tool-use policy violations | Explicit deny list before default-deny allowlist (`tools.deny`, `tools.default_deny`); argument constraints (`tools.deny_arguments`); network scope (`network.default_deny`) | `PolicyEvaluation` unit tests (deny before allow, argument rule, network scope); AI Village: 97,472 would-block decisions, including 5,512 argument-rule hits | Enforced in unit tests, report-only (real). No synthetic scenario walks through it yet |
| T5 | Credential and secret exposure | Argument constraints for credential stores and environment secrets; ingest scrubs emails, phone numbers and token-shaped strings | AI Village: 4,681 credential-store reads across 22 agents, 384 environment secret disclosures | Report-only |
| T6 | Runaway execution and resource exhaustion | Depth cap; `fanout` tripwire; per-agent step budget (`budget.steps`); `delegation_loop` tripwire on hand-off chains; write cap per shared resource (`tools.write_cap`); repeated-intent throttle (`semantic.identical_intent`); `rapid_consensus`, `echo_cascade` and `shared_state_loop` tripwires; revocation of descendants | `attack` and `runaway` scenarios (`LoopsAndBudgets` tests): a planner, executor and critic hand-off loop is detected when it closes and revoked; a scraper's retry storm is throttled after 20 attempts in 10 seconds. AI Village: 996 tripwire alerts over 389 days, 4,010 write-cap and 1,744 repeated-intent decisions | Enforced (synthetic and live). Report-only on AI Village, where the step budget and loop tripwire are off. See gap G3 |
| T7 | Activity outside the gateway | Out-of-band events are recorded as `observed` and still feed tripwires | `normal` and `attack` scenarios (wiki edits, side-channel messages) | Detected only; cannot be blocked or undone |
| T8 | Tampering with the enforcement point | Remote guard mode runs the gateway in the engine process, separate from agents | Live sessions over HTTP (`Guard.remote`) | Partial: no API authentication; in-process mode (`Guard.local`) shares the agent's process. See G6 |

## Known gaps

- **G1. Interception is cooperative.** Live sessions (`POST /api/swarm/sessions/{id}/evaluate`) decide each action before the agent runs it, and the guard wrapper refuses to run a denied call. Measured overhead on an Apple M2 for allowed traffic from 20 agents paced at 10 actions per second: p50 0.024 ms in-process, 0.39 ms over localhost HTTP (p99 0.044 ms and 0.58 ms; `examples/bench_latency.py`). But an agent that bypasses the wrapper and calls a tool directly is not stopped. Closing that needs the gateway in the tool path itself (an MCP or HTTP proxy in front of the tools), which is not built.
- **G2. Taint propagation is off for AI Village.** Village agents share broadcast chat rooms, so propagating through messages would mark nearly every agent as tainted within minutes and carry no signal. Real deployments with directed agent-to-agent channels are the intended setting.
- **G3. Loop and budget controls are coarse.** The `delegation_loop` tripwire follows hand-off chains through messages and assumes an agent's next message continues the chain it last received; it can miss loops that pass through shared state, and it may flag legitimate iterative review. A 3-agent loop also trips `rapid_consensus` on the same event. The step budget counts actions, not tokens or cost, and there is no global budget across a swarm. Both are off for AI Village, where broadcast chat makes hand-off chains ambiguous.
- **G4. Contamination is assigned, not detected.** The adapter labels reads as untrusted by source (web fetches, retrieved files). SwarmSentinel does not inspect content for injection text; it limits what a context can do once it has read untrusted content, which is the ASP paper's approach.
- **G5. Taint travels only through messages.** Writing to shared state (a repository, a wiki) and having another agent read it back is not yet a propagation path.
- **G6. Not yet a tamper-proof boundary.** The paper's reference-monitor properties (tamper-proof, always-invoked, verifiable) need the gateway in a process or service the agent cannot reach or reconfigure. In remote mode the gateway does run in the engine process, separate from the agents, so an agent cannot alter its state directly. But the session API has no authentication: any caller that can reach it can end a session or open a new one and choose its policy. In-process mode (`Guard.local`) runs the gateway inside the agent's own process. Closing this needs authenticated sessions with server-side policy assignment, and is always-invoked only once tools are reachable solely through the gateway (G1).

## Reading the evidence

Synthetic results show the controls working end to end on designed scenarios. AI Village results show what the same policy family would have flagged on 1.23M real agent actions; they are counterfactual and say nothing about whether those actions caused harm. Never present AI Village numbers as prevented incidents.
