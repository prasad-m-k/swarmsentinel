"""Local SQLite store of normalized AI Village events. Lives outside version control."""
import json
import os
import sqlite3
import zlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
DATA_DIR = Path(os.environ.get("SWARMSENTINEL_DATA_DIR", REPO_ROOT / "data" / "ai-village"))
DB_PATH = DATA_DIR / "village.sqlite"

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (ts TEXT NOT NULL, id TEXT NOT NULL, day TEXT NOT NULL, agent TEXT NOT NULL, body BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS agents (name TEXT PRIMARY KEY, id TEXT, model TEXT, joined TEXT);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS episodes (id TEXT PRIMARY KEY, day TEXT, start TEXT, end TEXT, score REAL, body TEXT);
"""


def connect(path=DB_PATH, create=False):
    if not create and not Path(path).exists():
        return None
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript(SCHEMA)
    return db


def pack(event):
    return zlib.compress(json.dumps(event, separators=(",", ":")).encode())


def unpack(blob):
    return json.loads(zlib.decompress(blob))


def meta(db):
    return {k: json.loads(v) for k, v in db.execute("SELECT key, value FROM meta")}


def window(db, start, end, limit):
    rows = db.execute("SELECT body FROM events WHERE ts >= ? AND ts < ? ORDER BY ts, id LIMIT ?", (start, end, limit + 1)).fetchall()
    return [unpack(b) for (b,) in rows[:limit]], len(rows) > limit


def day(db, value):
    return [unpack(b) for (b,) in db.execute("SELECT body FROM events WHERE day = ? ORDER BY ts, id", (value,))]


def registry(db, until):
    """Agents the village had admitted by `until`: the gateway's lineage registry."""
    return {name: ("village", 1, f"admit:{name}") for (name,) in db.execute("SELECT name FROM agents WHERE joined <= ?", (until,))}


def episodes(db, limit=60):
    return [json.loads(b) for (b,) in db.execute("SELECT body FROM episodes ORDER BY score DESC LIMIT ?", (limit,))]


def episode(db, episode_id):
    row = db.execute("SELECT body FROM episodes WHERE id = ?", (episode_id,)).fetchone()
    return json.loads(row[0]) if row else None
