from collections import Counter
from asp_gateway import ASPGateway
from models import Event
from sentinel import Sentinel


def replay(events, settings, policy, mode="enforce", registry=None, root="orchestrator", root_is_actor=True):
    gateway = ASPGateway(settings, policy, mode=mode, registry=registry, root=root, root_is_actor=root_is_actor)
    sentinel = Sentinel(policy)
    for raw in events:
        trace = gateway.evaluate_and_log(raw if isinstance(raw, Event) else Event.model_validate(raw))
        sentinel.ingest(trace, gateway)
    return gateway, sentinel


def summarize(traces, alerts):
    """Counts the dashboard and reporter share. In report-only runs, non-allow decisions are counterfactual."""
    decisions = Counter(t.decision for t in traces)
    blocked = [t for t in traces if t.decision in {"drop", "throttle"}]
    advisories = Counter(v.directive for t in traces for v in t.violations if not v.enforced and t.decision == "allow")
    return dict(
        decisions=dict(decisions),
        blockedByRule=dict(Counter(t.rule for t in blocked).most_common()),
        advisories=dict(advisories),
        alertsByKind=dict(Counter(a["kind"] for a in alerts)),
        agents=len({t.agentId for t in traces}),
        writes=sum(1 for t in traces if t.write),
        contaminatedWrites=sum(1 for t in traces if t.write and t.contaminated),
        networkHosts=dict(Counter(t.network for t in traces if t.network).most_common(15)),
        sharedResources=dict(Counter(t.resource for t in traces if t.resource).most_common(15)),
    )
