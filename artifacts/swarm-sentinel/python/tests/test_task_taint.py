"""Task-scoped taint (content-trust taint-scope "task"): taint per (agent, task), tasks opened only by a controller.

Covers the gateway rules directly, the MCP proxy end to end (one credential per task, rotation as the
controller's declassification step) and durability of rotation across a restart.
"""
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import persistence
from asp_gateway import ASPGateway
from asp_policy import ASPPolicy
from live import SessionInput
from mcp_proxy import broker as mcp_broker
from models import Event
from test_mcp_gateway import McpSampleHarness, sample_policy
import test_persistence

T0 = datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)


def policy(scope="task", propagate=True):
    data = ASPPolicy.load("mock").model_dump(by_alias=True)
    data["content-trust"]["taint-scope"] = scope
    data["content-trust"]["propagate-via-messages"] = propagate
    data["swarm"]["tripwires"]["taint-agents"] = 0  # isolate taint rules from revocation
    return ASPPolicy.model_validate(data)


class Gateway:
    """Thin driver: events with sensible defaults, one second apart."""

    def __init__(self, pol):
        self.g = ASPGateway(SimpleNamespace(feedbackEnabled=False), pol)
        self.n = 0

    def ev(self, agent, action, target, span, parent="orchestrator", parent_span="root", **extra):
        self.n += 1
        return self.g.evaluate_and_log(Event(
            id=f"e{self.n}", timestamp=(T0 + timedelta(seconds=self.n)).isoformat(), agentId=agent,
            parentId=parent, spanId=span, parentSpanId=parent_span, action=action, channel="in_band",
            target=target, intent=f"{action} {target}", intentVector=[], depth=0, **extra))

    def admit(self, agent, scope=None):
        trace = self.ev("orchestrator", "spawn", agent, "root", parent="", parent_span="", scope=scope or [])
        assert trace.decision == "allow", trace.rule


class TaskScopedGateway(unittest.TestCase):
    def setUp(self):
        self.d = Gateway(policy())
        self.d.admit("ledger")
        self.d.g.open_task("ledger", "task-1")

    def read_untrusted(self, span="s1"):
        return self.d.ev("ledger", "tool", "mock:fs.read", span, reads="untrusted")

    def pay(self, span):
        return self.d.ev("ledger", "tool", "mock:payments.transfer", span)

    def test_a_fresh_span_does_not_launder_taint_within_a_task(self):
        origin = self.read_untrusted().id
        trace = self.pay("invented-span")
        self.assertEqual((trace.decision, trace.rule, trace.taintOrigin, trace.taskId),
                         ("drop", "trust.contaminated_tool", origin, "task-1"))

    def test_actor_scope_without_mediation_is_laundered_by_a_fresh_span(self):
        """The gap task scope closes: in the default scope, span taint alone does not follow the agent."""
        d = Gateway(policy(scope="actor"))
        d.admit("ledger")
        d.ev("ledger", "tool", "mock:fs.read", "s1", reads="untrusted")
        self.assertEqual(d.ev("ledger", "tool", "mock:payments.transfer", "invented-span").decision, "allow")

    def test_a_controller_opened_task_starts_clean(self):
        self.read_untrusted()
        self.d.g.open_task("ledger", "task-2")
        trace = self.pay("s2")
        self.assertEqual((trace.decision, trace.taskId, trace.taintOrigin), ("allow", "task-2", ""))

    def test_agents_cannot_name_a_task_the_controller_did_not_open(self):
        self.read_untrusted()
        trace = self.d.ev("ledger", "tool", "mock:payments.transfer", "s3", taskId="task-made-up")
        self.assertEqual((trace.decision, trace.rule), ("drop", "lineage.task_unknown"))

    def test_an_explicit_older_task_keeps_its_taint(self):
        self.read_untrusted()
        self.d.g.open_task("ledger", "task-2")
        trace = self.d.ev("ledger", "tool", "mock:payments.transfer", "s4", taskId="task-1")
        self.assertEqual(trace.rule, "trust.contaminated_tool")

    def test_children_spawned_in_a_tainted_task_inherit_its_taint(self):
        self.read_untrusted()
        spawn = self.d.ev("ledger", "spawn", "helper", "s1", parent="orchestrator", parent_span="root")
        self.assertEqual(spawn.decision, "allow")
        child = self.d.ev("helper", "tool", "mock:payments.transfer", "h1", parent="ledger", parent_span="s1")
        self.assertEqual(child.rule, "trust.contaminated_tool")

    def test_children_spawned_in_a_clean_task_are_clean(self):
        self.read_untrusted()
        self.d.g.open_task("ledger", "task-2")
        self.d.ev("ledger", "spawn", "helper", "s5")
        child = self.d.ev("helper", "tool", "mock:payments.transfer", "h1", parent="ledger", parent_span="s5")
        self.assertEqual(child.decision, "allow")

    def test_messages_taint_the_recipients_current_task_only(self):
        self.d.admit("notifier")
        self.d.g.open_task("notifier", "n-1")
        self.read_untrusted()
        self.d.ev("ledger", "message", "notifier", "s1", mentions=["notifier"])
        self.assertEqual(self.d.ev("notifier", "tool", "mock:mcp:board.write", "n-span").rule, "trust.contaminated_write")
        self.d.g.open_task("notifier", "n-2")
        self.assertEqual(self.d.ev("notifier", "tool", "mock:mcp:board.write", "n-span-2").decision, "allow")

    def test_default_scope_is_actor_and_unchanged(self):
        self.assertEqual(ASPPolicy.load("mock").content_trust.taint_scope, "actor")


