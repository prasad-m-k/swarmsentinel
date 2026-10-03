from collections import defaultdict
from datetime import datetime
import networkx as nx


class Sentinel:
    def __init__(self):
        self.graph = nx.DiGraph()
        self.communication = []
        self.edits = defaultdict(list)
        self.fanout = defaultdict(set)
        self.alerts = []
        self.detected = set()
        self.policy_changes = []

    def ingest(self, trace, gateway):
        """Only executed actions contribute to behavioral graph; every attempt stays in recorder."""
        if not trace.executed:
            return
        now = datetime.fromisoformat(trace.timestamp).timestamp()
        self.graph.add_node(trace.agentId, kind="agent")
        self.graph.add_node(trace.target, kind="agent" if trace.action in {"spawn", "message"} else "resource")
        self.graph.add_edge(trace.agentId, trace.target, action=trace.action)
        candidates = []
        if trace.action == "spawn":
            self.fanout[trace.agentId].add(trace.target)
            if len(self.fanout[trace.agentId]) > 4:
                candidates.append(("fanout", "An agent spawned more than four admitted children", [trace.agentId, *sorted(self.fanout[trace.agentId])]))
        if trace.action == "wiki_edit":
            edits = [(t, a) for t, a in self.edits[trace.target] if now-t <= 10]
            edits.append((now, trace.agentId))
            self.edits[trace.target] = edits
            agents = sorted({a for _, a in edits})
            if len(agents) >= 4:
                candidates.append(("shared_state_loop", f"{len(agents)} agents edited {trace.target} within ten seconds", agents))
        if trace.action == "message":
            self.communication = [(t, a, b) for t, a, b in self.communication if now-t <= 10]
            self.communication.append((now, trace.agentId, trace.target))
            graph = nx.DiGraph()
            graph.add_edges_from((a, b) for _, a, b in self.communication)
            for cluster in nx.strongly_connected_components(graph):
                density = nx.density(graph.subgraph(cluster))
                if len(cluster) >= 3 and density >= .5:
                    candidates.append(("rapid_consensus", f"Reciprocal cluster of {len(cluster)} agents; directed density {density:.2f} in ten seconds", sorted(cluster)))
        for kind, reason, agents in candidates:
            key = (kind, trace.target if kind == "shared_state_loop" else tuple(sorted(agents)))
            if key in self.detected:
                continue
            self.detected.add(key)
            alert = dict(id=f"alert-{len(self.alerts)+1}", timestamp=trace.timestamp,
                         kind=kind, reason=reason, agents=agents, eventId=trace.id)
            self.alerts.append(alert)
            if gateway.settings.feedbackEnabled:
                gateway.revoke(agents)
                self.policy_changes.append(dict(
                    timestamp=trace.timestamp, version=gateway.version,
                    blockedAgents=sorted(gateway.revoked), reason=reason,
                ))