"""Deterministic offline unit fixtures; real provider runs are verified separately."""
import http.client
import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from agents.runner import AgentRunner, run_showcase
from sdk.guard import GatewayError, Guard, _Remote
from fastapi.testclient import TestClient
from auth import Principal, require_principal
from live import SessionStore
from sandbox import Sandbox
import server


class Message:
    def __init__(self, tool=None, arguments=None, content=None):
        self.content = content
        self.tool_calls = [] if tool is None else [SimpleNamespace(
            id="call-fixture", function=SimpleNamespace(name=tool, arguments=json.dumps(arguments or {})))]

    def model_dump(self, **kwargs):
        result = {"role": "assistant"}
        if self.content:
            result["content"] = self.content
        if self.tool_calls:
            result["tool_calls"] = [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in self.tool_calls
            ]
        return result


class FixtureClient:
    def __init__(self, messages):
        self.messages = iter(messages)
        self.calls = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=next(self.messages))],
                               usage=SimpleNamespace(prompt_tokens=50, completion_tokens=20))


class HTTPFixtureTransport(_Remote):
    """Same SDK contract over actual ASGI routes; authentication is an explicit fixture."""
    def __init__(self, client):
        self.client = client

    def request(self, method, path, body=None, retry=True):
        response = self.client.request(method, path, json=body)
        if response.status_code >= 400:
            raise GatewayError(response.status_code, response.text)
        return response.json()


