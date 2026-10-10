"""Every ASP gateway check, reached through MCP proxy mode against the sample workspace server.

Each test drives a real MCP client over HTTP into the engine, which launches tests/mcp_sample/
workspace_server.py over stdio. Refusals are proven three ways: the client gets an error result
naming the rule, the recorder row says not-started (engine-observed), and the server's audit log
has no entry for the call. Tests follow ASP Algorithm 1 order (docs/mcp-proxy-design.md).
"""
import asyncio
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

import auth
import server
import test_access as access
from asp_policy import ASPPolicy
from live import LiveSession, SessionInput, SessionStore
from test_mcp_proxy import running_engine

TESTS = Path(__file__).parent


def sample_policy():
    text = (TESTS / "mcp_sample" / "workspace.asp.json").read_text().replace("{tests}", str(TESTS))
    return ASPPolicy.model_validate(json.loads(text))


class McpSampleHarness(unittest.TestCase):
    """Engine on a real port, one sample-workspace session per test. Subclasses may override policy()."""
    token = access.AccessControl.token
    headers = access.AccessControl.headers

    @classmethod
    def setUpClass(cls):
        access.AccessControl.setUpClass.__func__(cls)
        cls.patchers = [
            patch.object(auth, "auth_config", return_value=(access.ISSUER, {access.ORIGIN})),
            patch.object(auth, "key_client", return_value=SimpleNamespace(
                get_signing_key_from_jwt=lambda token: SimpleNamespace(key=cls.key.public_key()))),
            patch.multiple(server, SESSIONS=SessionStore()),
        ]
        for patcher in cls.patchers:
            patcher.start()
        cls.engine = running_engine()
        cls.base = cls.engine.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.engine.__exit__(None, None, None)
        for patcher in reversed(cls.patchers):
            patcher.stop()

    def policy(self):
        return sample_policy()

    def setUp(self):
        self.session = LiveSession(SessionInput(policy="mcp-demo", label="sample workspace"),
                                   owner="owner", policy=self.policy())
        server.SESSIONS.sessions[self.session.id] = self.session
        self.addCleanup(server.SESSIONS.delete, self.session.id)
        self.http = httpx2.Client(base_url=self.base, headers=self.headers("owner"), timeout=30)
        self.addCleanup(self.http.close)

    # Harness

    def mint(self, agent, scope, parent=""):
        response = self.http.post(f"/api/swarm/sessions/{self.session.id}/mcp/agents",
                                  json={"agentId": agent, "scope": scope, "parentId": parent})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["credential"]

    def refused_mint(self, agent, scope, parent=""):
        response = self.http.post(f"/api/swarm/sessions/{self.session.id}/mcp/agents",
                                  json={"agentId": agent, "scope": scope, "parentId": parent})
        self.assertEqual(response.status_code, 403, response.text)
        return response.json()["detail"]

    def call(self, credential, *calls):
        """One MCP client session per call list; returns the CallToolResults in order."""
        async def go():
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}, timeout=30)
            async with http, Client(streamable_http_client(f"{self.base}/api/swarm/mcp", http_client=http)) as c:
                return [await c.call_tool(name, arguments) for name, arguments in calls]
        return asyncio.run(go())

    def listed(self, credential):
        async def go():
            http = httpx2.AsyncClient(headers={"Authorization": f"Bearer {credential}"}, timeout=30)
            async with http, Client(streamable_http_client(f"{self.base}/api/swarm/mcp", http_client=http)) as c:
                return sorted(t.name for t in (await c.list_tools()).tools)
        return asyncio.run(go())

    def audit(self):
        path = self.session.mcp.pool.workspace / "workspace" / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def assert_allowed(self, result):
        self.assertFalse(result.is_error, result.content[0].text if result.content else "")
        trace = self.session.gateway.telemetry[-1]
        self.assertEqual((trace.decision, trace.executionStatus, trace.executionProvenance),
                         ("allow", "succeeded", "engine-observed"))

    def assert_refused(self, credential, call, rule, decision="drop"):
        before = self.audit()
        [result] = self.call(credential, call)
        self.assertTrue(result.is_error)
        self.assertIn(f"({rule})", result.content[0].text)
        self.assertEqual(self.audit(), before, "a refused call reached the server")
        if rule != "mcp.undeclared":
            trace = self.session.gateway.telemetry[-1]
            self.assertEqual((trace.rule, trace.decision, trace.executed, trace.executionStatus,
                              trace.toolBodyExecuted, trace.executionProvenance, trace.source),
                             (rule, decision, False, "not-started", False, "engine-observed", "mcp"))



