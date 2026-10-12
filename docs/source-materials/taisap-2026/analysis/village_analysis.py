"""Aggregate-only, read-only analysis of the AI Village replay for the TAISAP manuscript.

Run from artifacts/swarm-sentinel/python:
    ../../../.venv/bin/python <this file> --out results.json [--variants all]

Replays every village day through ASPGateway + Sentinel in report-only mode with feedback off,
exactly as village.scan does, but never writes to the store. Output holds counts only: no chat
text, commands, intents or row identifiers leave this script.
"""
import argparse
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from datetime import datetime
from multiprocessing import Pool
from types import SimpleNamespace

from asp_gateway import ASPGateway
from asp_policy import ASPPolicy
from models import Event
from sentinel import Sentinel
from village import store

SCAFFOLD_CHANGES = ("2026-02-10", "2026-03-24")  # auto-nudger; perma-computer-use (dataset CHANGELOG)
LABEL = re.compile(r"Argument constraint: (.+)$")


def period(day):
    if day < SCAFFOLD_CHANGES[0]:
        return "P1 (before 2026-02-10)"
    if day < SCAFFOLD_CHANGES[1]:
        return "P2 (2026-02-10 to 2026-03-23)"
    return "P3 (from 2026-03-24)"


def variant_policy(name):
    base = ASPPolicy.load("ai-village").model_dump(by_alias=True)
    kind, _, value = name.partition("=")
    if kind == "write-cap":
        base["swarm"]["write-cap"] = int(value)
    elif kind == "repeated-intent":
        base["swarm"]["repeated-intent"]["limit"] = int(value)
    elif kind == "consensus-density":
        base["swarm"]["tripwires"]["consensus-density"] = float(value)
    elif kind in ("propagate", "agent-taint"):
        base["content-trust"]["propagate-via-messages"] = True
    elif kind == "agent-taint-local":
        base["content-trust"]["propagate-via-messages"] = False
    elif kind.startswith("conf-"):
        # Confidentiality labels from the policy's own credential argument rules.
        rules = {r["label"]: r["pattern"] for r in base["tools"]["deny-arguments"]}
        base["confidentiality"] = {"classes": {"credentials": {"arguments": [
            rules["credential store access"], rules["environment secret disclosure"]]}},
            "propagate-via-messages": kind.endswith("-prop")}
        if "-task" in kind:
            base["content-trust"]["taint-scope"] = "task"
    elif kind in ("task", "task-local"):
        base["content-trust"]["taint-scope"] = "task"
        base["content-trust"]["propagate-via-messages"] = kind == "task"
    elif kind != "baseline":
        raise ValueError(name)
    return ASPPolicy.model_validate(base)


class AgentTaintGateway(ASPGateway):
    """Counterfactual: an untrusted read taints the whole agent (not just its span), as live sessions do."""

    def evaluate_and_log(self, event):
        trace = super().evaluate_and_log(event)
        if trace.executed and event.reads == "untrusted" and event.agentId not in self.tainted \
                and event.agentId not in self.protected:
            self.tainted[event.agentId] = event.id
            self.taint_groups[event.id].add(event.agentId)
        return trace


