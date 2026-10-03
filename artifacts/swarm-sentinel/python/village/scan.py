"""Replay every village day through ASP + Sentinel (report-only) and index candidate swarm episodes.

    cd artifacts/swarm-sentinel/python
    python -m village.scan [--per-day 3]

An episode is a cluster of tripwire alerts. Its replay window opens shortly before the first
alert so the build-up is visible. Scores rank episodes for review; they are not severity ratings.
"""
import argparse
import json
import time
from collections import Counter
from datetime import datetime, timedelta
from types import SimpleNamespace
from asp_policy import ASPPolicy
from pipeline import replay
from village import store

LEAD, TAIL, GAP, MAX_SPAN = timedelta(minutes=20), timedelta(minutes=10), timedelta(minutes=30), timedelta(hours=2)


def clusters(alerts):
    group = []
    for alert in alerts:
        t = datetime.fromisoformat(alert["timestamp"])
        if group and (t - datetime.fromisoformat(group[-1]["timestamp"]) > GAP or t - datetime.fromisoformat(group[0]["timestamp"]) > MAX_SPAN):
            yield group
            group = []
        group.append(alert)
    if group:
        yield group


def scan_day(day, events, policy, registry):
    gateway, sentinel = replay(events, SimpleNamespace(feedbackEnabled=False), policy, mode="report-only",
                               registry=registry, root="village", root_is_actor=False)
    traces = gateway.telemetry
    found = []
    for group in clusters(sentinel.alerts):
        start = datetime.fromisoformat(group[0]["timestamp"]) - LEAD
        end = datetime.fromisoformat(group[-1]["timestamp"]) + TAIL
        inside = [t for t in traces if start <= datetime.fromisoformat(t.timestamp) < end]
        kinds = Counter(a["kind"] for a in group)
        blocked = Counter(t.rule for t in inside if t.decision in {"drop", "throttle"})
        agents = sorted({a for alert in group for a in alert["agents"]})
        found.append(dict(
            id=f"{day}-{start.strftime('%H%M')}", day=day, start=start.isoformat(), end=end.isoformat(),
            events=len(inside), alerts=len(group), alertsByKind=dict(kinds), agents=agents,
            wouldBlock=sum(blocked.values()), blockedByRule=dict(blocked.most_common(5)),
            resources=[r for r, _ in Counter(t.resource for t in inside if t.resource).most_common(5)],
            score=round(len(group) + 2 * len(kinds) + len(agents) / 4, 2),
            headline=group[0]["reason"],
        ))
    return found, sentinel.alerts, traces


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--per-day", type=int, default=3)
    args = parser.parse_args()
    db = store.connect()
    if db is None:
        raise SystemExit("No store found. Run `python -m village.ingest` first.")
    policy = ASPPolicy.load("ai-village")
    started, days, kept = time.time(), {}, []
    db.execute("DELETE FROM episodes")
    for (day,) in db.execute("SELECT DISTINCT day FROM events ORDER BY day").fetchall():
        events = store.day(db, day)
        found, alerts, traces = scan_day(day, events, policy, store.registry(db, events[-1]["timestamp"]))
        days[day] = dict(events=len(events), alerts=dict(Counter(a["kind"] for a in alerts)),
                         wouldBlock=sum(1 for t in traces if t.decision in {"drop", "throttle"}))
        best = sorted(found, key=lambda e: -e["score"])[:args.per_day]
        kept.extend(best)
        db.executemany("INSERT OR REPLACE INTO episodes VALUES (?,?,?,?,?,?)",
                       [(e["id"], e["day"], e["start"], e["end"], e["score"], json.dumps(e)) for e in best])
        print(f"{day}: {len(events):>6} events, {len(alerts):>3} alerts, {len(found)} episodes  ({time.time()-started:.0f}s)", flush=True)
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("days", json.dumps(days)))
    db.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", ("scanPolicy", json.dumps(policy.declaration())))
    db.commit()
    totals = Counter()
    for d in days.values():
        totals.update(d["alerts"])
    print(f"\n{len(days)} days, {len(kept)} episodes kept, alerts by kind {dict(totals)}, {time.time()-started:.0f}s")


if __name__ == "__main__":
    main()
