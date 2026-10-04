"""Replay AI Village episodes from your own local store, without the web app or a sign-in.

    cd artifacts/swarm-sentinel/python
    python -m village.replay --list                      # indexed episodes, best first
    python -m village.replay --episode 2026-05-11-1643   # replay one, write report + trace
    python -m village.replay --start 2026-05-11T16:30:00Z --end 2026-05-11T18:00:00Z

Runs the same engine path as the dashboard's AI Village tab (report-only, counterfactual) and writes
the same Markdown report and JSON trace it exports, under data/ai-village/replays/ (gitignored).
"""
import argparse
import json
from collections import Counter
from pathlib import Path
from uuid import uuid4
from asp_policy import ASPPolicy
from models import RunInput
from pipeline import replay
from server import VILLAGE, _run, _village_events
from village import store


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--list", action="store_true", help="list indexed episodes")
    parser.add_argument("--episode", help="episode id from --list")
    parser.add_argument("--start", help="UTC window start (with --end, max 24 h)")
    parser.add_argument("--end")
    parser.add_argument("--no-feedback", action="store_true", help="detect only; do not apply revocations")
    parser.add_argument("--out", type=Path, default=store.DATA_DIR / "replays")
    args = parser.parse_args()

    db = store.connect()
    if db is None:
        raise SystemExit(f"No store at {store.DB_PATH}. Build one first: python -m village.ingest, then python -m village.scan")
    if args.list or not (args.episode or (args.start and args.end)):
        for e in store.episodes(db, 60):
            kinds = ", ".join(f"{k} {n}" for k, n in e["alertsByKind"].items())
            print(f"{e['id']:<18} {e['events']:>5} events  {e['alerts']:>2} alerts ({kinds})")
        return

    settings = RunInput(scenario="ai-village", episodeId=args.episode, start=args.start, end=args.end,
                        feedbackEnabled=not args.no_feedback)
    events, registry, window = _village_events(settings)
    policy = ASPPolicy.load("ai-village")
    gateway, sentinel = replay(events, settings, policy, mode="report-only", registry=registry,
                               root="village", root_is_actor=False)
    run = _run(str(uuid4()), "ai-village", "ai-village", VILLAGE, gateway, sentinel, "ai-village", policy,
               settings, events[0]["timestamp"], window)

    args.out.mkdir(parents=True, exist_ok=True)
    name = args.episode or f"{args.start}_{args.end}".replace(":", "")
    (args.out / f"{name}-report.md").write_text(run["report"])
    (args.out / f"{name}-traces.json").write_text(json.dumps(run, indent=2))
    s = run["summary"]
    print(f"{len(run['events'])} events, {len(run['alerts'])} alerts {dict(Counter(a['kind'] for a in run['alerts']))}")
    print(f"would block (counterfactual): {s['blockedByRule']}")
    print(f"report: {args.out / (name + '-report.md')}\ntraces: {args.out / (name + '-traces.json')}")


if __name__ == "__main__":
    main()
