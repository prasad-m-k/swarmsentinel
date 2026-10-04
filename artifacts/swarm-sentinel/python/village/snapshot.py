"""Export a reduced, structure-only AI Village snapshot for private hosting.

    cd artifacts/swarm-sentinel/python
    python -m village.snapshot [--episodes 60] [--out ../../../data/ai-village/snapshot]

The snapshot keeps what replay needs and drops what a reader would recognise:

- Only events inside the top-ranked indexed episodes (by scan score) are copied.
- Intent text (chat, bash comments) is replaced word by word with keyed hashes. The key is random
  and discarded, so words cannot be recovered or guessed, yet the repeated-intent rule (string
  equality) and the echo detector (token overlap) produce the same decisions as on full text.
- Shell commands are dropped except the exact fragment an argument rule matched, so credential and
  force-push rules still fire. Hash fingerprints are emptied.
- Agent names, hostnames, repository names, timestamps and decisions inputs are kept.

It is still derived from the AI Village dataset and stays under its research terms: keep it private,
serve it only to approved readers, cite AI Digest. Output is gitignored with the rest of data/.
"""
import argparse
import gzip
import hashlib
import hmac
import json
import os
import re
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from asp_policy import ASPPolicy, tool_matches
from village import store

TOKEN = re.compile(r"[a-z0-9']{3,}")      # same token pattern as sentinel._tokens
GREEK = "αβγδεζηθικλμνξοπρστυφχψω"          # not in [a-z0-9'], so word tags never become tokens


class Withholder:
    def __init__(self, policy):
        self.key = os.urandom(32)            # never stored: the mapping is irreversible
        self.rules = policy.tools.deny_arguments

    def _mac(self, kind, value):
        return hmac.new(self.key, f"{kind}:{value}".encode(), hashlib.sha256).digest()

    def _word(self, word):
        tokens = "-".join(self._mac("t", t).hex()[:10] for t in TOKEN.findall(word))
        tag = "".join(GREEK[b % len(GREEK)] for b in self._mac("w", word)[:6])
        return f"{tokens}·{tag}" if tokens else tag

    def intent(self, text):
        """Equality after lower()+whitespace normalisation, and token sets, are preserved exactly."""
        return " ".join(self._word(w) for w in text.lower().split())

    def detail(self, target, text):
        """Keep only the first argument-rule match, which is what the gateway evaluates."""
        for rule in self.rules:
            if tool_matches(target, [rule.tool]):
                m = re.search(rule.pattern, text or "")
                if m:
                    return m.group(0)
        return ""

    def event(self, e):
        return {**e, "intent": self.intent(e.get("intent", "")), "detail": self.detail(e["target"], e.get("detail", "")),
                "intentVector": []}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source", type=Path, default=store.DB_PATH)
    parser.add_argument("--out", type=Path, default=store.DATA_DIR / "snapshot")
    parser.add_argument("--episodes", type=int, default=60, help="top-ranked episodes to include")
    args = parser.parse_args()

    src = sqlite3.connect(f"file:{args.source}?mode=ro", uri=True)
    src.execute("BEGIN")                     # one read transaction: a consistent view
    if not src.execute("SELECT count(*) FROM episodes").fetchone()[0]:
        raise SystemExit("Source has no indexed episodes. Run `python -m village.scan` first.")
    episodes = [json.loads(b) for (b,) in src.execute("SELECT body FROM episodes ORDER BY score DESC LIMIT ?", (args.episodes,))]
    withhold = Withholder(ASPPolicy.load("ai-village"))

    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / "village-snapshot.sqlite"
    if path.exists():
        path.unlink()
    dst = store.connect(path, create=True)
    seen = set()
    for ep in episodes:
        rows = src.execute("SELECT ts, id, day, agent, body FROM events WHERE ts >= ? AND ts < ? ORDER BY ts, id", (ep["start"], ep["end"]))
        batch = [(ts, eid, day, agent, store.pack(withhold.event(store.unpack(body))))
                 for ts, eid, day, agent, body in rows if eid not in seen and not seen.add(eid)]
        dst.executemany("INSERT INTO events VALUES (?,?,?,?,?)", batch)
        dst.execute("INSERT INTO episodes VALUES (?,?,?,?,?,?)",
                    (ep["id"], ep["day"], ep["start"], ep["end"], ep["score"], json.dumps(ep)))
    dst.executemany("INSERT INTO agents VALUES (?,?,?,?)", src.execute("SELECT name, id, model, joined FROM agents"))
    meta = dict(src.execute("SELECT key, value FROM meta"))
    meta["snapshot"] = json.dumps(dict(
        mode="structure-only", textWithheld=True, episodes=len(episodes), events=len(seen),
        createdAt=datetime.now(timezone.utc).isoformat(),
        sourceEvents=src.execute("SELECT count(*) FROM events").fetchone()[0]))
    meta.pop("days", None)                   # per-day statistics describe the full store, not this subset
    dst.executemany("INSERT INTO meta VALUES (?,?)", meta.items())
    dst.execute("CREATE INDEX IF NOT EXISTS events_ts ON events(ts, id)")
    dst.execute("CREATE INDEX IF NOT EXISTS events_day ON events(day)")
    dst.commit()
    dst.execute("VACUUM")
    dst.close()
    src.rollback()
    src.close()

    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    (args.out / "village-snapshot.sqlite.sha256").write_text(f"{digest}  village-snapshot.sqlite\n")
    with path.open("rb") as raw, gzip.open(args.out / "village-snapshot.sqlite.gz", "wb", compresslevel=9) as packed:
        shutil.copyfileobj(raw, packed)
    print(f"{len(episodes)} episodes, {len(seen)} events -> {path} ({path.stat().st_size / 1e6:.1f} MB, "
          f"gz {(args.out / 'village-snapshot.sqlite.gz').stat().st_size / 1e6:.1f} MB)\nsha256 {digest}")


if __name__ == "__main__":
    main()
