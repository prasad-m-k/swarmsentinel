from collections import defaultdict, deque
from datetime import datetime
from asp_policy import argument_violation, host_allowed, tool_matches
from models import Trace, Violation

DIRECTIVES = {
    "tools.deny": "tools.deny", "tools.default_deny": "tools.allow", "tools.deny_arguments": "tools.deny-arguments",
    "network.default_deny": "network.allow", "trust.contaminated_write": "content-trust",
    "trust.contaminated_tool": "content-trust.contaminated-deny",
    "delegation.out_of_scope": "delegation.sub-agents", "delegation.scope_expansion": "delegation.sub-agents",
    "tools.write_cap": "swarm.write-cap", "semantic.identical_intent": "swarm.repeated-intent",
    "lineage.unknown": "delegation", "lineage.invalid": "delegation", "lineage.max_depth": "delegation.max-depth",
    "lineage.duplicate": "delegation", "tripwire.revoked": "swarm.tripwires", "budget.steps": "swarm.step-budget",
}


class ASPGateway:
    """Deterministic interception (ASP Algorithm 1) over normalized agent actions.

    In "enforce" mode a non-allow decision means the call never executed. In "report-only"
    mode (historical replay) every action already happened; decisions record what the policy
    would have done, and rate/intent budgets only count calls the policy would have allowed.
    """

    def __init__(self, settings, policy, mode="enforce", registry=None, root="orchestrator", root_is_actor=True):
        self.settings = settings
        self.policy = policy
        self.mode = mode
        self.root = root
        self.protected = set() if root_is_actor else {root}
        self.agents = {root: ("", 0, "")}
        self.agents.update(registry or {})
        self.revoked = set()
        self.version = 1
        self.writes = defaultdict(list)
        self.intents = defaultdict(list)
        # Biba-style taint: span -> origin event id, agent -> origin event id (via messages),
        # and origin event id -> every agent that origin has reached.
        self.contaminated = {}
        self.tainted = {}
        self.taint_groups = defaultdict(set)
        # Inherit-and-restrict delegation: agent -> tool patterns granted at spawn.
        self.scopes = {}
        # Per-agent step budget: timestamps of in-band attempts inside the window.
        self.steps = defaultdict(deque)
        self.telemetry = []

    @property
    def window(self):
        return self.policy.swarm.window_seconds

    def revoke(self, agents):
        self.revoked.update(set(agents) - self.protected)
        self.version += 1

    def is_revoked(self, agent):
        while agent:
            if agent in self.revoked:
                return True
            agent = self.agents.get(agent, ("", 0, ""))[0]
        return False

    def taint_origin(self, event):
        """Event id of the untrusted read this context descends from, or "" if clean."""
        origin = self.contaminated.get(event.spanId)
        actor = event.agentId
        while not origin and actor:
            origin = self.tainted.get(actor, "")
            actor = self.agents.get(actor, ("", 0, ""))[0]
        return origin or ""

    def _scope_chain(self, agent):
        while agent:
            if agent in self.scopes:
                yield agent, self.scopes[agent]
            agent = self.agents.get(agent, ("", 0, ""))[0]

    def _over_budget(self, agent, now):
        """Count every in-band attempt, blocked or not, so a retry loop stays throttled until it backs off."""
        limit = self.policy.swarm.step_budget
        if not limit:
            return False
        attempts = self.steps[agent]
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        attempts.append(now)
        return len(attempts) > limit

    def _rate_checks(self, event, now, key, is_write, write_key):
        window, cap = self.window, self.policy.swarm.write_cap
        repeated = self.policy.swarm.repeated_intent
        self.intents[key] = [(t, a) for t, a in self.intents[key] if now-t <= window]
        if is_write:
            self.writes[write_key] = [t for t in self.writes[write_key] if now-t <= window]
        if is_write and len(self.writes[write_key]) >= cap:
            return "drop", "tools.write_cap", f"Write budget {cap}/{window} seconds reached for {write_key}"
        if len(self.intents[key]) >= repeated.limit-1 and len({a for _, a in self.intents[key]} | {event.agentId}) >= repeated.min_agents:
            return "throttle", "semantic.identical_intent", f"Repeated normalized intent and target across at least {repeated.min_agents} agent IDs; call not executed"
        self.intents[key].append((now, event.agentId))
        if is_write:
            self.writes[write_key].append(now)
        return None

    def _tool_checks(self, event, now, advisories):
        tools, network = self.policy.tools, self.policy.network
        if tool_matches(event.target, tools.deny):
            return "drop", "tools.deny", "Tool is on the explicit deny list"
        if tools.default == "deny" and not tool_matches(event.target, tools.allow):
            return "drop", "tools.default_deny", "Tool is outside the explicit allowlist"
        for holder, scope in self._scope_chain(event.agentId):
            if not tool_matches(event.target, scope):
                whose = "its delegated scope" if holder == event.agentId else f"the scope delegated to ancestor {holder}"
                return "drop", "delegation.out_of_scope", f"Tool is outside {whose} ({', '.join(scope)})"
        label = argument_violation(event.target, event.detail, tools.deny_arguments)
        if label:
            return "drop", "tools.deny_arguments", f"Argument constraint: {label}"
        if not host_allowed(event.network, network):
            return "drop", "network.default_deny", f"Network destination {event.network} is outside the allowlist"
        is_write = event.write if event.write is not None else event.target.endswith(".write")
        origin = self.taint_origin(event)
        trust = self.policy.content_trust
        if origin and tool_matches(event.target, trust.contaminated_deny):
            return "drop", "trust.contaminated_tool", f"Capability withdrawn: context is contaminated by untrusted content (origin {origin})"
        if is_write and origin:
            if trust.contaminated_write == "deny":
                return "drop", "trust.contaminated_write", f"Write from a context contaminated by untrusted content (origin {origin})"
            advisories.append(Violation(directive="content-trust", attempted_action=event.target,
                                        detail=f"write from a context contaminated by untrusted content (origin {origin})", enforced=False))
        key = (" ".join(event.intent.lower().split()), event.target)
        return self._rate_checks(event, now, key, is_write, event.resource or event.target)

    def evaluate_and_log(self, event):
        now = datetime.fromisoformat(event.timestamp).timestamp()
        decision, rule, reason = "allow", "policy.allow", "Allowed by ASP policy"
        advisories = []
        known = event.agentId in self.agents
        parent, depth, parent_span = self.agents.get(event.agentId, ("", 0, ""))
        proposed_depth = depth + (event.action == "spawn")
        if event.channel == "out_of_band":
            decision, rule, reason = "observed", "monitor.out_of_band", "Observed activity outside the gateway; ASP cannot block or undo this path"
        elif not known:
            decision, rule, reason = "drop", "lineage.unknown", "Actor was not admitted by an allowed spawn or registry entry"
        elif event.parentId != parent or event.parentSpanId != parent_span:
            decision, rule, reason = "drop", "lineage.invalid", "Declared lineage does not match gateway registry"
        elif proposed_depth > self.policy.delegation.max_depth:
            decision, rule, reason = "drop", "lineage.max_depth", f"Hop depth {proposed_depth} exceeds cap {self.policy.delegation.max_depth}"
        elif self.is_revoked(event.agentId):
            decision, rule, reason = "drop", "tripwire.revoked", "Sentinel revoked this actor or an ancestor; downstream call not executed"
        elif self._over_budget(event.agentId, now):
            decision, rule, reason = "throttle", "budget.steps", f"Step budget of {self.policy.swarm.step_budget} actions per {self.window} seconds exhausted for this agent"
        elif event.action == "spawn" and event.target in self.agents:
            decision, rule, reason = "drop", "lineage.duplicate", "Agent ID already registered"
        elif event.action == "spawn" and (wider := [p for p in event.scope for _, scope in self._scope_chain(event.agentId) if not tool_matches(p, scope)]):
            decision, rule, reason = "drop", "delegation.scope_expansion", f"Sub-agent scope would exceed the delegator's: {', '.join(sorted(set(wider)))}"
        elif event.action == "tool":
            decision, rule, reason = self._tool_checks(event, now, advisories) or (decision, rule, reason)
        elif event.action == "message" and "message" in self.policy.swarm.repeated_intent.actions:
            key = (" ".join(event.intent.lower().split()), "chat")
            decision, rule, reason = self._rate_checks(event, now, key, False, "") or (decision, rule, reason)
        if event.action == "spawn" and decision == "allow":
            self.agents[event.target] = (event.agentId, proposed_depth, event.spanId)
            if event.scope:
                self.scopes[event.target] = list(event.scope)
        executed = self.mode == "report-only" or decision in {"allow", "observed"}
        if executed and event.reads == "untrusted" and not self.taint_origin(event):
            self.contaminated[event.spanId] = event.id
            self.taint_groups[event.id].add(event.agentId)
        origin = self.taint_origin(event)
        if executed and origin and event.action == "message" and event.channel == "in_band" and self.policy.content_trust.propagate:
            for recipient in event.mentions or [event.target]:
                if recipient in self.agents and recipient not in self.protected and recipient not in self.tainted:
                    self.tainted[recipient] = origin
                    self.taint_groups[origin].add(recipient)
        violations = advisories
        if decision in {"drop", "throttle"}:
            violations = [Violation(directive=DIRECTIVES.get(rule, rule), attempted_action=event.target,
                                    detail=reason, enforced=self.mode == "enforce"), *advisories]
        if self.mode == "report-only" and decision in {"drop", "throttle"}:
            reason = f"Would {decision}: {reason}"
        trace = Trace(**{
            **event.model_dump(), "depth": proposed_depth,
            "parentId": parent if known else event.parentId,
            "parentSpanId": parent_span if known else event.parentSpanId,
            "decision": decision, "rule": rule, "reason": reason,
            "executed": executed, "policyVersion": self.version, "mode": self.mode,
            "violations": violations, "contaminated": bool(origin), "taintOrigin": origin,
        })
        self.telemetry.append(trace)
        return trace
