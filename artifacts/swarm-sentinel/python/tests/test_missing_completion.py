"""Overdue external receipts are visibility, not outcomes or detector inputs."""
import asyncio
import json
import unittest
from unittest.mock import patch

import auth
import server
from live import LiveSession, SessionInput, CallInput
import test_completion as completion
import test_engine as engine


class MissingCompletion(unittest.TestCase):
    token = completion.CooperativeCompletion.token
    headers = completion.CooperativeCompletion.headers
    setUpClass = completion.CooperativeCompletion.__dict__["setUpClass"]
    setUp = completion.CooperativeCompletion.setUp
    spawn = completion.CooperativeCompletion.spawn
    call = completion.CooperativeCompletion.call
    evaluate = completion.CooperativeCompletion.evaluate
    receipt = completion.CooperativeCompletion.receipt
    complete = completion.CooperativeCompletion.complete
    snapshot = completion.CooperativeCompletion.snapshot

    def test_pending_overdue_and_late_receipt_leave_admission_unchanged(self):
        self.session.spec.completionGraceSeconds = 10
        with patch("live.completion_clock", return_value=100):
            decision = self.evaluate(timestamp="2000-01-01T00:00:00Z")
        with patch("live.completion_clock", return_value=109.999):
            before = self.snapshot()
        self.assertEqual(before["completionWarnings"], [])
        self.assertEqual(before["completionGraceSeconds"], 10)
        with patch("live.completion_clock", return_value=110):
            overdue = self.snapshot()
        warning = overdue["completionWarnings"][0]
        self.assertEqual(warning["eventId"], decision["eventId"])
        self.assertEqual(warning["kind"], "missing-completion")
        self.assertNotEqual(warning["admittedAt"], overdue["events"][-1]["timestamp"])
        for field in ("events", "summary", "edges", "alerts", "policies"):
            self.assertEqual(before[field], overdue[field])
        row = overdue["events"][-1]
        for field in ("executionStatus", "executionProvenance", "toolBodyExecuted", "executionError"):
            self.assertNotIn(field, row)
        self.assertIn("Missing completion:", overdue["report"])
        self.assertIn("does not establish body entry, failure, rollback, or retry safety", overdue["report"])
        self.assertNotIn(decision["completionToken"], json.dumps(overdue))
        # Neither a forged receipt nor elapsed grace consumes the valid capability.
        self.assertEqual(self.complete(self.receipt(decision, completionToken="0" * 32)).status_code, 409)
        self.assertEqual(self.complete(self.receipt(decision, executionStatus="failed")).status_code, 200)
        after = self.snapshot()
        self.assertEqual(after["completionWarnings"], [])
        self.assertEqual(after["events"][-1]["executionProvenance"], "caller-reported")
        self.assertEqual(after["events"][-1]["executionStatus"], "failed")
        for field in ("summary", "edges", "alerts", "policies"):
            self.assertEqual(before[field], after[field])

    def test_warning_sse_without_new_events_late_receipt_and_reconnect(self):
        with patch("live.completion_clock", return_value=100):
            decision = self.evaluate()
        count = len(self.session.gateway.telemetry)
        principal = auth.Principal("owner", 9999999999)

        async def read(stream):
            return json.loads((await anext(stream)).split("data: ", 1)[1])

        async def verify():
            with patch("live.completion_clock", return_value=159):
                stream = (await server.stream_session(self.id, count, principal)).body_iterator
                pending = await read(stream)
            self.assertEqual(pending["completionWarnings"], [])
            with patch("live.completion_clock", return_value=160):
                overdue = await read(stream)
                reconnect = (await server.stream_session(self.id, count, principal)).body_iterator
                resumed = await read(reconnect)
            for message in (overdue, resumed):
                self.assertEqual(message["events"], [])
                self.assertEqual(message["updates"], [])
                self.assertEqual(message["total"], count)
                self.assertEqual(message["completionWarnings"][0]["eventId"], decision["eventId"])
                self.assertNotIn(decision["completionToken"], json.dumps(message))
            await reconnect.aclose()
            self.assertEqual(self.complete(self.receipt(decision)).status_code, 200)
            resolved = await read(stream)
            self.assertEqual(resolved["completionWarnings"], [])
            self.assertEqual(len(resolved["updates"]), 1)
            self.assertEqual(resolved["total"], count)
            await stream.aclose()
            reconnect = (await server.stream_session(self.id, count, principal)).body_iterator
            self.assertEqual((await read(reconnect))["completionWarnings"], [])
            await reconnect.aclose()
        asyncio.run(verify())

    def test_owner_isolation_and_closed_session(self):
        with patch("live.completion_clock", return_value=0):
            self.evaluate()
        with patch("live.completion_clock", return_value=60):
            self.assertEqual(len(self.snapshot()["completionWarnings"]), 1)
            self.assertEqual(self.client.get(self.path, headers=self.other).status_code, 404)
            self.assertEqual(self.client.get(self.path + "/stream", headers=self.other).status_code, 404)
            self.assertEqual(self.client.get(self.path).status_code, 401)
            other = self.client.post("/api/swarm/sessions", headers=self.other, json={}).json()["sessionId"]
            self.assertEqual(self.client.get(f"/api/swarm/sessions/{other}", headers=self.other).json()["completionWarnings"], [])
        self.session.close()
        self.assertEqual(self.session.completion_warnings(), [])
        self.assertEqual(self.session.completion_pending, {})

    def test_non_tool_denied_engine_observed_and_public_demo_never_warn(self):
        with patch("live.completion_clock", return_value=0):
            self.evaluate(agentId="reader", spanId="reader-span", target="demo:payments.transfer")
            self.evaluate(action="message", target="reporter")
            self.client.post(self.path + "/execute", headers=self.owner,
                             json=self.call(tool="read_ledger", arguments={}))
            demo = LiveSession(SessionInput())
            self.addCleanup(demo.close)
            demo.evaluate(CallInput(agentId="orchestrator", spanId="root",
                                    action="tool", target="ledger.read"))
        with patch("live.completion_clock", return_value=999999):
            self.assertEqual(self.snapshot()["completionWarnings"], [])
            self.assertEqual(demo.completion_warnings(), [])
            public = server._snapshot(demo, demo=True)
        self.assertNotIn("completionWarnings", public)
        self.assertNotIn("completionGraceSeconds", public)
        synthetic = self.client.post("/api/swarm/simulate", json={"scenario": "normal"}).json()
        self.assertNotIn("completionWarnings", synthetic)
        self.assertNotIn("completionGraceSeconds", synthetic)
        self.assertNotIn("Missing-completion warnings", synthetic["report"])

    def test_configurable_grace_is_validated_and_returned(self):
        for value in (0, -1, 86401, 1.5, "not-seconds", None):
            self.assertEqual(self.client.post("/api/swarm/sessions", headers=self.owner,
                json={"completionGraceSeconds": value}).status_code, 422)
        for value in (1, 300, 86400):
            response = self.client.post("/api/swarm/sessions", headers=self.owner,
                                       json={"completionGraceSeconds": value})
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["completionGraceSeconds"], value)
            snap = self.client.get("/api/swarm/sessions/" + response.json()["sessionId"],
                                   headers=self.owner).json()
            self.assertEqual(snap["completionGraceSeconds"], value)

    def test_historical_replay_keeps_counterfactual_semantics(self):
        events = [engine.village_event(1, "GPT-5", "village:bash", write=True)]
        gateway, sentinel = engine.village_replay(events)
        policy = gateway.policy
        window = dict(dataset="handwritten fixture", exportedAt="2026-06-02",
                      citation="test fixture", start=events[0]["timestamp"],
                      end=events[0]["timestamp"], truncated=False, day="2026-06-01",
                      regime="perma-computer-use")
        run = server._run("historical", "ai-village", "ai-village", server.VILLAGE,
                          gateway, sentinel, "ai-village", policy, engine.OFF,
                          events[0]["timestamp"], window)
        self.assertNotIn("completionWarnings", run)
        self.assertNotIn("completionGraceSeconds", run)
        self.assertNotIn("Missing-completion warnings", run["report"])
        self.assertIn("counterfactual, not evidence of prevention", run["report"])
        self.assertNotIn("executionStatus", run["events"][0])


if __name__ == "__main__":
    unittest.main()