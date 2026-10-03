from collections import defaultdict
from datetime import datetime
from asp_policy import argument_violation, host_allowed, tool_matches
from models import Trace, Violation

DIRECTIVES = {
    "tools.deny": "tools.deny", "tools.default_deny": "tools.allow", "tools.deny_arguments": "tools.deny-arguments",
    "network.default_deny": "network.allow", "trust.contaminated_write": "content-trust",
    "tools.write_cap": "swarm.write-cap", "semantic.identical_intent": "swarm.repeated-intent",
    "lineage.unknown": "delegation", "lineage.invalid": "delegation", "lineage.max_depth": "delegation.max-depth",
    "lineage.duplicate": "delegation", "tripwire.revoked": "swarm.tripwires",
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
        self.contaminated = set()
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
        label = argument_violation(event.target, event.detail, tools.deny_arguments)
        if label:
            return "drop", "tools.deny_arguments", f"Argument constraint: {label}"
        if not host_allowed(event.network, network):
            return "drop", "network.default_deny", f"Network destination {event.network} is outside the allowlist"
        is_write = event.write if event.write is not None else event.target.endswith(".write")
        if is_write and event.spanId in self.contaminated:
            if self.policy.content_trust.contaminated_write == "deny":
                return "drop", "trust.contaminated_write", "Write from a context that read untrusted content"
            advisories.append(Violation(directive="content-trust", attempted_action=event.target,
                                        detail="write after reading untrusted content in this span", enforced=False))
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
        elif event.action == "spawn" and event.target in self.agents:
            decision, rule, reason = "drop", "lineage.duplicate", "Agent ID already registered"
        elif event.action == "tool":
            decision, rule, reason = self._tool_checks(event, now, advisories) or (decision, rule, reason)
        elif event.action == "message" and "message" in self.policy.swarm.repeated_intent.actions:
            key = (" ".join(event.intent.lower().split()), "chat")
            decision, rule, reason = self._rate_checks(event, now, key, False, "") or (decision, rule, reason)
        if event.action == "spawn" and decision == "allow":
            self.agents[event.target] = (event.agentId, proposed_depth, event.spanId)
        executed = self.mode == "report-only" or decision in {"allow", "observed"}
        if executed and event.reads == "untrusted":
            self.contaminated.add(event.spanId)
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
            "violations": violations, "contaminated": event.spanId in self.contaminated,
        })
        self.telemetry.append(trace)
        return trace
