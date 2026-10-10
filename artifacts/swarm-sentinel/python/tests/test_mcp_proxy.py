"""MCP proxy mode (docs/mcp-proxy.md): engine-issued identity, policy-declared semantics, refused calls never dispatch."""
import asyncio
import json
import socket
import threading
import time
import unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import auth
import server
import test_access as access
from asp_policy import ASPPolicy
from live import LiveSession, SessionInput, SessionStore
from mcp_proxy.broker import lookup


def session(**changes):
    return LiveSession(SessionInput(policy="mcp-demo", **changes), owner="owner")


class McpPolicy(unittest.TestCase):
    def test_existing_declarations_are_unchanged(self):
        for name in ("mock", "ai-village", "agents"):
            with self.subTest(policy=name):
                self.assertIsNone(ASPPolicy.load(name).mcp)
                self.assertNotIn("mcp", ASPPolicy.load(name).declaration())

    def test_demo_policy_declares_semantics(self):
        tools = ASPPolicy.load("mcp-demo").mcp.servers["notes"].tools
        self.assertEqual(set(tools), {"read_note", "write_note", "fetch_page"})
        self.assertEqual(tools["fetch_page"].reads, "untrusted")
        self.assertEqual(tools["fetch_page"].network_argument, "url")
        self.assertTrue(tools["write_note"].write)

    def test_sessions_without_mcp_have_no_broker(self):
        self.assertIsNone(LiveSession(SessionInput(policy="agents")).mcp)


class McpBroker(unittest.TestCase):
    def setUp(self):
        self.s = session()
        self.addCleanup(self.s.close)
        self.broker = self.s.mcp
        _, self.writer = self.broker.mint("writer", "orchestrator", ["mcp:notes:*"])
        _, self.researcher = self.broker.mint("researcher", "orchestrator", ["mcp:web:fetch", "mcp:notes:*"])

    def admit(self, agent, name, arguments):
        admission = self.broker.admit(agent, name, arguments)
        if admission.allowed:
            self.broker.record(admission, "succeeded")
        return admission

    def test_minting_is_a_gateway_spawn_with_engine_issued_identity(self):
        self.assertEqual(lookup(self.writer), (self.s.id, "writer"))
        spawn = self.s.gateway.telemetry[0]
        self.assertEqual((spawn.action, spawn.target, spawn.source, spawn.decision), ("spawn", "writer", "mcp", "allow"))
        self.assertIsNone(lookup("ssa_forged"))
        self.assertIsNone(lookup(self.writer + "x"))

    def test_child_cannot_widen_scope_or_name_a_foreign_parent(self):
        decision, credential = self.broker.mint("helper", "writer", ["mcp:web:fetch"])
        self.assertIsNone(credential)
        self.assertEqual(decision["rule"], "delegation.scope_expansion")
        with self.assertRaises(LookupError):
            self.broker.mint("orphan", "not-minted", [])
        with self.assertRaises(ValueError):
            self.broker.mint("bad id!", "orchestrator", [])

    def test_allowed_call_is_engine_observed(self):
        admission = self.admit("writer", "notes__write_note", {"name": "plan", "text": "ship it"})
        self.assertTrue(admission.allowed)
        trace = admission.trace
        self.assertEqual((trace.target, trace.write, trace.resource, trace.source),
                         ("mcp:notes:write", True, "mcp:notes", "mcp"))
        self.assertEqual((trace.executionStatus, trace.toolBodyExecuted, trace.executionProvenance),
                         ("succeeded", True, "engine-observed"))
        self.assertEqual(trace.intent, 'notes__write_note {"name": "plan", "text": "ship it"}')

    def test_refusals_are_recorded_as_not_started(self):
        cases = [
            ("writer", "notes__write_note", {"name": "k", "text": "-----BEGIN RSA PRIVATE KEY-----"}, "tools.deny_arguments"),
            ("writer", "notes__fetch_page", {"url": "https://docs.example.org/a"}, "delegation.out_of_scope"),
            ("researcher", "notes__fetch_page", {"url": "https://evil.example.com/a"}, "network.default_deny"),
            ("researcher", "notes__fetch_page", {"url": "evil.example.com/a"}, "network.default_deny"),
        ]
        for agent, name, arguments, rule in cases:
            with self.subTest(rule=rule, arguments=arguments):
                admission = self.admit(agent, name, arguments)
                self.assertFalse(admission.allowed)
                self.assertEqual(admission.decision["rule"], rule)
                self.assertEqual((admission.trace.executionStatus, admission.trace.toolBodyExecuted,
                                  admission.trace.executionProvenance), ("not-started", False, "engine-observed"))

    def test_untrusted_read_withdraws_writes_for_the_actor(self):
        self.assertTrue(self.admit("researcher", "notes__fetch_page", {"url": "https://docs.example.org/a"}).allowed)
        refused = self.admit("researcher", "notes__write_note", {"name": "summary", "text": "from the page"})
        self.assertEqual(refused.decision["rule"], "trust.contaminated_write")
        self.assertTrue(self.admit("writer", "notes__write_note", {"name": "plan", "text": "clean"}).allowed)

    def test_undeclared_and_oversized_calls_are_refused_before_the_gateway(self):
        before = len(self.s.gateway.telemetry)
        for name, arguments, rule in [("notes__delete_all", {}, "mcp.undeclared"),
                                      ("delete_all", {}, "mcp.undeclared"),
                                      ("notes__write_note", {"name": "big", "text": "x" * 5000}, "mcp.arguments_too_large")]:
            with self.subTest(name=name):
                admission = self.broker.admit("writer", name, arguments)
                self.assertFalse(admission.allowed)
                self.assertEqual(admission.decision["rule"], rule)
        self.assertEqual(len(self.s.gateway.telemetry), before)

    def test_visible_tools_follow_scope_and_revocation(self):
        self.assertEqual(self.broker.visible("writer"), ["notes__read_note", "notes__write_note"])
        self.assertEqual(self.broker.visible("researcher"), ["notes__read_note", "notes__write_note", "notes__fetch_page"])
        self.s.gateway.revoke(["writer"])
        self.assertEqual(self.broker.visible("writer"), [])
        self.assertEqual(self.admit("writer", "notes__read_note", {"name": "welcome"}).decision["rule"], "tripwire.revoked")

    def test_closing_the_session_revokes_credentials(self):
        self.s.close()
        self.assertIsNone(lookup(self.writer))
        self.assertIsNone(lookup(self.researcher))


