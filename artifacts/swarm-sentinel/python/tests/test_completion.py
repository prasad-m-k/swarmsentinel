"""Owner-bound cooperative receipts: snapshots, live row updates and SDK behavior."""
import asyncio
import json
import unittest
from unittest.mock import Mock, patch

import auth
import server
from sdk.guard import Agent, CompletionReportError, Guard, PolicyViolation, _Local, _Remote
import test_execution as execution


class CooperativeCompletion(unittest.TestCase):
    token = execution.GatewayExecution.token
    headers = execution.GatewayExecution.headers
    setUpClass = execution.GatewayExecution.__dict__["setUpClass"]
    setUp = execution.GatewayExecution.setUp
    spawn = execution.GatewayExecution.spawn
    call = execution.GatewayExecution.call

    def evaluate(self, **changes):
        body = dict(agentId="reviewer", spanId="reviewer-span",
                    parentId="orchestrator", parentSpanId="root-span",
                    action="tool", target="demo:ledger.read", intent="inspect ledger")
        body.update(changes)
        response = self.client.post(self.path + "/evaluate", headers=self.owner, json=body)
        self.assertEqual(response.status_code, 200)
        return response.json()

    def receipt(self, decision, **changes):
        body = dict(eventId=decision["eventId"], completionToken=decision.get("completionToken", "0" * 32),
                    agentId="reviewer", spanId="reviewer-span", executionStatus="succeeded")
        body.update(changes)
        return body

    def complete(self, body, headers=None, path=None):
        return self.client.post((path or self.path) + "/completion", json=body,
                                headers=self.owner if headers is None else headers)

    def snapshot(self):
        return self.client.get(self.path, headers=self.owner).json()

    def test_snapshot_failure_sanitization_and_admission_accounting(self):
        decision = self.evaluate()
        before = self.snapshot()
        self.assertNotIn("executionStatus", before["events"][-1])
        self.assertNotIn(decision["completionToken"], json.dumps(before))
        response = self.complete(self.receipt(decision, executionStatus="failed"))
        self.assertEqual(response.status_code, 200)
        row = response.json()
        self.assertEqual(row["executionStatus"], "failed")
        self.assertTrue(row["toolBodyExecuted"])
        self.assertTrue(row["executed"])
        self.assertEqual(row["executionProvenance"], "caller-reported")
        self.assertEqual(row["executionError"], "tool_execution_failed")
        after = self.snapshot()
        self.assertEqual(after["events"][-1], row)
        self.assertEqual(after["summary"], before["summary"])
        self.assertEqual(after["edges"], before["edges"])
        self.assertEqual(after["alerts"], before["alerts"])
        self.assertEqual(after["policies"], before["policies"])
        self.assertEqual(len(after["events"]), len(before["events"]))
        self.assertIn("- caller-reported: 1", after["report"])
        self.assertIn("owner assertions, not engine verification", after["report"])
        self.assertNotIn(decision["completionToken"], json.dumps(after))
        self.assertEqual(self.complete(self.receipt(decision, executionStatus="failed")).status_code, 409)
        self.assertEqual(self.complete(self.receipt(decision)).status_code, 409)
        self.assertEqual(self.snapshot(), after)

    def test_owner_auth_session_event_and_actor_binding(self):
        decision = self.evaluate()
        receipt = self.receipt(decision)
        for headers, status in (({}, 401), (self.other, 404), (self.headers(exp=1), 401)):
            self.assertEqual(self.complete(receipt, headers=headers).status_code, status)
        with patch.object(server, "still_valid", return_value=False):
            self.assertEqual(self.complete(receipt).status_code, 401)
        for key, value in (("agentId", "reader"), ("spanId", "other-span"),
                           ("completionToken", "0" * 32)):
            self.assertEqual(self.complete({**receipt, key: value}).status_code, 409)
        self.assertEqual(self.complete({**receipt, "eventId": "missing"}).status_code, 404)
        other_id = self.client.post("/api/swarm/sessions", headers=self.owner,
                                    json={"policy": "agents"}).json()["sessionId"]
        other = server.SESSIONS.get(other_id)
        # Identical actor/span and sequential event IDs in another session must
        # still not accept this session's token.
        other.evaluate(server.CallInput(agentId="orchestrator", spanId="root-span", action="spawn",
                                        target="reviewer", scope=["demo:ledger.read"]))
        for _ in range(3):
            foreign = other.evaluate(server.CallInput(agentId="reviewer", spanId="reviewer-span",
                parentId="orchestrator", parentSpanId="root-span", action="tool", target="demo:ledger.read"))
        self.assertEqual(foreign["eventId"], decision["eventId"])
        self.assertEqual(self.complete(receipt, path=f"/api/swarm/sessions/{other_id}").status_code, 409)
        self.assertEqual(self.complete(receipt).status_code, 200)
        server.SESSIONS.delete(self.id)
        self.assertEqual(self.complete(receipt).status_code, 404)

    def test_denied_non_tool_and_engine_observed_receipts_cannot_be_overwritten(self):
        denied = self.evaluate(agentId="reader", spanId="reader-span", target="demo:payments.transfer")
        self.assertFalse(denied["allowed"])
        self.assertNotIn("completionToken", denied)
        self.assertEqual(self.complete(self.receipt(denied)).status_code, 409)
        spawned = self.evaluate(agentId="orchestrator", spanId="root-span", parentId="", parentSpanId="",
                                action="spawn", target="extra", scope=["demo:ledger.read"])
        self.assertEqual(self.complete(self.receipt(spawned)).status_code, 409)
        message = self.evaluate(action="message", target="reporter")
        self.assertEqual(self.complete(self.receipt(message)).status_code, 409)
        engine = self.client.post(self.path + "/execute", headers=self.owner,
                                 json=self.call(tool="read_ledger", arguments={})).json()["decision"]
        self.assertNotIn("completionToken", engine)
        self.assertEqual(self.complete(self.receipt(engine)).status_code, 409)
        self.assertEqual(self.snapshot()["events"][-1]["executionProvenance"], "engine-observed")

    def test_invalid_receipts_do_not_echo_or_store_secret_fields(self):
        decision = self.evaluate()
        before = self.snapshot()
        secret = "fixture-secret /private/path credential result exception"
        for field in ("error", "result", "executionError", "executionProvenance", "toolBodyExecuted"):
            response = self.complete(self.receipt(decision, **{field: secret}))
            self.assertEqual(response.status_code, 422)
            self.assertNotIn(secret, response.text)
            self.assertNotIn(decision["completionToken"], response.text)
        for status in ("not-started", "running", None, True):
            self.assertEqual(self.complete(self.receipt(decision, executionStatus=status)).status_code, 422)
        self.assertEqual(self.snapshot(), before)

    def test_concurrent_receipts_accept_exactly_one_terminal_update(self):
        import concurrent.futures
        decision = self.evaluate()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(self.complete, self.receipt(decision, executionStatus=status))
                       for status in ("succeeded", "failed")]
            self.assertEqual(sorted(f.result().status_code for f in futures), [200, 409])
        self.assertEqual(len(self.snapshot()["events"]), 4)

    def test_sse_updates_existing_rows_snapshot_race_and_reconnect(self):
        decision = self.evaluate()
        count = len(self.session.gateway.telemetry)
        principal = auth.Principal("owner", 9999999999)

        async def verify():
            stream = (await server.stream_session(self.id, 0, principal)).body_iterator
            first = json.loads((await anext(stream)).split("data: ", 1)[1])
            self.assertEqual(len(first["events"]), count)
            self.assertEqual(first["updates"], [])
            self.assertEqual(self.complete(self.receipt(decision)).status_code, 200)
            update = json.loads((await anext(stream)).split("data: ", 1)[1])
            self.assertEqual(update["events"], [])
            self.assertEqual(update["updates"], [self.snapshot()["events"][-1]])
            self.assertEqual(update["total"], count)
            await stream.aclose()
            # The completion may occur between the snapshot and subscribe:
            # initial updates resend known evidence even at a caught-up cursor.
            reconnect = (await server.stream_session(self.id, count, principal)).body_iterator
            resumed = json.loads((await anext(reconnect)).split("data: ", 1)[1])
            self.assertEqual(resumed["events"], [])
            self.assertEqual(resumed["updates"], update["updates"])
            self.assertEqual(resumed["total"], count)
            self.assertNotIn(decision["completionToken"], json.dumps(resumed))
            await reconnect.aclose()
        asyncio.run(verify())

    def actor(self):
        guard = Guard(_Local(server.SESSIONS), session_id=self.id)
        root = guard.root()
        root.span = "root-span"
        actor = Agent(guard, "reviewer", root)
        actor.span = "reviewer-span"
        return actor

    def test_sdk_success_failure_refusal_and_manual_check_receipt(self):
        actor = self.actor()
        result = {"credential": "fixture-secret", "path": "/private/path"}
        body = Mock(return_value=result)
        body.__name__ = "external_read"
        self.assertIs(actor.call("demo:ledger.read", body, intent="inspect ledger"), result)
        self.assertEqual(self.snapshot()["events"][-1]["executionStatus"], "succeeded")
        error = ValueError("fixture-secret /private/path")
        body = Mock(side_effect=error)
        body.__name__ = "external_failure"
        with self.assertRaises(ValueError) as raised:
            actor.call("demo:ledger.read", body, intent="inspect another ledger")
        self.assertIs(raised.exception, error)
        exported = json.dumps(self.snapshot())
        self.assertNotIn("fixture-secret", exported)
        self.assertNotIn("/private/path", exported)
        self.assertEqual(self.snapshot()["events"][-1]["executionStatus"], "failed")
        decision = actor.check("tool", "demo:ledger.read", "inspect third ledger")
        self.assertNotIn("executionStatus", self.snapshot()["events"][-1])
        actor.complete(decision, "succeeded")
        with self.assertRaises(CompletionReportError):
            actor.complete(decision, "succeeded")
        actor.name, actor.span = "reader", "reader-span"
        refused_body = Mock()
        refused_body.__name__ = "refused_transfer"
        with self.assertRaises(PolicyViolation):
            actor.call("demo:payments.transfer", refused_body, intent="unauthorized transfer")
        refused_body.assert_not_called()
        self.assertEqual(self.snapshot()["events"][-1]["executionStatus"], "not-started")

    def test_sdk_lost_or_rejected_receipt_never_reruns_body_or_masks_failure(self):
        actor = self.actor()
        body = Mock(return_value="private result")
        body.__name__ = "external_read"
        with patch.object(actor.guard.transport, "complete", side_effect=OSError("private error")) as report:
            with self.assertRaises(CompletionReportError) as raised:
                actor.call("demo:ledger.read", body, intent="read first ledger")
            self.assertEqual(raised.exception.execution_status, "succeeded")
            self.assertNotIn("private", str(raised.exception))
            self.assertTrue(raised.exception.decision.eventId)
            report.assert_called_once()
        body.assert_called_once()
        body_error = RuntimeError("body secret")
        failed_body = Mock(side_effect=body_error)
        failed_body.__name__ = "external_failure"
        with patch.object(actor.guard.transport, "complete", side_effect=OSError("report secret")):
            with self.assertRaises(RuntimeError) as raised:
                actor.call("demo:ledger.read", failed_body, intent="read second ledger")
        self.assertIs(raised.exception, body_error)
        self.assertIn("not acknowledged", body_error.__notes__[0])
        self.assertNotIn("report secret", json.dumps(self.snapshot()))

    def test_remote_transport_sends_only_receipt_without_automatic_retry(self):
        remote = _Remote("https://example.test/engine", 5, lambda: "fixture-token")
        receipt = self.receipt(self.evaluate(), executionStatus="failed")
        with patch.object(remote, "request", return_value={}) as request:
            remote.complete(self.id, receipt)
            request.assert_called_once_with("POST", self.path + "/completion", receipt, retry=False)

    def test_cookie_mutation_origin_and_synthetic_routes_stay_closed(self):
        decision = self.evaluate()
        self.client.cookies.set("__session", self.token())
        receipt = self.receipt(decision)
        for headers in ({}, {"Origin": "https://evil.invalid"}):
            self.assertEqual(self.complete(receipt, headers=headers).status_code, 403)
        self.assertEqual(self.complete(receipt, headers={"Origin": execution.access.ORIGIN}).status_code, 200)
        self.assertEqual(self.client.post(f"/api/swarm/demo/sessions/{self.id}/completion",
                                         json=receipt, headers=self.owner).status_code, 404)

    def test_decorator_reports_completion_without_leaking_token_to_loggers(self):
        actor = self.actor()
        logger = Mock()
        actor.guard.on_decision = logger
        @actor.tool("demo:ledger.read", intent="wrapped external read")
        def read():
            return "local result"
        self.assertEqual(read(), "local result")
        self.assertEqual(self.snapshot()["events"][-1]["executionProvenance"], "caller-reported")
        logged_decision = logger.call_args.args[1]
        self.assertEqual(logged_decision.completionToken, "")
        decision = actor.check("tool", "demo:ledger.read", "second wrapped read")
        self.assertTrue(decision.completionToken)
        self.assertNotIn(decision.completionToken, repr(decision))

    def test_public_synthetic_wrapper_stays_unchanged(self):
        guard = Guard.local()
        self.addCleanup(lambda: guard.transport.store.delete(guard.session_id))
        self.assertEqual(guard.root().call("mock:fs.read", lambda: 42, intent="read"), 42)
        run = server._snapshot(guard.transport.store.get(guard.session_id), demo=True)
        for field in ("executionStatus", "executionProvenance", "toolBodyExecuted", "executionError"):
            self.assertNotIn(field, run["events"][-1])
        self.assertNotIn("executionEvidence", run)