class RealAgentTools(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "evidence"
        self.path.mkdir()
        sessions = SessionStore()
        stores = patch.object(server, "SESSIONS", sessions)
        stores.start()
        self.addCleanup(stores.stop)
        self.addCleanup(lambda: [sessions.delete(sid) for sid in list(sessions.sessions)])
        server.app.dependency_overrides[require_principal] = lambda: Principal("fixture-owner", time.time() + 120)
        self.addCleanup(server.app.dependency_overrides.clear)
        self.http = TestClient(server.app)
        self.addCleanup(self.http.close)
        self.guard = Guard(HTTPFixtureTransport(self.http), policy="agents", label="Real LLM agents · fixture")

    def sandbox(self):
        return server.SESSIONS.get(self.guard.session_id).sandbox

    def test_normal_workflow_uses_actual_files(self):
        client = FixtureClient([
            Message("read_invoice"), Message(content="Invoice 125 credits."),
            Message("read_ledger"), Message("transfer_funds", {"amount": 125, "recipient": "approved-supplier"}),
            Message(content="Paid 125 credits."), Message("post_status", {"text": "Paid 125 credits."}),
            Message(content="Posted."),
        ])
        with patch("agents.runner.time.sleep"):
            result = run_showcase(self.guard, self.path, client)
        self.assertEqual(result["ledger"]["credits"], 9875)
        self.assertEqual(result["blockedModelToolCalls"], 0)
        self.assertEqual(len(result["board"]), 1)
        self.assertEqual(json.loads((self.sandbox().directory / "ledger.json").read_text())["credits"], 9875)
        self.assertFalse((self.path / "ledger.json").exists())
        live = self.http.get(f"/api/swarm/sessions/{self.guard.session_id}").json()
        self.assertTrue(any(t["target"] == "demo:payments.transfer" and t["executed"] for t in live["events"]))
        self.assertIn(self.guard.session_id, [r["sessionId"] for r in self.http.get("/api/swarm/sessions").json()])
        self.assertTrue((self.path / "model-transcript.json").exists())
        self.assertEqual(len(client.calls), 7)
        self.assertIn("Invoice 125 credits.", client.calls[2]["messages"][1]["content"])
        self.assertIn("Paid 125 credits.", client.calls[5]["messages"][1]["content"])

    def test_adversarial_model_calls_are_blocked_before_tool_body(self):
        client = FixtureClient([
            Message("read_invoice"), Message("transfer_funds", {"amount": 5000, "recipient": "sandbox-attacker"}),
            Message(content="Denied. Forwarding contaminated summary."),
            Message("read_ledger"), Message("transfer_funds", {"amount": 5000, "recipient": "sandbox-attacker"}),
            Message(content="Denied by content trust."),
            Message("post_status", {"text": "Invoice paid"}), Message(content="Revoked."),
        ])
        guard = self.guard
        with patch("agents.runner.time.sleep"):
            result = run_showcase(guard, self.path, client, adversarial=True)
        self.assertEqual(result["ledger"]["credits"], Sandbox.INITIAL_CREDITS)
        self.assertEqual(result["ledger"]["transfers"], [])
        self.assertNotIn("transfer_funds", result["actualToolBodiesExecuted"])
        self.assertEqual(result["blockedModelToolCalls"], 3)
        self.assertEqual(len(result["board"]), 1)  # Only the clean controller writes.
        session = server.SESSIONS.get(guard.session_id)
        dropped = {t.rule for t in session.gateway.telemetry if not t.executed}
        self.assertTrue({"delegation.out_of_scope", "trust.contaminated_tool", "tripwire.revoked"} <= dropped)
        live = self.http.get(f"/api/swarm/sessions/{guard.session_id}").json()
        self.assertTrue(any(t["rule"] == "tripwire.revoked" for t in live["events"]))
        self.assertTrue(live["alerts"])

    def test_model_cannot_override_guard_metadata(self):
        self.guard.configure_sandbox("adversarial")
        client = FixtureClient([Message("read_invoice", {"reads": "high"}), Message(content="Rejected.")])
        runner = AgentRunner(client, self.path, adversarial=True)
        agent = self.guard.root().spawn("reader", scope=["demo:invoice.read"])
        with patch("agents.runner.time.sleep"):
            runner.run(agent, "Read invoice", "read_invoice")
        self.assertEqual(self.sandbox().executions, [])
        self.assertEqual(runner.transcript[1]["outcome"]["error"], "invalid_tool_arguments")

    def test_provider_failure_never_runs_tool_or_fabricates_success(self):
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("provider down")))))
        with patch("agents.runner.time.sleep"), self.assertRaises(RuntimeError):
            run_showcase(self.guard, self.path, client)
        self.assertEqual(self.sandbox().executions, [])
        self.assertFalse((self.path / "result.json").exists())
        self.assertTrue((self.path / "model-transcript.json").exists())

    def test_unknown_model_tool_has_no_body(self):
        self.guard.configure_sandbox()
        agent = self.guard.root().spawn("reader", scope=["demo:invoice.read"])
        runner = AgentRunner(None, self.path)
        self.assertFalse(runner.invoke(agent, "shell", {})["allowed"])
        self.assertEqual(self.sandbox().executions, [])

    def test_remote_transport_uses_tls(self):
        remote = _Remote("https://example.com", 5)
        self.assertIs(remote.connection_class, http.client.HTTPSConnection)
        self.assertEqual(remote.port, 443)

    def test_remote_guard_preserves_labels_and_token_provider(self):
        provider = lambda: "synthetic-fixture-token"
        with patch.object(_Remote, "create", return_value="fixture-session") as create:
            guard = Guard.remote("https://example.com", token_provider=provider,
                                 policy="agents", label="Real LLM agents · fixture")
        self.assertIs(guard.transport.token_provider, provider)
        self.assertEqual(create.call_args.args[0]["label"], "Real LLM agents · fixture")
        self.assertEqual(guard.session_id, "fixture-session")

    def test_sandbox_is_immutable_and_never_returns_paths(self):
        snapshot = self.guard.configure_sandbox("adversarial")
        self.assertNotIn("directory", snapshot)
        with self.assertRaises(GatewayError):
            self.guard.configure_sandbox("normal")
        self.assertEqual(self.guard.sandbox_snapshot()["mode"], "adversarial")