@contextmanager
def running_engine():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    engine = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=engine.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not engine.started:
        if time.monotonic() > deadline:
            raise RuntimeError("engine did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        engine.should_exit = True
        thread.join(10)


class McpEndToEnd(unittest.TestCase):
    """A real MCP client, the engine over HTTP, and the stdio fixture server launched by the engine."""

    token = access.AccessControl.token
    headers = access.AccessControl.headers

    @classmethod
    def setUpClass(cls):
        access.AccessControl.setUpClass.__func__(cls)

    def setUp(self):
        for patcher in (patch.object(auth, "auth_config", return_value=(access.ISSUER, {access.ORIGIN})),
                        patch.object(auth, "key_client", return_value=SimpleNamespace(
                            get_signing_key_from_jwt=lambda token: SimpleNamespace(key=self.key.public_key()))),
                        patch.multiple(server, SESSIONS=SessionStore())):
            patcher.start()
            self.addCleanup(patcher.stop)
        engine = running_engine()
        self.base = engine.__enter__()
        self.addCleanup(engine.__exit__, None, None, None)
        self.http = httpx2.Client(base_url=self.base, headers=self.headers("owner"), timeout=30)
        self.addCleanup(self.http.close)
        created = self.http.post("/api/swarm/sessions", json={"policy": "mcp-demo"})
        self.assertEqual(created.status_code, 201, created.text)
        self.id = created.json()["sessionId"]
        self.session = server.SESSIONS.get(self.id)

    def mint(self, agent, scope, parent=""):
        response = self.http.post(f"/api/swarm/sessions/{self.id}/mcp/agents",
                                  json={"agentId": agent, "scope": scope, "parentId": parent})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["credential"]

    def calls(self):
        path = self.session.mcp.pool.workspace / "notes" / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def agent(self, credential, steps):
        http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}, timeout=30)
        async with http, Client(streamable_http_client(f"{self.base}/api/swarm/mcp", http_client=http)) as client:
            return await steps(client)

    def run_agent(self, credential, steps):
        return asyncio.run(self.agent(credential, steps))

    def test_missing_or_forged_credentials_are_rejected(self):
        for headers in ({}, {"Authorization": "Bearer ssa_forged"}, self.headers("owner")):
            with self.subTest(headers=list(headers)):
                response = httpx2.post(f"{self.base}/api/swarm/mcp", headers=headers,
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                self.assertEqual(response.status_code, 401)

    def test_minting_requires_the_owner_and_an_mcp_policy(self):
        response = httpx2.post(f"{self.base}/api/swarm/sessions/{self.id}/mcp/agents",
                               headers=self.headers("other"), json={"agentId": "writer"})
        self.assertEqual(response.status_code, 404)
        plain = self.http.post("/api/swarm/sessions", json={"policy": "agents"}).json()["sessionId"]
        response = self.http.post(f"/api/swarm/sessions/{plain}/mcp/agents", json={"agentId": "writer"})
        self.assertEqual(response.status_code, 409)

    def test_proxy_enforces_and_dispatches(self):
        writer = self.mint("writer", ["mcp:notes:*"])

        async def steps(client):
            listed = sorted(t.name for t in (await client.list_tools()).tools)
            saved = await client.call_tool("notes__write_note", {"name": "plan", "text": "ship it"})
            secret = await client.call_tool("notes__write_note", {"name": "k", "text": "-----BEGIN PRIVATE KEY-----"})
            hidden = await client.call_tool("notes__delete_all", {})
            fetch = await client.call_tool("notes__fetch_page", {"url": "https://docs.example.org/"})
            read = await client.call_tool("notes__read_note", {"name": "plan"})
            return listed, saved, secret, hidden, fetch, read

        listed, saved, secret, hidden, fetch, read = self.run_agent(writer, steps)
        self.assertEqual(listed, ["notes__read_note", "notes__write_note"])
        self.assertFalse(saved.is_error)
        self.assertEqual(read.content[0].text, "ship it")
        for refused, rule in ((secret, "tools.deny_arguments"), (hidden, "mcp.undeclared"),
                              (fetch, "delegation.out_of_scope")):
            with self.subTest(rule=rule):
                self.assertTrue(refused.is_error)
                self.assertIn(rule, refused.content[0].text)
        # Only the two allowed bodies reached the server process.
        self.assertEqual([c["tool"] for c in self.calls()], ["write_note", "read_note"])
        rows = [t for t in self.session.gateway.telemetry if t.action == "tool"]
        self.assertEqual([(t.target, t.decision, t.executionStatus) for t in rows], [
            ("mcp:notes:write", "allow", "succeeded"),
            ("mcp:notes:write", "drop", "not-started"),
            ("mcp:web:fetch", "drop", "not-started"),
            ("mcp:notes:read", "allow", "succeeded"),
        ])
        self.assertTrue(all(t.executionProvenance == "engine-observed" and t.source == "mcp" for t in rows))

    def test_injection_path_is_cut_between_read_and_write(self):
        researcher = self.mint("researcher", ["mcp:web:fetch", "mcp:notes:*"])

        async def steps(client):
            page = await client.call_tool("notes__fetch_page", {"url": "https://docs.example.org/guide"})
            write = await client.call_tool("notes__write_note", {"name": "summary", "text": "from the page"})
            return page, write

        page, write = self.run_agent(researcher, steps)
        self.assertFalse(page.is_error)
        self.assertTrue(write.is_error)
        self.assertIn("trust.contaminated_write", write.content[0].text)
        self.assertEqual([c["tool"] for c in self.calls()], ["fetch_page"])

    def test_downstream_errors_are_recorded_as_failed(self):
        writer = self.mint("writer", ["mcp:notes:*"])
        result = self.run_agent(writer, lambda client: client.call_tool("notes__read_note", {"name": "Not Valid"}))
        self.assertTrue(result.is_error)
        trace = self.session.gateway.telemetry[-1]
        self.assertEqual((trace.decision, trace.toolBodyExecuted, trace.executionStatus, trace.executionError),
                         ("allow", True, "failed", "tool_execution_failed"))

    def test_deleted_session_stops_the_credential_and_the_server(self):
        writer = self.mint("writer", ["mcp:notes:*"])
        self.run_agent(writer, lambda client: client.call_tool("notes__read_note", {"name": "welcome"}))
        workspace = self.session.mcp.pool.workspace
        self.assertTrue(workspace.exists())
        self.assertEqual(self.http.delete(f"/api/swarm/sessions/{self.id}").status_code, 204)
        response = httpx2.post(f"{self.base}/api/swarm/mcp", headers={"Authorization": f"Bearer {writer}"},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertEqual(response.status_code, 401)
        deadline = time.monotonic() + 10
        while workspace.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(workspace.exists())


if __name__ == "__main__":
    unittest.main()
