"""Confidentiality labels and the egress monitor link (resolve identity and labels, record decisions)."""
import json
import os
import unittest
from unittest.mock import patch

from asp_policy import ASPPolicy
from live import SessionInput
from mcp_proxy import broker as mcp_broker
import test_persistence
from test_mcp_gateway import McpSampleHarness, sample_policy
from test_task_taint import Gateway

CREDENTIALS = {"classes": {"credentials": {"arguments": [r"secrets/"]}, "payments": {"tools": ["mock:payments.*"]}}}


def mock_policy(scope="actor"):
    data = ASPPolicy.load("mock").model_dump(by_alias=True)
    data["content-trust"]["taint-scope"] = scope
    data["swarm"]["tripwires"]["taint-agents"] = 0
    data["confidentiality"] = {"classes": {"credentials": {"arguments": [r"\.netrc"]}}}
    return ASPPolicy.model_validate(data)


class Labels(unittest.TestCase):
    def setUp(self):
        self.d = Gateway(mock_policy())
        self.d.admit("reader")
        self.d.admit("peer")

    def read(self, path, agent="reader", span="s1"):
        return self.d.ev(agent, "tool", "mock:fs.read", span, detail=path)

    def test_matching_reads_label_the_context_and_later_rows(self):
        self.assertEqual(self.read("/home/a/notes.txt").labels, [])
        self.assertEqual(self.read("/home/a/.netrc").labels, ["asp:credentials"])
        self.assertEqual(self.d.ev("reader", "tool", "mock:search.read", "s2").labels, ["asp:credentials"])

    def test_refused_calls_do_not_label(self):
        trace = self.d.ev("reader", "tool", "mock:shell.exec", "s1", detail="cat ~/.netrc")
        self.assertEqual((trace.decision, trace.labels), ("drop", []))

    def test_labels_follow_messages_and_children(self):
        self.read("~/.netrc")
        self.d.ev("reader", "message", "peer", "s1", mentions=["peer"])
        self.assertEqual(self.d.g.labels_for("peer"), {"credentials"})
        self.d.ev("reader", "spawn", "child", "s1")
        self.assertEqual(self.d.g.labels_for("child"), {"credentials"})

    def test_task_scope_starts_new_tasks_without_labels(self):
        d = Gateway(mock_policy(scope="task"))
        d.admit("reader")
        d.g.open_task("reader", "t1")
        d.ev("reader", "tool", "mock:fs.read", "s1", detail="~/.netrc")
        self.assertEqual(d.g.labels_for("reader"), {"credentials"})
        d.g.open_task("reader", "t2")
        self.assertEqual(d.g.labels_for("reader"), set())
        self.assertEqual(d.g.labels_for("reader", "t1"), {"credentials"})

    def test_no_confidentiality_section_means_no_labels(self):
        d = Gateway(ASPPolicy.load("mock"))
        d.admit("reader")
        self.assertEqual(d.ev("reader", "tool", "mock:fs.read", "s1", detail="~/.netrc").labels, [])


def labelled_policy():
    data = json.loads(sample_policy().model_dump_json(by_alias=True))
    data["confidentiality"] = CREDENTIALS
    return ASPPolicy.model_validate(data)


class EgressEndpoints(McpSampleHarness):
    SECRET = "test-egress-secret"

    def policy(self):
        return labelled_policy()

    def setUp(self):
        super().setUp()
        env = patch.dict(os.environ, {"SWARM_EGRESS_SECRET": self.SECRET})
        env.start()
        self.addCleanup(env.stop)

    def resolve(self, credential, secret=None):
        return self.http.post("/api/swarm/egress/resolve", json={"credential": credential},
                              headers={"X-Egress-Secret": self.SECRET if secret is None else secret})

    def test_disabled_without_a_secret_and_refuses_a_wrong_one(self):
        agent = self.mint("reader", ["mcp:files:*"])
        with patch.dict(os.environ, {"SWARM_EGRESS_SECRET": ""}):
            self.assertEqual(self.resolve(agent).status_code, 404)
        self.assertEqual(self.resolve(agent, secret="wrong").status_code, 401)
        self.assertEqual(self.resolve("ssa_" + "A" * 43).status_code, 404)

    def test_resolve_reports_labels_taint_and_revocation(self):
        agent = self.mint("reader", ["mcp:files:*", "mcp:web:get"])
        first = self.resolve(agent).json()
        self.assertEqual((first["agentId"], first["labels"], first["tainted"], first["revoked"]),
                         ("reader", [], False, False))
        self.call(agent, ("workspace__write_file", {"path": "secrets/token.txt", "content": "x"}),
                  ("workspace__read_file", {"path": "secrets/token.txt"}),
                  ("workspace__http_get", {"url": "https://docs.example.org/"}))
        after = self.resolve(agent).json()
        self.assertEqual((after["labels"], after["tainted"]), (["asp:credentials"], True))
        self.session.gateway.revoke(["reader"])
        self.assertTrue(self.resolve(agent).json()["revoked"])

    def test_events_are_recorded_as_egress_monitor_rows(self):
        self.mint("reader", ["mcp:files:*"])
        task = self.session.mcp.agents["reader"].spanId
        response = self.http.post("/api/swarm/egress/events", headers={"X-Egress-Secret": self.SECRET}, json=dict(
            sessionId=self.session.id, agentId="reader", taskId=task, verdict="block", applied="block", mode="enforce",
            destination="api.anthropic.com", category="public-api", classes=["credentials"],
            rules=["label:asp:credentials"], model="claude-x"))
        self.assertEqual(response.status_code, 201, response.text)
        row = self.session.gateway.telemetry[-1]
        self.assertEqual((row.id, row.decision, row.source, row.channel, row.target, row.executionProvenance, row.taskId),
                         (response.json()["eventId"], "observed", "egress", "out_of_band", "egress:api.anthropic.com",
                          "egress-monitor-reported", task))
        self.assertTrue(row.violations[-1].enforced)
        self.assertIn("credentials", row.violations[-1].directive)
        bad = self.http.post("/api/swarm/egress/events", headers={"X-Egress-Secret": self.SECRET}, json=dict(
            sessionId=self.session.id, agentId="nobody", verdict="allow", applied="allow", mode="report",
            destination="api.anthropic.com"))
        self.assertEqual(bad.status_code, 404)


class EgressDurability(unittest.TestCase):
    setUp = test_persistence.Durable.setUp
    restart = test_persistence.Durable.restart

    def test_egress_rows_and_labels_survive_a_restart(self):
        s = self.store.create(SessionInput(policy="mcp-demo"), owner="owner")
        s.mcp.mint("reader", "orchestrator", ["mcp:notes:*"])
        s.record_egress(agentId="reader", taskId=s.mcp.agents["reader"].spanId, verdict="hold", applied="allow",
                        mode="report", destination="api.openai.com", category="public-api", classes=["credentials"])
        before = [t.recorder_dump() for t in s.gateway.telemetry]
        restored = self.restart().get(s.id)
        self.assertEqual([t.recorder_dump() for t in restored.gateway.telemetry], before)
        self.assertEqual(restored.gateway.telemetry[-1].executionProvenance, "egress-monitor-reported")


if __name__ == "__main__":
    unittest.main()