class McpGatewayChecks(McpSampleHarness):
    # Admission and lineage

    def test_01_unknown_or_forged_credentials_never_reach_the_gateway(self):
        before = len(self.session.gateway.telemetry)
        for headers in ({}, {"Authorization": "Bearer ssa_forged"}, self.headers("owner")):
            with self.subTest(headers=list(headers)):
                response = httpx2.post(f"{self.base}/api/swarm/mcp", headers=headers,
                                       json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                             "params": {"name": "workspace__read_file", "arguments": {"path": "README.txt"}}})
                self.assertEqual(response.status_code, 401)
        self.assertEqual(len(self.session.gateway.telemetry), before)
        self.assertEqual(self.audit(), [])

    def test_02_delegation_depth_is_capped(self):
        self.mint("lead", ["mcp:files:*"])
        self.mint("worker", ["mcp:files:read"], parent="lead")
        self.assertIn("lineage.max_depth", self.refused_mint("intern", ["mcp:files:read"], parent="worker"))

    def test_03_sub_agents_cannot_widen_scope(self):
        self.mint("lead", ["mcp:files:read"])
        self.assertIn("delegation.scope_expansion", self.refused_mint("helper", ["mcp:web:get"], parent="lead"))
        self.assertIn("lineage.duplicate", self.refused_mint("lead", []))

    def test_04_sentinel_tripwire_revokes_agents_and_descendants(self):
        agents = {name: self.mint(name, ["mcp:tickets:*", "mcp:files:read"]) for name in ("ana", "ben", "cai")}
        helper = self.mint("ana-helper", ["mcp:files:read"], parent="ana")
        for name, credential in agents.items():
            self.assert_allowed(self.call(credential, ("workspace__post_ticket", {"title": f"triage from {name}"}))[0])
        alert = self.session.sentinel.alerts[-1]
        self.assertEqual((alert["kind"], alert["agents"]), ("shared_state_loop", ["ana", "ben", "cai"]))
        self.assert_refused(agents["ben"], ("workspace__read_file", {"path": "README.txt"}), "tripwire.revoked")
        self.assert_refused(helper, ("workspace__read_file", {"path": "README.txt"}), "tripwire.revoked")
        self.assertEqual(self.listed(agents["cai"]), [])

    def test_05_step_budget_throttles_a_runaway_agent(self):
        scraper = self.mint("scraper", ["mcp:files:read"])
        results = self.call(scraper, *[("workspace__read_file", {"path": f"page-{i}.txt"}) for i in range(10)])
        self.assertFalse(any(r.is_error for r in results))
        self.assert_refused(scraper, ("workspace__read_file", {"path": "page-10.txt"}), "budget.steps", "throttle")

    # Tool checks

    def test_06_explicit_deny_wins_over_the_allowlist(self):
        ops = self.mint("ops", [])
        self.assertNotIn("workspace__run_command", self.listed(ops))
        self.assert_refused(ops, ("workspace__run_command", {"command": "uname -a"}), "tools.deny")

    def test_07_default_deny_refuses_tools_outside_the_allowlist(self):
        ops = self.mint("ops", [])
        self.assertNotIn("workspace__delete_file", self.listed(ops))
        self.assert_refused(ops, ("workspace__delete_file", {"path": "README.txt"}), "tools.default_deny")
        self.assertEqual(self.call(ops, ("workspace__read_file", {"path": "README.txt"}))[0].content[0].text,
                         "Shared workspace for the sample team. Fictional data.")

    def test_08_tools_outside_delegated_scope_are_refused(self):
        reader = self.mint("reader", ["mcp:files:read"])
        self.assertEqual(self.listed(reader), ["workspace__read_file"])
        self.assert_refused(reader, ("workspace__write_file", {"path": "notes__txt", "content": "x"}),
                            "delegation.out_of_scope")

    def test_09_argument_constraints(self):
        writer = self.mint("writer", ["mcp:files:*", "mcp:mail:*"])
        for call in [("workspace__write_file", {"path": "../escape.txt", "content": "x"}),
                     ("workspace__read_file", {"path": "config/.env"}),
                     ("workspace__read_file", {"path": "home/id_rsa"}),
                     ("workspace__send_email", {"to": "someone@evil.example.com", "subject": "hi", "body": "x"}),
                     ("workspace__send_email", {"to": "a@example.org.evil.com", "subject": "hi", "body": "x"})]:
            with self.subTest(arguments=call[1]):
                self.assert_refused(writer, call, "tools.deny_arguments")
        self.assert_allowed(self.call(writer, ("workspace__send_email",
                                              {"to": "team@example.org", "subject": "hi", "body": "status"}))[0])

    def test_10_network_scope(self):
        browser = self.mint("browser", ["mcp:web:get"])
        for url in ("https://evil.example.com/x", "evil.example.com/x", "https://example.org.evil.com/"):
            with self.subTest(url=url):
                self.assert_refused(browser, ("workspace__http_get", {"url": url}), "network.default_deny")
        self.assert_allowed(self.call(browser, ("workspace__http_get", {"url": "https://wiki.example.org/page"}))[0])
        self.assertEqual(self.session.gateway.telemetry[-1].network, "wiki.example.org")

    # Content trust

    def test_11_untrusted_read_withdraws_listed_capabilities(self):
        assistant = self.mint("assistant", ["mcp:web:get", "mcp:mail:send"])
        self.assert_allowed(self.call(assistant, ("workspace__http_get", {"url": "https://docs.example.org/"}))[0])
        origin = self.session.gateway.telemetry[-1].id
        self.assert_refused(assistant, ("workspace__send_email", {"to": "team@example.org", "subject": "s", "body": "b"}),
                            "trust.contaminated_tool")
        self.assertEqual(self.session.gateway.telemetry[-1].taintOrigin, origin)

    def test_12_untrusted_read_blocks_writes_for_the_agent_and_later_children(self):
        researcher = self.mint("researcher", ["mcp:web:get", "mcp:files:*"])
        clean = self.mint("clean", ["mcp:files:*"])
        self.assert_allowed(self.call(researcher, ("workspace__http_get", {"url": "https://docs.example.org/"}))[0])
        self.assert_refused(researcher, ("workspace__write_file", {"path": "summary.txt", "content": "from the page"}),
                            "trust.contaminated_write")
        child = self.mint("summarizer", ["mcp:files:*"], parent="researcher")
        self.assert_refused(child, ("workspace__write_file", {"path": "summary.txt", "content": "fresh span"}),
                            "trust.contaminated_write")
        self.assert_allowed(self.call(clean, ("workspace__write_file", {"path": "plan.txt", "content": "clean"}))[0])

    # Swarm rate checks

    def test_13_write_cap_per_shared_resource(self):
        writer = self.mint("writer", ["mcp:files:*"])
        for i in range(3):
            self.assert_allowed(self.call(writer, ("workspace__write_file", {"path": f"draft-{i}.txt", "content": "v"}))[0])
        self.assert_refused(writer, ("workspace__write_file", {"path": "draft-3.txt", "content": "v"}), "tools.write_cap")

    def test_14_repeated_identical_intent_across_agents_is_throttled(self):
        credentials = [self.mint(name, ["mcp:files:read"]) for name in ("a1", "a2", "a3")]
        for credential in credentials[:2]:
            self.assert_allowed(self.call(credential, ("workspace__read_file", {"path": "README.txt"}))[0])
        self.assert_refused(credentials[2], ("workspace__read_file", {"path": "README.txt"}),
                            "semantic.identical_intent", "throttle")
        self.assert_allowed(self.call(credentials[2], ("workspace__read_file", {"path": "other.txt"}))[0])

    # Proxy-specific

    def test_15_undeclared_server_tools_are_hidden_and_never_dispatched(self):
        ops = self.mint("ops", [])
        self.assertEqual(self.listed(ops), ["workspace__http_get", "workspace__post_ticket", "workspace__read_file",
                                            "workspace__send_email", "workspace__write_file"])
        before = len(self.session.gateway.telemetry)
        self.assert_refused(ops, ("workspace__drop_database", {}), "mcp.undeclared")
        self.assert_refused(ops, ("drop_database", {}), "mcp.undeclared")
        self.assertEqual(len(self.session.gateway.telemetry), before)

    def test_16_allowed_calls_reach_the_server_with_engine_evidence(self):
        writer = self.mint("writer", ["mcp:files:*"])
        saved, read = self.call(writer, ("workspace__write_file", {"path": "plan.txt", "content": "ship friday"}),
                               ("workspace__read_file", {"path": "plan.txt"}))
        self.assertFalse(saved.is_error)
        self.assertEqual(read.content[0].text, "ship friday")
        self.assertEqual(self.audit(), [{"tool": "write_file", "path": "plan.txt"}, {"tool": "read_file", "path": "plan.txt"}])
        rows = [t for t in self.session.gateway.telemetry if t.action == "tool"]
        self.assertEqual([(t.agentId, t.target, t.write, t.resource, t.executionStatus) for t in rows],
                         [("writer", "mcp:files:write", True, "mcp:files", "succeeded"),
                          ("writer", "mcp:files:read", False, "", "succeeded")])
        # Proxied calls feed the Sentinel graph exactly like SDK calls.
        edges = {(e["source"], e["target"]): e["actions"] for e in self.session.sentinel.weighted_edges()}
        self.assertEqual(edges, {("orchestrator", "writer"): {"spawn": 1},
                                 ("writer", "mcp:files:write"): {"tool": 1},
                                 ("writer", "mcp:files:read"): {"tool": 1}})

    def test_17_downstream_failure_is_recorded_after_body_entry(self):
        writer = self.mint("writer", ["mcp:files:*"])
        # The policy's path rule only matches "../", so an absolute path is admitted. The server's own
        # guard rejects it: the gateway judges arguments by the rules it is given, nothing more.
        [result] = self.call(writer, ("workspace__read_file", {"path": "/etc/hosts"}))
        self.assertTrue(result.is_error)
        trace = self.session.gateway.telemetry[-1]
        self.assertEqual((trace.decision, trace.toolBodyExecuted, trace.executionStatus, trace.executionError),
                         ("allow", True, "failed", "tool_execution_failed"))
        self.assertEqual(self.audit()[-1], {"tool": "read_file", "path": "/etc/hosts"})


if __name__ == "__main__":
    unittest.main()
