"""Layer ablation on the injection scenario (Simulation A): which payments would execute without each layer."""
import copy, json
from pathlib import Path
from types import SimpleNamespace
from asp_policy import ASPPolicy
from pipeline import replay

base_events = json.loads(Path("samples/injection.json").read_text())
base = ASPPolicy.load("mock").model_dump(by_alias=True)

def run(name, no_scope=False, no_taint=False, no_tripwire=False):
    pol = copy.deepcopy(base)
    events = copy.deepcopy(base_events)
    if no_scope:
        for e in events:
            e["scope"] = []
    if no_taint:
        pol["content-trust"].update({"contaminated-deny": [], "contaminated-write": "report", "propagate-via-messages": False})
    if no_tripwire:
        pol["swarm"]["tripwires"]["taint-agents"] = 0
    g, s = replay(events, SimpleNamespace(feedbackEnabled=not no_tripwire), ASPPolicy.model_validate(pol))
    pays = [(t.agentId, t.decision, t.rule) for t in g.telemetry if t.target == "mock:payments.transfer"]
    spawned = [t.decision for t in g.telemetry if t.action == "spawn" and t.target == "payment-helper"]
    board = [(t.agentId, t.decision, t.rule) for t in g.telemetry if t.target == "mock:mcp:board.write"]
    return dict(variant=name, payments=pays, helper_spawn=spawned, board=board,
                executed_payments=sum(1 for _, d, _ in pays if d == "allow"),
                alerts=[a["kind"] for a in s.alerts])

rows = [run("full ASP"), run("no tripwire feedback", no_tripwire=True), run("no taint", no_taint=True),
        run("no delegated scopes", no_scope=True), run("no taint, no scopes", no_scope=True, no_taint=True),
        run("allowlist only (no scopes, taint, tripwires)", no_scope=True, no_taint=True, no_tripwire=True)]
print(json.dumps(rows, indent=1))
