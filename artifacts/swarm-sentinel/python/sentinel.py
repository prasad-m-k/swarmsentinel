import re
from collections import Counter, defaultdict, deque
from datetime import datetime
import networkx as nx

TOKEN = re.compile(r"[a-z0-9']{3,}")


def _tokens(text):
    return frozenset(TOKEN.findall(text.lower()))


def _jaccard(a, b):
    return len(a & b) / len(a | b) if a and b else 0.0


class Sentinel:
    def __init__(self, policy):
        self.window = policy.swarm.window_seconds
        self.limits = policy.swarm.tripwires
        self.graph = nx.DiGraph()
        # Windowed communication graph: edge weight = messages from a to b still inside the window.
        self.comm_graph = nx.DiGraph()
        self.communication = deque()
        self.chat = []
        self.edits = defaultdict(list)
        self.fanout = defaultdict(set)
        self.alerts = []
        self.detected = set()
        self.recent = defaultdict(list)
        self.policy_changes = []

    def _link(self, a, b, delta):
        """Adjust one windowed communication edge; drop it (and orphaned agents) when its weight reaches zero."""
        weight = self.comm_graph.get_edge_data(a, b, {}).get("weight", 0) + delta
        if weight > 0:
            self.comm_graph.add_edge(a, b, weight=weight)
            return
        self.comm_graph.remove_edge(a, b)
        self.comm_graph.remove_nodes_from([n for n in (a, b) if self.comm_graph.degree(n) == 0])

    def _consensus(self, now, trace):
        while self.communication and now - self.communication[0][0] > self.window:
            _, a, b = self.communication.popleft()
            self._link(a, b, -1)
        for b in trace.mentions or [trace.target]:
            if b != trace.agentId:
                self.communication.append((now, trace.agentId, b))
                self._link(trace.agentId, b, +1)
        graph = self.comm_graph
        for cluster in nx.strongly_connected_components(graph):
            if len(cluster) < self.limits.consensus_min_cluster:
                continue
            # Density counts distinct directed pairs; weight records how often each pair spoke.
            density = nx.density(graph.subgraph(cluster))
            if density >= self.limits.consensus_density:
                yield ("rapid_consensus", f"Reciprocal cluster of {len(cluster)} agents; directed density {density:.2f} within {self.window} seconds", sorted(cluster), tuple(sorted(cluster)))

    def _echo(self, now, trace):
        tokens = _tokens(trace.intent)
        self.chat = [m for m in self.chat if now-m[0] <= self.window]
        similar = [m for m in self.chat if m[2] != trace.agentId and _jaccard(tokens, m[3]) >= self.limits.echo_similarity]
        self.chat.append((now, trace.id, trace.agentId, tokens))
        agents = sorted({m[2] for m in similar} | {trace.agentId})
        if self.limits.echo_agents and len(agents) >= self.limits.echo_agents:
            origin = min(similar)[1]
            yield ("echo_cascade", f"{len(agents)} agents posted near-duplicate messages (token overlap >= {self.limits.echo_similarity:.0%}) within {self.window} seconds", agents, origin)

    def _overlaps_recent(self, kind, agents, now):
        """A drifting cluster (one agent joins or leaves) is the same episode, not a new detection."""
        self.recent[kind] = [(t, s) for t, s in self.recent[kind] if now-t <= self.window]
        return any(_jaccard(frozenset(agents), frozenset(s)) >= .5 for _, s in self.recent[kind])

    def ingest(self, trace, gateway):
        """Only executed actions contribute to the behavioral graph; every attempt stays in the recorder."""
        if not trace.executed:
            return
        now = datetime.fromisoformat(trace.timestamp).timestamp()
        self.graph.add_node(trace.agentId, kind="agent")
        self.graph.add_node(trace.target, kind="agent" if trace.action in {"spawn", "message"} else "resource")
        if self.graph.has_edge(trace.agentId, trace.target):
            edge = self.graph.edges[trace.agentId, trace.target]
            edge["weight"] += 1
            edge["actions"][trace.action] += 1
            edge["last"] = trace.timestamp
        else:
            self.graph.add_edge(trace.agentId, trace.target, weight=1, actions=Counter({trace.action: 1}),
                                first=trace.timestamp, last=trace.timestamp)
        candidates = []
        if trace.action == "spawn":
            self.fanout[trace.agentId].add(trace.target)
            if len(self.fanout[trace.agentId]) > self.limits.fanout_children:
                agents = [trace.agentId, *sorted(self.fanout[trace.agentId])]
                candidates.append(("fanout", f"An agent spawned more than {self.limits.fanout_children} admitted children", agents, tuple(sorted(agents))))
        shared = trace.target if trace.action == "wiki_edit" else trace.resource if trace.write else ""
        if shared:
            edits = [(t, a) for t, a in self.edits[shared] if now-t <= self.window]
            edits.append((now, trace.agentId))
            self.edits[shared] = edits
            agents = sorted({a for _, a in edits})
            if len(agents) >= self.limits.shared_state_agents:
                candidates.append(("shared_state_loop", f"{len(agents)} agents wrote {shared} within {self.window} seconds", agents, shared))
        if trace.action == "message":
            candidates.extend(self._consensus(now, trace))
            candidates.extend(self._echo(now, trace))
        for kind, reason, agents, scope in candidates:
            key = (kind, scope)
            if key in self.detected or self._overlaps_recent(kind, agents, now):
                continue
            self.detected.add(key)
            self.recent[kind].append((now, set(agents)))
            alert = dict(id=f"alert-{len(self.alerts)+1}", timestamp=trace.timestamp,
                         kind=kind, reason=reason, agents=agents, eventId=trace.id)
            self.alerts.append(alert)
            if gateway.settings.feedbackEnabled:
                gateway.revoke(agents)
                self.policy_changes.append(dict(
                    timestamp=trace.timestamp, version=gateway.version,
                    blockedAgents=sorted(gateway.revoked), reason=reason,
                ))

    def weighted_edges(self):
        """Executed interactions collapsed to one edge per directed pair, heaviest first."""
        edges = [dict(source=a, target=b, weight=d["weight"], actions=dict(d["actions"]), first=d["first"], last=d["last"])
                 for a, b, d in self.graph.edges(data=True)]
        return sorted(edges, key=lambda e: (-e["weight"], e["source"], e["target"]))
