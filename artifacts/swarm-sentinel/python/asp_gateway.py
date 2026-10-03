from collections import defaultdict
from datetime import datetime
from models import Trace


class ASPGateway:
    """Independent deterministic policy engine for mock tools only."""
    ALLOWED_TOOLS = {"mock:search.read", "mock:mcp:wiki.write", "mock:mcp:board.write"}

    def __init__(self, settings):
        self.settings = settings
        self.agents = {"orchestrator": ("", 0, "")}
        self.revoked = set()
        self.version = 1
        self.writes = defaultdict(list)
        self.intents = defaultdict(list)
        self.telemetry = []

    def revoke(self, agents):
        self.revoked.update(agents)
        self.version += 1

    def is_revoked(self, agent):
        while agent:
            if agent in self.revoked:
                return True
            agent = self.agents.get(agent, ("", 0, ""))[0]
        return False

    def evaluate_and_log(self, event):
        now = datetime.fromisoformat(event.timestamp).timestamp()
        decision, rule, reason = "allow", "policy.allow", "Allowed by mock tool policy"
        known = event.agentId in self.agents
        parent, depth, parent_span = self.agents.get(event.agentId, ("", 0, ""))
        proposed_depth = depth + (event.action == "spawn")
        if event.channel == "out_of_band":
            decision, rule, reason = "observed", "monitor.out_of_band", "Observed shared-state activity; ASP cannot undo or block this external path"
        elif not known:
            decision, rule, reason = "drop", "lineage.unknown", "Actor was not admitted by an allowed spawn"
        elif event.parentId != parent or event.parentSpanId != parent_span:
            decision, rule, reason = "drop", "lineage.invalid", "Declared lineage does not match gateway registry"
        elif proposed_depth > self.settings.maxDepth:
            decision, rule, reason = "drop", "lineage.max_depth", f"Hop depth {proposed_depth} exceeds cap {self.settings.maxDepth}"
        elif self.is_revoked(event.agentId):
            decision, rule, reason = "drop", "tripwire.revoked", "Sentinel revoked this actor or an ancestor; downstream mock call not executed"
        elif event.action == "spawn" and event.target in self.agents:
            decision, rule, reason = "drop", "lineage.duplicate", "Agent ID already registered"
        elif event.action == "tool":
            key = (" ".join(event.intent.lower().split()), event.target)
            self.intents[key] = [(t, a) for t, a in self.intents[key] if now-t <= 10]
            self.writes[event.target] = [t for t in self.writes[event.target] if now-t <= 10]
            is_write = event.target.endswith(".write")
            if event.target not in self.ALLOWED_TOOLS:
                decision, rule, reason = "drop", "tools.default_deny", "Tool is outside the explicit mock allowlist"
            elif is_write and len(self.writes[event.target]) >= self.settings.writeLimit:
                decision, rule, reason = "drop", "tools.write_cap", f"Write budget {self.settings.writeLimit}/10 seconds reached for this MCP target"
            elif len(self.intents[key]) >= self.settings.semanticLimit-1 and len({a for _, a in self.intents[key]} | {event.agentId}) >= 3:
                decision, rule, reason = "throttle", "semantic.identical_intent", "Repeated normalized intent and tool target across at least three agent IDs; call not executed"
            else:
                self.intents[key].append((now, event.agentId))
                if is_write:
                    self.writes[event.target].append(now)
        if event.action == "spawn" and decision == "allow":
            self.agents[event.target] = (event.agentId, proposed_depth, event.spanId)
        trace = Trace(**{
            **event.model_dump(), "depth": proposed_depth,
            "parentId": parent if known else event.parentId,
            "parentSpanId": parent_span if known else event.parentSpanId,
            "decision": decision, "rule": rule, "reason": reason,
            "executed": decision in {"allow", "observed"}, "policyVersion": self.version,
        })
        self.telemetry.append(trace)
        return trace