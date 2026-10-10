"""Durable live sessions: an append-only SQLite journal per session, replayed to restore state.

Enabled by SWARM_STATE_DB=<path>. Without it the engine keeps its in-memory behaviour.

The gateway and Sentinel are deterministic given their inputs and event timestamps, so a session is
restored by replaying, in order, what was evaluated plus the few state changes made outside the
gateway (outcome evidence, taint pins, MCP agents). Replay rebuilds the recorder, taint, counters,
alerts and revocations exactly. Not restored: sandbox fixtures, downstream MCP server workspaces,
unused completion tokens and missing-completion timers (docs/durable-state.md).

The journal holds tool arguments as the recorder does. Keep the file on private storage, and never
use it for AI Village replays (those are not live sessions and are not journaled).
"""
import json
import os
import sqlite3
import threading
from contextlib import closing

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, tenant TEXT, owner TEXT, created TEXT NOT NULL,
    spec TEXT NOT NULL, policy TEXT NOT NULL, events INTEGER NOT NULL DEFAULT 0, alerts INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS sessions_tenant ON sessions (tenant);
CREATE TABLE IF NOT EXISTS journal (
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    seq INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
    PRIMARY KEY (session_id, seq)
);
CREATE TABLE IF NOT EXISTS credentials (
    digest TEXT PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions (id) ON DELETE CASCADE,
    agent_id TEXT NOT NULL
);
"""


class StateStore:
    def __init__(self, path):
        self.path = str(path)
        if self.path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        if self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self.lock = threading.Lock()
        with self.lock:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA foreign_keys=ON")
            self.db.executescript(SCHEMA)
        self.seq = {}

    def _write(self, sql, args=()):
        with self.lock:
            self.db.execute(sql, args)

    def create(self, session):
        self._write("INSERT INTO sessions (id, tenant, owner, created, spec, policy) VALUES (?, ?, ?, ?, ?, ?)",
                    (session.id, session.tenant, session.owner, session.created, session.spec.model_dump_json(),
                     json.dumps(session.policy.model_dump(by_alias=True))))
        self.seq[session.id] = 0

    def append(self, session_id, kind, payload, events, alerts):
        with self.lock:
            seq = self.seq.get(session_id)
            if seq is None:
                seq = self.db.execute("SELECT COALESCE(MAX(seq), 0) FROM journal WHERE session_id = ?",
                                      (session_id,)).fetchone()[0]
            seq += 1
            self.db.execute("BEGIN")
            try:
                self.db.execute("INSERT INTO journal VALUES (?, ?, ?, ?)", (session_id, seq, kind, json.dumps(payload)))
                self.db.execute("UPDATE sessions SET events = ?, alerts = ? WHERE id = ?", (events, alerts, session_id))
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise
            self.seq[session_id] = seq

    def delete(self, session_id):
        # Hard delete: the journal and credentials go with the session (data minimisation).
        self._write("DELETE FROM sessions WHERE id = ?", (session_id,))
        self.seq.pop(session_id, None)

    def row(self, session_id):
        with self.lock, closing(self.db.execute(
                "SELECT id, tenant, owner, created, spec, policy, events, alerts FROM sessions WHERE id = ?",
                (session_id,))) as cursor:
            return cursor.fetchone()

    def journal(self, session_id):
        with self.lock, closing(self.db.execute(
                "SELECT kind, payload FROM journal WHERE session_id = ? ORDER BY seq", (session_id,))) as cursor:
            return [(kind, json.loads(payload)) for kind, payload in cursor.fetchall()]

    def rows(self, tenant):
        with self.lock, closing(self.db.execute(
                "SELECT id, tenant, owner, created, spec, events, alerts FROM sessions WHERE tenant = ?",
                (tenant,))) as cursor:
            return cursor.fetchall()

    def count(self, tenant):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM sessions WHERE tenant = ?", (tenant,)).fetchone()[0]

    def add_credential(self, digest, session_id, agent_id):
        self._write("INSERT INTO credentials VALUES (?, ?, ?)", (digest, session_id, agent_id))

    def credential(self, digest):
        with self.lock:
            row = self.db.execute("SELECT session_id, agent_id FROM credentials WHERE digest = ?", (digest,)).fetchone()
        return tuple(row) if row else None

    def close(self):
        with self.lock:
            self.db.close()


STORE = None


def configure(path):
    """Install the process-wide store (or remove it with None). Returns the store."""
    global STORE
    STORE = StateStore(path) if path else None
    return STORE


def from_env():
    return configure(os.environ.get("SWARM_STATE_DB") or None)
