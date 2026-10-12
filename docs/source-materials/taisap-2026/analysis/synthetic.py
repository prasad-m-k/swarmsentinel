"""Per-scenario decisions for the synthetic scenarios, feedback on and off (run from the engine dir)."""
import json
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from asp_policy import ASPPolicy
from pipeline import replay

policy = ASPPolicy.load("mock")
out = {}
for scenario in ["normal", "attack", "injection", "runaway"]:
    events = json.loads(Path(f"samples/{scenario}.json").read_text())
    for feedback in (True, False):
        g, s = replay(events, SimpleNamespace(feedbackEnabled=feedback), policy)
        t = g.telemetry
        out[f"{scenario}/{'on' if feedback else 'off'}"] = dict(
            events=len(t), decisions=dict(Counter(x.decision for x in t)),
            rules=dict(Counter(x.rule for x in t if x.decision in ("drop", "throttle"))),
            alerts=[(a["kind"], a["agents"]) for a in s.alerts], revoked=sorted(g.revoked),
            steps=[(x.id, x.agentId, x.action, x.target, x.decision, x.rule) for x in t] if scenario == "injection" else None)
print(json.dumps(out, indent=1))
