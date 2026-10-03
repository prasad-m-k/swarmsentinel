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

Status key: **Enforced** means calls are blocked before execution in the synthetic scenarios. **Report-only** means real AI Village records are scored counterfactually; those actions had already happened. **Gap** means not covered.

| # | Threat vector | Controls (rule ids) | Evidence | Status |
|---|---|---|---|---|
| T1 | Prompt injection via retrieved content, single agent | Content-trust contamination of the reading span; `trust.contaminated_tool` withdraws listed capabilities (`contaminated-deny`); `trust.contaminated_write` | `injection` scenario, events 005 and 010; `InjectionDefense` tests | Enforced (synthetic). Report-only on AI Village: 44,430 of 101,332 shared-state writes came from contaminated sessions |
| T2 | Injection propagation across agents | Taint propagation through messages (`propagate-via-messages`), carrying the origin event id; `injection_spread` tripwire plus revocation | `injection` scenario, events 008 to 013: the taint reaches three agents, the tripwire fires, the compromised branch is revoked, the clean orchestrator keeps working | Enforced (synthetic). Off for AI Village; see gap G2 |
| T3 | Confused deputy and privilege escalation through delegation | Inherit-and-restrict scopes on spawn: `delegation.out_of_scope`, `delegation.scope_expansion`; lineage admission (`lineage.unknown`, `lineage.invalid`); depth cap (`lineage.max_depth`) | `injection` events 006 and 007; one `lineage.max_depth` drop in the `attack` scenario | Enforced (synthetic). Not exercised on AI Village (no sub-agent spawns in the export) |
| T4 | Tool-use policy violations | Explicit deny list before default-deny allowlist (`tools.deny`, `tools.default_deny`); argument constraints (`tools.deny_arguments`); network scope (`network.default_deny`) | `PolicyEvaluation` unit tests (deny before allow, argument rule, network scope); AI Village: 97,472 would-block decisions, including 5,512 argument-rule hits | Enforced in unit tests, report-only (real). No synthetic scenario walks through it yet |
| T5 | Credential and secret exposure | Argument constraints for credential stores and environment secrets; ingest scrubs emails, phone numbers and token-shaped strings | AI Village: 4,681 credential-store reads across 22 agents, 384 environment secret disclosures | Report-only |
| T6 | Runaway execution and resource exhaustion | Depth cap; `fanout` tripwire; write cap per shared resource (`tools.write_cap`); repeated-intent throttle (`semantic.identical_intent`); `rapid_consensus`, `echo_cascade` and `shared_state_loop` tripwires; revocation of descendants | `attack` scenario; AI Village: 996 tripwire alerts over 389 days, 4,010 write-cap and 1,744 repeated-intent decisions | Enforced (synthetic), report-only (real). See gap G3 |
| T7 | Activity outside the gateway | Out-of-band events are recorded as `observed` and still feed tripwires | `normal` and `attack` scenarios (wiki edits, side-channel messages) | Detected only; cannot be blocked or undone |
| T8 | Tampering with the enforcement point | None | | Gap: gateway and recorder share one process |

## Known gaps

- **G1. No live interception yet.** The engine evaluates recorded event streams through `POST /api/swarm/simulate`. A per-call `evaluate` endpoint, an agent-side wrapper and latency measurements are the next phase. Until then, "enforced" means enforced within the simulated execution, not in front of a running agent.
- **G2. Taint propagation is off for AI Village.** Village agents share broadcast chat rooms, so propagating through messages would mark nearly every agent as tainted within minutes and carry no signal. Real deployments with directed agent-to-agent channels are the intended setting.
- **G3. No explicit delegation-cycle or step-budget control.** Recursive delegation is bounded by the depth cap and duplicate-ID check, and conversational loops by the consensus and echo tripwires, but a task bouncing A to B to A, or one agent running an unbounded number of steps, has no dedicated rule. There is no token or cost budget.
- **G4. Contamination is assigned, not detected.** The adapter labels reads as untrusted by source (web fetches, retrieved files). SwarmSentinel does not inspect content for injection text; it limits what a context can do once it has read untrusted content, which is the ASP paper's approach.
- **G5. Taint travels only through messages.** Writing to shared state (a repository, a wiki) and having another agent read it back is not yet a propagation path.
- **G6. Not a tamper-proof boundary.** The paper's reference-monitor properties (tamper-proof, always-invoked, verifiable) need the gateway in a separate process or service the agent cannot reach. The prototype separates them in code only.

## Reading the evidence

Synthetic results show the controls working end to end on designed scenarios. AI Village results show what the same policy family would have flagged on 1.23M real agent actions; they are counterfactual and say nothing about whether those actions caused harm. Never present AI Village numbers as prevented incidents.