def run_day(job):
    day, variant = job
    db = store.connect()
    events = store.day(db, day)
    if not events:
        return day, None
    registry = store.registry(db, events[-1]["timestamp"])
    policy = variant_policy(variant)
    cls = AgentTaintGateway if variant.startswith("agent-taint") else ASPGateway
    gateway = cls(SimpleNamespace(feedbackEnabled=False), policy, mode="report-only",
                  registry=registry, root="village", root_is_actor=False)
    sentinel = Sentinel(policy)
    taint_times = {}
    active = set()
    task_mode = variant in ("task", "task-local") or variant.startswith("conf-task")
    conf_mode = variant.startswith("conf-")
    egress = Counter()
    for raw in events:
        event = Event.model_validate(raw)
        if task_mode and event.spanId.startswith(("cu:", "cc:")):
            # Computer-use and coding sessions are started by the village scaffold, a trusted controller.
            if event.spanId not in gateway.tasks[event.agentId]:
                gateway.open_task(event.agentId, event.spanId)
            event.taskId = event.spanId
        if conf_mode and event.channel == "in_band" and event.action in ("tool", "message"):
            # One in-band action is one model turn; the request that produced it carried the context's prior labels.
            egress["model_requests"] += 1
            if "credentials" in gateway.labels_for(event.agentId, gateway.task_of(event)):
                egress["labelled_requests"] += 1
        trace = gateway.evaluate_and_log(event)
        sentinel.ingest(trace, gateway)
        if trace.action != "spawn":
            active.add(trace.agentId)
        if variant in ("propagate", "agent-taint", "agent-taint-local"):
            for agent in gateway.tainted:
                taint_times.setdefault(agent, trace.timestamp)
        if task_mode:
            for (agent, _task) in gateway.task_tainted:
                taint_times.setdefault(agent, trace.timestamp)
    traces = gateway.telemetry
    out = dict(day=day, period=period(day), events=len(traces))
    out["actions"] = Counter(t.action for t in traces)
    out["decisions"] = Counter(t.decision for t in traces)
    blocked = [t for t in traces if t.decision in {"drop", "throttle"}]
    out["by_rule"] = Counter(t.rule for t in blocked)
    out["arg_labels"] = Counter(m.group(1) for t in blocked if t.rule == "tools.deny_arguments"
                                and (m := LABEL.search(t.reason.removeprefix("Would drop: "))))
    out["arg_label_agents"] = {lab: sorted({t.agentId for t in blocked if t.rule == "tools.deny_arguments"
                                            and t.reason.endswith(lab)}) for lab in out["arg_labels"]}
    out["agents"] = sorted({t.agentId for t in traces})
    out["agent_events"] = Counter(t.agentId for t in traces)
    out["agent_blocked"] = Counter(t.agentId for t in blocked)
    out["writes"] = sum(1 for t in traces if t.write)
    out["contaminated_writes"] = sum(1 for t in traces if t.write and t.contaminated)
    out["untrusted_reads"] = sum(1 for t in traces if t.reads == "untrusted")
    out["network_events"] = sum(1 for t in traces if t.network)
    out["network_hosts_dropped"] = len({t.network for t in blocked if t.rule == "network.default_deny"})
    out["alerts"] = Counter(a["kind"] for a in sentinel.alerts)
    if conf_mode:
        egress["labelled_units"] = sum(1 for v in gateway.labels.values() if "credentials" in v)
        egress["units"] = len(gateway.tasks and [t for ts in gateway.tasks.values() for t in ts]) if task_mode else len(active)
        egress["labelled_agents"] = len({(k[0] if isinstance(k, tuple) else k) for k, v in gateway.labels.items() if "credentials" in v})
        out["egress"] = egress
    if variant in ("propagate", "agent-taint", "agent-taint-local", "task", "task-local") and active:
        start = datetime.fromisoformat(traces[0].timestamp)
        times = sorted((datetime.fromisoformat(v) - start).total_seconds() / 60 for v in taint_times.values())
        need = (len(active) // 2) + 1
        if task_mode:
            opened = sum(len(v) for v in gateway.tasks.values())
            out["tasks"] = dict(opened=opened, tainted=len(gateway.task_tainted))
        out["taint"] = dict(active=len(active), tainted=len(taint_times),
                            minutes_to_majority=times[need - 1] if len(times) >= need else None)
    return day, out


def merge(rows):
    tot = defaultdict(Counter)
    scal = Counter()
    agents, label_agents = set(), defaultdict(set)
    per_agent_events, per_agent_blocked = Counter(), Counter()
    periods = defaultdict(Counter)
    taint = []
    tasks = Counter()
    egress_tot, egress_days = Counter(), 0
    days_without_alert = 0
    cred_days = Counter()
    for r in rows:
        for k in ("actions", "decisions", "by_rule", "arg_labels", "alerts"):
            tot[k].update(r[k])
        for k in ("events", "writes", "contaminated_writes", "untrusted_reads", "network_events"):
            scal[k] += r[k]
        agents.update(r["agents"])
        for lab, a in r["arg_label_agents"].items():
            label_agents[lab].update(a)
        per_agent_events.update(r["agent_events"])
        per_agent_blocked.update(r["agent_blocked"])
        p = periods[r["period"]]
        p["days"] += 1
        p["events"] += r["events"]
        p["would_block"] += sum(r["by_rule"].values())
        p["credential_reads"] += r["arg_labels"].get("credential store access", 0)
        p["alerts"] += sum(r["alerts"].values())
        if not r["alerts"]:
            days_without_alert += 1
        if "taint" in r:
            taint.append(r["taint"])
        if "tasks" in r:
            tasks.update(r["tasks"])
        if "egress" in r:
            egress_tot.update(r["egress"])
            egress_days += bool(r["egress"].get("labelled_requests"))
    rates = sorted(per_agent_blocked[a] / n for a, n in per_agent_events.items() if n >= 100 and a != "human")
    summary = dict(
        days=len(rows), events=scal["events"], agents=len(agents - {"human", "village"}),
        actions=dict(tot["actions"]), decisions=dict(tot["decisions"]),
        would_block=sum(tot["by_rule"].values()), by_rule=dict(tot["by_rule"].most_common()),
        arg_labels=dict(tot["arg_labels"].most_common()),
        arg_label_agent_counts={k: len(v - {"human"}) for k, v in label_agents.items()},
        writes=scal["writes"], contaminated_writes=scal["contaminated_writes"],
        untrusted_reads=scal["untrusted_reads"], network_events=scal["network_events"],
        alerts=dict(tot["alerts"]), alert_total=sum(tot["alerts"].values()),
        days_without_alert=days_without_alert,
        periods={k: dict(v) for k, v in sorted(periods.items())},
        per_agent_would_block_rate=dict(n=len(rates), min=rates[0] if rates else None,
                                        median=statistics.median(rates) if rates else None,
                                        max=rates[-1] if rates else None),
    )
    if tasks:
        summary["tasks"] = dict(tasks)
    if egress_tot:
        summary["egress"] = dict(egress_tot, days_with_labelled_requests=egress_days)
    if taint:
        reached = [t["minutes_to_majority"] for t in taint if t["minutes_to_majority"] is not None]
        summary["taint_propagation"] = dict(
            days=len(taint), days_majority_tainted=len(reached),
            median_minutes_to_majority=statistics.median(reached) if reached else None,
            p25=statistics.quantiles(reached, n=4)[0] if len(reached) > 3 else None,
            p75=statistics.quantiles(reached, n=4)[2] if len(reached) > 3 else None,
            mean_fraction_tainted=statistics.fmean(t["tainted"] / t["active"] for t in taint))
    return summary


VARIANTS = ["baseline", "write-cap=6", "write-cap=24", "repeated-intent=3", "repeated-intent=8",
            "consensus-density=0.5", "consensus-density=0.8", "propagate"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--variants", default="all")
    ap.add_argument("--workers", type=int, default=6)
    args = ap.parse_args()
    db = store.connect()
    days = [d for (d,) in db.execute("SELECT DISTINCT day FROM events ORDER BY day")]
    meta = store.meta(db)
    variants = VARIANTS if args.variants == "all" else args.variants.split(",")
    results = dict(export=meta.get("export"), days_in_store=len(days))
    for variant in variants:
        started = time.time()
        with Pool(args.workers) as pool:
            rows = [r for _, r in pool.map(run_day, [(d, variant) for d in days], chunksize=4) if r]
        results[variant] = merge(rows)
        results[variant]["seconds"] = round(time.time() - started, 1)
        print(variant, results[variant]["events"], results[variant]["would_block"], results[variant]["alert_total"],
              f"{results[variant]['seconds']}s", flush=True)
        with open(args.out, "w") as fh:
            json.dump(results, fh, indent=2, sort_keys=True)


if __name__ == "__main__":
    main()