def task_policy():
    data = json.loads(sample_policy().model_dump_json(by_alias=True))
    data["content-trust"]["taint-scope"] = "task"
    return ASPPolicy.model_validate(data)


class TaskScopedMcp(McpSampleHarness):
    def policy(self):
        return task_policy()

    def rotate(self, agent):
        return self.http.post(f"/api/swarm/sessions/{self.session.id}/mcp/agents/{agent}/task")

    def test_taint_stays_with_the_credential_and_rotation_declassifies(self):
        old = self.mint("assistant", ["mcp:web:get", "mcp:mail:send"])
        self.assert_allowed(self.call(old, ("workspace__http_get", {"url": "https://docs.example.org/"}))[0])
        mail = ("workspace__send_email", {"to": "team@example.org", "subject": "s", "body": "b"})
        self.assert_refused(old, mail, "trust.contaminated_tool")
        response = self.rotate("assistant")
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(mcp_broker.lookup(old), None, "the retired credential must stop working")
        self.assertEqual(self.session.gateway.current_task["assistant"], body["taskId"])
        self.assert_allowed(self.call(body["credential"], mail)[0])
        self.assertEqual(self.session.gateway.telemetry[-1].taskId, body["taskId"])

    def test_children_minted_from_a_tainted_task_stay_tainted_after_rotation(self):
        researcher = self.mint("researcher", ["mcp:web:get", "mcp:files:*"])
        self.assert_allowed(self.call(researcher, ("workspace__http_get", {"url": "https://docs.example.org/"}))[0])
        child = self.mint("summarizer", ["mcp:files:*"], parent="researcher")
        self.assertEqual(self.rotate("researcher").status_code, 201)
        self.assert_refused(child, ("workspace__write_file", {"path": "s.txt", "content": "x"}), "trust.contaminated_write")

    def test_rotation_refuses_unknown_and_revoked_agents(self):
        self.assertEqual(self.rotate("nobody").status_code, 404)
        self.mint("worker", ["mcp:files:*"])
        self.session.gateway.revoke(["worker"])
        self.assertEqual(self.rotate("worker").status_code, 403)


class TaskRotationDurability(unittest.TestCase):
    setUp = test_persistence.Durable.setUp
    restart = test_persistence.Durable.restart

    def test_rotation_survives_a_restart(self):
        s = self.store.create(SessionInput(policy="mcp-demo"), owner="owner")
        _, old = s.mcp.mint("researcher", "orchestrator", ["mcp:notes:*"])
        task, new = s.mcp.rotate_task("researcher")
        store = self.restart()
        self.assertIsNone(mcp_broker.lookup(old))
        self.assertEqual(mcp_broker.lookup(new), (s.id, "researcher"))
        restored = store.get(s.id)
        self.assertEqual(restored.gateway.current_task["researcher"], task)
        self.assertEqual(restored.mcp.agents["researcher"].spanId, task)


if __name__ == "__main__":
    unittest.main()
