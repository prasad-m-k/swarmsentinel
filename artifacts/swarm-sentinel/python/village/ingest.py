"""Normalize locally downloaded AI Village tables into the SwarmSentinel event store.

    cd artifacts/swarm-sentinel/python
    python -m village.ingest --raw ../../../download-hugginface [--since 2026-06-01 --until 2026-07-01]

Raw files are read in place and never copied. Only normalized, scrubbed events are stored.
"""
import argparse
import gzip
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from village import store
from village.normalize import Normalizer, iso, pacific_day

CITATION = 'AI Digest, "AI Village dataset", 2026. https://theaidigest.org/village'


def rows(raw, table):
    with gzip.open(raw / f"{table}.jsonl.gz", "rt") as f:
        for line in f:
            yield json.loads(line)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--raw", type=Path, default=store.REPO_ROOT / "download-hugginface")
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--since", default="", help="UTC date (inclusive), e.g. 2026-06-01")
    parser.add_argument("--until", default="9999", help="UTC date (exclusive)")
    args = parser.parse_args()

    started = time.time()
    if args.db.exists():
        args.db.unlink()
    db = store.connect(args.db, create=True)
    agents = {}
    for r in rows(args.raw, "agents"):
        agents[r["id"]] = (r["name"], r["created_at"])
        db.execute("INSERT INTO agents VALUES (?,?,?,?)", (r["name"], r["id"], r["model_string"], iso(r["created_at"])))
    rooms = {r["id"]: r["name"] for r in rows(args.raw, "chat_rooms")}
    sessions = {r["id"]: r["agent_id"] for r in rows(args.raw, "computer_use_sessions")}
    normalizer = Normalizer(agents, sessions, rooms)
    in_range = lambda ts: args.since <= ts[:10] < args.until

    counts, batch = Counter(), []

    def add(event, ts):
        if event:
            counts[event["sourceRef"].split(":")[0]] += 1
            batch.append((event["timestamp"], event["id"], pacific_day(ts), event["agentId"], store.pack(event)))
            if len(batch) >= 5000:
                db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", batch)
                batch.clear()

    for r in rows(args.raw, "chat_messages"):
        if in_range(r["created_at"]):
            add(normalizer.chat(r), r["created_at"])
    print(f"chat done {counts} {time.time()-started:.0f}s", flush=True)
    for r in rows(args.raw, "claude_code_messages"):
        if r["message_type"] == "assistant" and in_range(r["created_at"]):
            for event in normalizer.claude_code(r):
                add(event, r["created_at"])
    print(f"claude code done {counts} {time.time()-started:.0f}s", flush=True)
    for r in rows(args.raw, "computer_use_turns"):
        if in_range(r["created_at"]):
            add(normalizer.turn(r), r["created_at"])
    db.executemany("INSERT INTO events VALUES (?,?,?,?,?)", batch)
    print(f"turns done {counts} {time.time()-started:.0f}s", flush=True)
    db.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts, id)")
    db.execute("CREATE INDEX IF NOT EXISTS events_day ON events(day)")

    manifest = json.loads((args.raw / "manifest.json").read_text())
    first, last = db.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
    info = dict(
        dataset="aidigestorg/ai-village", exportedAt=manifest.get("exportedAt"), citation=CITATION,
        ingestedAt=datetime.now(timezone.utc).isoformat(), range=[first, last],
        filter=[args.since or None, None if args.until == "9999" else args.until],
        events=dict(counts), skipped=dict(sorted(normalizer.skipped.items(), key=lambda kv: -kv[1])),
    )
    db.executemany("INSERT OR REPLACE INTO meta VALUES (?,?)", [(k, json.dumps(v)) for k, v in info.items()])
    db.commit()
    print(json.dumps(info, indent=2))
    print(f"wrote {args.db} in {time.time()-started:.0f}s")


if __name__ == "__main__":
    main()
