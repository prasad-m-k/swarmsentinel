"""Durable sessions: journal, replay fidelity, cache eviction, deletion, and restart over HTTP."""
import asyncio
import os
import stat
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import auth
import persistence
import server
import test_access as access
from live import CallInput, SessionInput, SessionStore
from mcp_proxy import broker as mcp_broker
from models import CompletionReceipt
from persistence import StateStore
from sandbox import ExecutionInput
from test_mcp_proxy import running_engine

T0 = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def at(seconds):
    return (T0 + timedelta(seconds=seconds)).isoformat()


def snapshot(session):
    g = session.gateway
    return dict(rows=[t.recorder_dump() for t in g.telemetry], alerts=session.sentinel.alerts,
                changes=session.sentinel.policy_changes, revoked=sorted(g.revoked), tainted=dict(g.tainted),
                contaminated=dict(g.contaminated), version=g.version, agents=dict(g.agents),
                edges=session.sentinel.weighted_edges())


class Durable(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state" / "swarm.db"
        self.store = SessionStore(state=StateStore(self.path))
        self.previous = persistence.STORE
        persistence.STORE = self.store.state
        self.addCleanup(setattr, persistence, "STORE", self.previous)

    def restart(self):
        """A new process: fresh store object, empty credential index, nothing in memory."""
        for session in list(self.store.sessions.values()):
            session.close()
        mcp_broker._credentials.clear()
        self.store = SessionStore(state=StateStore(self.path))
        persistence.STORE = self.store.state
        return self.store

    def test_replay_rebuilds_decisions_alerts_and_revocations_exactly(self):
        s = self.store.create(SessionInput(policy="mock"), owner="owner")
        call = lambda n, **kw: s.evaluate(CallInput(spanId=f"s{n}", timestamp=at(n), **kw))
        for i in range(6):  # fanout tripwire (limit 4) revokes the root's children
            call(i, agentId="orchestrator", action="spawn", target=f"w{i}", intent=f"spawn w{i}")
        for i in range(8):
            call(10 + i, agentId="w1", parentId="orchestrator", parentSpanId="s1", action="tool",
                 target="mock:mcp:wiki.write", intent="update the wiki page", write=True, resource="wiki")
        call(30, agentId="ghost", action="tool", target="mock:search.read", intent="read")
        before = snapshot(s)
        self.assertTrue(before["alerts"] and before["revoked"])
        restored = self.restart().get(s.id)
        self.assertIsNot(restored, s)
        self.assertEqual(snapshot(restored), before)

    def test_sandbox_execution_and_completion_evidence_replay(self):
        s = self.store.create(SessionInput(policy="agents"), owner="owner")
        s.configure_sandbox("adversarial")
        s.evaluate(CallInput(agentId="orchestrator", action="spawn", target="reviewer", spanId="root",
                             scope=["demo:invoice.read", "demo:payments.transfer"]))
        base = dict(agentId="reviewer", spanId="rs", parentId="orchestrator", parentSpanId="root")
        s.execute(ExecutionInput(idempotencyKey="k1", tool="read_invoice", **base))
        s.execute(ExecutionInput(idempotencyKey="k2", tool="transfer_funds",
                                 arguments={"amount": 5000, "recipient": "sandbox-attacker"}, **base))
        decision = s.evaluate(CallInput(agentId="orchestrator", action="tool", target="demo:invoice.read",
                                        spanId="root", intent="external read"))
        s.complete(CompletionReceipt(eventId=decision["eventId"], completionToken=decision["completionToken"],
                                     agentId="orchestrator", spanId="root", executionStatus="succeeded"))
        before = snapshot(s)
        self.assertEqual([r.get("executionProvenance") for r in before["rows"][1:]],
                         ["engine-observed", "engine-observed", "caller-reported"])
        restored = self.restart().get(s.id)
        self.assertEqual(snapshot(restored), before)
        # Fixtures are not durable: the restored session reports them unavailable instead of resetting them.
        self.assertIsNone(restored.sandbox)
        with self.assertRaises(ValueError):
            restored.execute(ExecutionInput(idempotencyKey="k3", tool="read_ledger", **base))

    def test_mcp_agents_credentials_and_taint_survive_a_restart(self):
        s = self.store.create(SessionInput(policy="mcp-demo"), owner="owner")
        _, credential = s.mcp.mint("researcher", "orchestrator", ["mcp:notes:*", "mcp:web:fetch"])
        admission = s.mcp.admit("researcher", "notes__fetch_page", {"url": "https://docs.example.org/"})
        s.mcp.record(admission, "succeeded")
        before = snapshot(s)
        store = self.restart()
        self.assertEqual(mcp_broker.lookup(credential), (s.id, "researcher"))
        restored = store.get(s.id)
        self.assertEqual(snapshot(restored), before)
        refused = restored.mcp.admit("researcher", "notes__write_note", {"name": "n", "text": "t"})
        self.assertEqual(refused.decision["rule"], "trust.contaminated_write")

    def test_eviction_is_a_cache_miss_not_data_loss(self):
        store = SessionStore(limit=1, state=self.store.state)
        first = store.create(SessionInput(policy="mock"), owner="owner")
        first.evaluate(CallInput(agentId="orchestrator", action="tool", target="mock:search.read", spanId="s", intent="a"))
        rows = snapshot(first)["rows"]
        second = store.create(SessionInput(policy="mock"), owner="owner")
        self.assertNotIn(first.id, store.sessions)
        self.assertTrue(first.closed)
        listed = {r["sessionId"]: r for r in store.summaries("user:owner")}
        self.assertEqual((listed[first.id]["events"], listed[second.id]["events"]), (1, 0))
        self.assertEqual(snapshot(store.get(first.id))["rows"], rows)
        self.assertNotIn(second.id, store.sessions)

    def test_quota_counts_durable_sessions_and_delete_removes_everything(self):
        with patch("live.PER_TENANT", 2):
            a = self.store.create(SessionInput(policy="mcp-demo"), owner="owner")
            self.store.create(SessionInput(policy="mock"), owner="owner")
            with self.assertRaises(OverflowError):
                self.store.create(SessionInput(policy="mock"), owner="owner")
            self.store.create(SessionInput(policy="mock"), owner="other")
        _, credential = a.mcp.mint("writer", "orchestrator", [])
        self.restart()
        self.assertTrue(self.store.delete(a.id))
        self.assertIsNone(self.store.get(a.id))
        self.assertIsNone(mcp_broker.lookup(credential))
        self.assertEqual(self.store.state.journal(a.id), [])

    def test_state_file_is_private(self):
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_memory_only_store_is_unchanged(self):
        store = SessionStore(limit=1)
        store.create(SessionInput(), owner="owner")
        with self.assertRaises(OverflowError):
            store.create(SessionInput(), owner="owner")
        self.assertIsNone(store.create(SessionInput()).journal)


class RestartOverHttp(unittest.TestCase):
    """An agent keeps working across an engine restart with the same credential; its taint persists."""
    token = access.AccessControl.token
    headers = access.AccessControl.headers

    @classmethod
    def setUpClass(cls):
        access.AccessControl.setUpClass.__func__(cls)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "swarm.db"
        for patcher in (patch.object(auth, "auth_config", return_value=(access.ISSUER, {access.ORIGIN})),
                        patch.object(auth, "key_client", return_value=SimpleNamespace(
                            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.key.public_key()))),
                        patch.object(persistence, "STORE", None)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def boot(self):
        store = SessionStore(state=StateStore(self.path))
        persistence.STORE = store.state
        mcp_broker._credentials.clear()
        sessions = patch.object(server, "SESSIONS", store)
        sessions.start()
        self.addCleanup(sessions.stop)
        engine = running_engine()
        return engine, engine.__enter__()

    def call(self, base, credential, name, arguments):
        async def go():
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}, timeout=30)
            async with http, Client(streamable_http_client(f"{base}/api/swarm/mcp", http_client=http)) as c:
                return await c.call_tool(name, arguments)
        return asyncio.run(go())

    def test_restart(self):
        engine, base = self.boot()
        owner = httpx2.Client(base_url=base, headers=self.headers("owner"), timeout=30)
        session_id = owner.post("/api/swarm/sessions", json={"policy": "mcp-demo"}).json()["sessionId"]
        credential = owner.post(f"/api/swarm/sessions/{session_id}/mcp/agents",
                                json={"agentId": "researcher", "scope": ["mcp:notes:*", "mcp:web:fetch"]}).json()["credential"]
        self.assertFalse(self.call(base, credential, "notes__fetch_page", {"url": "https://docs.example.org/"}).is_error)
        rows = owner.get(f"/api/swarm/sessions/{session_id}").json()["events"]
        owner.close()
        engine.__exit__(None, None, None)

        engine, base = self.boot()
        self.addCleanup(engine.__exit__, None, None, None)
        refused = self.call(base, credential, "notes__write_note", {"name": "n", "text": "t"})
        self.assertTrue(refused.is_error)
        self.assertIn("trust.contaminated_write", refused.content[0].text)
        self.assertFalse(self.call(base, credential, "notes__read_note", {"name": "welcome"}).is_error)
        with httpx2.Client(base_url=base, headers=self.headers("owner"), timeout=30) as owner:
            after = owner.get(f"/api/swarm/sessions/{session_id}").json()["events"]
        self.assertEqual(after[:len(rows)], rows)
        self.assertEqual([e["target"] for e in after[len(rows):]], ["mcp:notes:write", "mcp:notes:read"])


if __name__ == "__main__":
    unittest.main()
