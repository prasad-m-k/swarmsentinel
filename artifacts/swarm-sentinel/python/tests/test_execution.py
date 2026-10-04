"""Direct HTTP bypass attempts against file-backed tools, using signed fixture JWTs."""
import concurrent.futures
import asyncio
import http.client
import json
import time
import threading
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4

import test_access as access
from sdk.guard import Agent, ExecutionUncertain, Guard, _Remote
from sandbox import ExecutionInput
import server
import auth


class GatewayExecution(unittest.TestCase):
    token = access.AccessControl.token
    headers = access.AccessControl.headers
    setUpClass = classmethod(access.AccessControl.setUpClass.__func__)

    def setUp(self):
        access.AccessControl.setUp(self)
        self.addCleanup(lambda: [server.SESSIONS.delete(sid) for sid in list(server.SESSIONS.sessions)])
        response = self.client.post("/api/swarm/sessions", headers=self.owner,
                                    json={"policy": "agents", "label": "Real LLM agents · security fixture"})
        self.id = response.json()["sessionId"]
        self.path = f"/api/swarm/sessions/{self.id}"
        self.session = server.SESSIONS.get(self.id)
        self.assertEqual(self.client.post(self.path + "/sandbox", headers=self.owner,
                                         json={"mode": "adversarial"}).status_code, 201)
        self.spawn("reader", ["demo:invoice.read"])
        self.spawn("reviewer", ["demo:invoice.read", "demo:ledger.read", "demo:payments.transfer"])
        self.spawn("reporter", ["demo:board.write"])

    def spawn(self, name, scope):
        call = dict(agentId="orchestrator", spanId="root-span", action="spawn", target=name, scope=scope)
        response = self.client.post(self.path + "/evaluate", headers=self.owner, json=call)
        self.assertTrue(response.json()["allowed"], response.text)

    def call(self, agent="reviewer", tool="transfer_funds", arguments=None, **extra):
        extra.setdefault("idempotencyKey", uuid4().hex)
        return dict(agentId=agent, spanId=f"{agent}-span", parentId="orchestrator",
                    parentSpanId="root-span", tool=tool,
                    arguments={"amount": 125, "recipient": "approved-supplier"} if arguments is None else arguments,
                    **extra)

    def files(self):
        return {p.name: p.read_bytes() for p in self.session.sandbox.directory.iterdir()}

    def refuse_unchanged(self, body, rule=None, status=200, headers=None, path=None):
        before, executions = self.files(), list(self.session.sandbox.executions)
        with patch.object(self.session.sandbox, "dispatch", wraps=self.session.sandbox.dispatch) as dispatch:
            response = self.client.post((path or self.path) + "/execute", json=body,
                                        headers=self.owner if headers is None else headers)
            self.assertEqual(response.status_code, status, response.text)
            if status == 200:
                self.assertFalse(response.json()["allowed"])
                self.assertFalse(response.json()["toolBodyExecuted"])
                self.assertEqual(response.json()["rule"], rule)
            dispatch.assert_not_called()
        self.assertEqual(self.files(), before)
        self.assertEqual(self.session.sandbox.executions, executions)

    def test_refused_scope_revocation_unknown_and_forged_lineage_never_dispatch(self):
        self.refuse_unchanged(self.call(agent="reader"), "delegation.out_of_scope")
        self.refuse_unchanged(self.call(agent="unadmitted"), "lineage.unknown")
        for field in ("parentId", "parentSpanId"):
            body = self.call()
            body[field] = "forged"
            self.refuse_unchanged(body, "lineage.invalid")
        with self.session.lock:
            self.session.gateway.revoke(["reviewer"])
        self.refuse_unchanged(self.call(), "tripwire.revoked")

    def test_untrusted_read_cannot_be_laundered_through_fresh_span_or_child(self):
        read = self.client.post(self.path + "/execute", headers=self.owner,
                               json=self.call(tool="read_invoice", arguments={}))
        self.assertTrue(read.json()["allowed"])
        self.assertEqual(read.json()["decision"]["taintOrigin"], read.json()["decision"]["eventId"])
        write = self.call()
        write["spanId"] = "made-up-clean-span"
        self.refuse_unchanged(write, "trust.contaminated_tool")
        spawned = self.client.post(self.path + "/evaluate", headers=self.owner, json=dict(
            agentId="reviewer", spanId="made-up-clean-span", parentId="orchestrator",
            parentSpanId="root-span", action="spawn", target="child", scope=["demo:payments.transfer"]))
        self.assertTrue(spawned.json()["allowed"])
        child = self.call(agent="child")
        child.update(parentId="reviewer", parentSpanId="made-up-clean-span")
        self.refuse_unchanged(child, "trust.contaminated_tool")

    def test_tainted_message_recipient_cannot_write_board(self):
        self.client.post(self.path + "/execute", headers=self.owner,
                         json=self.call(tool="read_invoice", arguments={}))
        self.client.post(self.path + "/evaluate", headers=self.owner, json=dict(
            agentId="reviewer", spanId="reviewer-span", parentId="orchestrator", parentSpanId="root-span",
            action="message", target="reporter", intent="invoice summary", mentions=["reporter"]))
        self.refuse_unchanged(self.call(agent="reporter", tool="post_status", arguments={"text": "paid"}),
                              "trust.contaminated_write")

    def test_cross_owner_anonymous_expired_and_cross_session_requests(self):
        body = self.call()
        for headers, status in (({}, 401), ({"X-User-Id": "owner"}, 401), (self.other, 404),
                                (self.headers(exp=1), 401)):
            self.refuse_unchanged(body, headers=headers, status=status)
        for method, suffix, payload in (("GET", "/sandbox", None), ("POST", "/sandbox", {"mode": "normal"})):
            for headers, status in (({}, 401), (self.other, 404)):
                self.assertEqual(self.client.request(method, self.path + suffix, headers=headers,
                                                     json=payload).status_code, status)
        other = self.client.post("/api/swarm/sessions", headers=self.owner, json={"policy": "agents"}).json()["sessionId"]
        other_path = f"/api/swarm/sessions/{other}"
        self.client.post(other_path + "/sandbox", headers=self.owner, json={})
        self.refuse_unchanged(body, "lineage.unknown", path=other_path)
        # Even the same agent name admitted in another session has different lineage.
        self.client.post(other_path + "/evaluate", headers=self.owner, json=dict(
            agentId="orchestrator", spanId="other-root-span", action="spawn", target="reviewer",
            scope=["demo:payments.transfer"]))
        # This is a new logical request after admission, not a replay of the
        # earlier lineage.unknown refusal.
        self.refuse_unchanged({**body, "idempotencyKey": uuid4().hex}, "lineage.invalid", path=other_path)
        self.assertEqual(server.SESSIONS.get(other).sandbox.executions, [])

    def test_caller_cannot_supply_decision_metadata_paths_or_bad_arguments(self):
        for key, value in (("allowed", True), ("decision", {"allowed": True}), ("reads", "high"),
                           ("write", False), ("resource", "other-ledger"), ("target", "demo:ledger.read"),
                           ("timestamp", "2000-01-01T00:00:00Z"), ("directory", "/tmp")):
            body = self.call()
            body[key] = value
            self.refuse_unchanged(body, status=422)
        for arguments in ({"amount": True, "recipient": "approved-supplier"},
                          {"amount": "125", "recipient": "approved-supplier"},
                          {"amount": -1, "recipient": "approved-supplier"},
                          {"amount": 125, "recipient": "external"},
                          {"amount": 125, "recipient": "approved-supplier", "path": "/tmp"},
                          {}):
            self.refuse_unchanged(self.call(arguments=arguments), status=422)
        for tool in ("shell", "__getattribute__", "../transfer_funds"):
            self.refuse_unchanged(self.call(tool=tool), status=422)
        self.refuse_unchanged(self.call(tool="read_invoice", arguments={"reads": "high"}), status=422)
        self.refuse_unchanged(self.call(tool="post_status", arguments={"text": 123}), status=422)

    def test_allow_on_evaluate_is_not_a_transfer_authorization(self):
        evaluation = self.client.post(self.path + "/evaluate", headers=self.owner, json=dict(
            agentId="reviewer", spanId="reviewer-span", parentId="orchestrator", parentSpanId="root-span",
            action="tool", target="demo:payments.transfer", write=False, reads="high"))
        self.assertTrue(evaluation.json()["allowed"])
        self.assertEqual(self.session.sandbox.executions, [])
        with self.session.lock:
            self.session.gateway.revoke(["reviewer"])
        self.refuse_unchanged(self.call(), "tripwire.revoked")

    def test_tool_failure_is_not_reported_as_success(self):
        before = self.files()
        response = self.client.post(self.path + "/execute", headers=self.owner,
                                    json=self.call(arguments={"amount": 20000, "recipient": "approved-supplier"}))
        outcome = response.json()
        self.assertTrue(outcome["allowed"])
        self.assertTrue(outcome["toolBodyExecuted"])
        self.assertEqual(outcome["error"], "tool_execution_failed")
        self.assertNotIn("result", outcome)
        self.assertEqual(self.files(), before)
        self.assertEqual(self.session.sandbox.executions, [])

    def test_concurrent_transfers_do_not_lose_updates(self):
        def transfer(_):
            return self.client.post(self.path + "/execute", headers=self.owner, json=self.call()).json()
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(transfer, range(2)))
        self.assertTrue(all(o["allowed"] and o["toolBodyExecuted"] for o in outcomes))
        ledger = self.session.sandbox.snapshot()["ledger"]
        self.assertEqual(ledger["credits"], 9750)
        self.assertEqual(len(ledger["transfers"]), 2)

    def test_session_lock_covers_evaluation_and_body(self):
        entered, release, attempted, revoked = [threading.Event() for _ in range(4)]
        original = self.session.sandbox.dispatch
        def body(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original(*args)
        def revoke():
            attempted.set()
            with self.session.lock:
                self.session.gateway.revoke(["reviewer"])
                revoked.set()
        with patch.object(self.session.sandbox, "dispatch", side_effect=body), \
                concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            call = pool.submit(self.client.post, self.path + "/execute", headers=self.owner, json=self.call())
            self.assertTrue(entered.wait(5))
            revocation = pool.submit(revoke)
            self.assertTrue(attempted.wait(5))
            self.assertFalse(revoked.is_set())
            release.set()
            self.assertTrue(call.result(timeout=5).json()["toolBodyExecuted"])
            revocation.result(timeout=5)
        self.refuse_unchanged(self.call(), "tripwire.revoked")

    def test_cookie_origin_demo_isolation_and_cleanup(self):
        self.client.cookies.set("__session", self.token())
        self.refuse_unchanged(self.call(), headers={}, status=403)
        self.refuse_unchanged(self.call(), headers={"Origin": "https://evil.invalid"}, status=403)
        self.assertEqual(self.client.post(self.path + "/execute", headers={"Origin": access.ORIGIN},
                                         json=self.call()).status_code, 200)
        self.assertEqual(self.client.post(f"/api/swarm/demo/sessions/{self.id}/execute",
                                         json=self.call()).status_code, 404)
        directory = self.session.sandbox.directory
        self.assertEqual(self.client.delete(self.path, headers=self.owner).status_code, 204)
        self.assertFalse(directory.exists())
        self.assertEqual(self.session.execution_outcomes, {})
        self.assertEqual(self.client.post(self.path + "/execute", headers=self.owner,
                                         json=self.call()).status_code, 404)
        with self.assertRaises(ValueError):
            self.session.execute(ExecutionInput(**self.call()))

    def test_exact_replay_returns_original_and_new_keys_get_fresh_checks(self):
        body = self.call()
        original = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
        self.assertTrue(original["toolBodyExecuted"])
        telemetry = len(self.session.gateway.telemetry)
        self.session.gateway.revoke(["reviewer"])
        with patch.object(self.session.sandbox, "dispatch", wraps=self.session.sandbox.dispatch) as dispatch:
            # JSON member order has no meaning, but every value must match.
            replay = {**body, "arguments": dict(reversed(list(body["arguments"].items())))}
            self.assertEqual(self.client.post(self.path + "/execute", headers=self.owner,
                                              json=replay).json(), original)
            dispatch.assert_not_called()
        self.assertEqual(len(self.session.gateway.telemetry), telemetry)
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 1)
        self.refuse_unchanged(self.call(), "tripwire.revoked")
        self.assertEqual(len(self.session.gateway.telemetry), telemetry + 1)

    def test_same_key_changed_actor_lineage_tool_or_arguments_is_conflict(self):
        body = self.call()
        self.client.post(self.path + "/execute", headers=self.owner, json=body)
        telemetry = len(self.session.gateway.telemetry)
        for changes in ({"agentId": "reporter"}, {"spanId": "new-span"},
                        {"parentId": "forged"}, {"parentSpanId": "forged"},
                        {"tool": "post_status", "arguments": {"text": "paid"}},
                        {"arguments": {"amount": 126, "recipient": "approved-supplier"}},
                        {"arguments": {"amount": 125, "recipient": "sandbox-attacker"}},
                        {"arguments": {"amount": 125.0, "recipient": "approved-supplier"}}):
            self.refuse_unchanged({**body, **changes}, status=409)
        self.assertEqual(len(self.session.gateway.telemetry), telemetry)

    def test_refusal_and_tool_failure_replays_are_stable(self):
        for body in (self.call(agent="reader"),
                     self.call(arguments={"amount": 20000, "recipient": "approved-supplier"})):
            first = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
            telemetry = len(self.session.gateway.telemetry)
            with patch.object(self.session.sandbox, "dispatch") as dispatch:
                second = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
                dispatch.assert_not_called()
            self.assertEqual(second, first)
            self.assertEqual(len(self.session.gateway.telemetry), telemetry)

    def test_snapshot_and_report_distinguish_authorization_from_outcomes(self):
        denied = self.call(agent="reader")
        failed = self.call(arguments={"amount": 20000, "recipient": "approved-supplier"})
        succeeded = self.call(tool="read_ledger", arguments={})
        for body in (denied, failed, succeeded):
            self.client.post(self.path + "/execute", headers=self.owner, json=body)
        # /evaluate returns permission only, not tool completion.
        self.client.post(self.path + "/evaluate", headers=self.owner, json=dict(
            agentId="reviewer", spanId="external-span", parentId="orchestrator",
            parentSpanId="root-span", action="tool", target="demo:ledger.read"))
        run = self.client.get(self.path, headers=self.owner).json()
        self.assertTrue(run["executionEvidence"])
        blocked, failure, success, unknown = run["events"][-4:]
        self.assertEqual((blocked["executed"], blocked["executionStatus"], blocked["toolBodyExecuted"]),
                         (False, "not-started", False))
        self.assertEqual((failure["executed"], failure["executionStatus"], failure["toolBodyExecuted"]),
                         (True, "failed", True))
        self.assertEqual(failure["executionError"], "tool_execution_failed")
        self.assertEqual((success["executed"], success["executionStatus"], success["toolBodyExecuted"]),
                         (True, "succeeded", True))
        self.assertNotIn("executionError", success)
        self.assertNotIn("executionStatus", unknown)
        self.assertNotIn("toolBodyExecuted", unknown)
        self.assertNotIn("executionStatus", run["events"][0])  # spawn is not a tool
        self.assertIn("- failed: 1", run["report"])
        self.assertIn("- succeeded: 1", run["report"])
        self.assertIn("- not-started: 1", run["report"])
        self.assertIn("- Outcome not observed: 1", run["report"])
        self.assertIn("| yes | failed | true | tool_execution_failed |", run["report"])
        self.assertIn("Shared resources with successful tool writes: none", run["report"])
        self.assertIn("weight counts admissions, not successful executions", run["report"])
        self.assertEqual(self.session.sandbox.snapshot()["ledger"]["credits"], 10000)
        # Replays do not change evidence or append a second trace.
        for body in (denied, failed, succeeded):
            self.client.post(self.path + "/execute", headers=self.owner, json=body)
        self.assertEqual(self.client.get(self.path, headers=self.owner).json(), run)

    def test_stream_outcomes_match_snapshot_and_cursor_reconnect(self):
        before = len(self.session.gateway.telemetry)
        for body in (self.call(agent="reader"),
                     self.call(arguments={"amount": 20000, "recipient": "approved-supplier"}),
                     self.call(tool="read_ledger", arguments={})):
            self.client.post(self.path + "/execute", headers=self.owner, json=body)
        snapshot = self.client.get(self.path, headers=self.owner).json()

        async def read(after):
            principal = auth.Principal("owner", time.time() + 300)
            response = await server.stream_session(self.id, after, principal)
            iterator = response.body_iterator
            try:
                return json.loads((await anext(iterator)).split("data: ", 1)[1])
            finally:
                await iterator.aclose()

        for after in (before, before + 1):
            message = asyncio.run(read(after))
            self.assertEqual(message["events"], snapshot["events"][after:])
            self.assertEqual(message["total"], len(snapshot["events"]))
            self.assertEqual(message["alerts"], snapshot["alerts"])
            self.assertEqual(message["policies"], snapshot["policies"])

    def test_failure_evidence_never_contains_exception_text_paths_or_results(self):
        for error in (ValueError("credential=fixture-secret /tmp/private-sandbox"),
                      OSError("credential=fixture-secret /tmp/private-sandbox")):
            with patch.object(self.session.sandbox, "dispatch", side_effect=error):
                outcome = self.client.post(self.path + "/execute", headers=self.owner,
                                           json=self.call(tool="read_ledger", arguments={})).json()
            self.assertTrue(outcome["allowed"])
            self.assertTrue(outcome["toolBodyExecuted"])
            self.assertEqual(outcome["reason"], "Registered tool execution failed")
        run = self.client.get(self.path, headers=self.owner).json()
        exported = json.dumps(run)
        for text in ("fixture-secret", "/tmp/private-sandbox", str(self.session.sandbox.directory)):
            self.assertNotIn(text, exported)
            self.assertNotIn(text, json.dumps(outcome))
        for event in run["events"][-2:]:
            self.assertEqual(event["executionStatus"], "failed")
            self.assertNotIn("result", event)
        self.assertIn("- failed: 2", run["report"])

    def test_all_registered_tools_record_success_without_copying_results(self):
        for body in (self.call(agent="reader", tool="read_invoice", arguments={}),
                     self.call(tool="read_ledger", arguments={}),
                     self.call(),
                     self.call(agent="reporter", tool="post_status", arguments={"text": "paid"})):
            # The reader's adversarial invoice is not shared with the other actors.
            outcome = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
            self.assertTrue(outcome["allowed"])
            self.assertTrue(outcome["toolBodyExecuted"])
            self.assertIn("result", outcome)
        run = self.client.get(self.path, headers=self.owner).json()
        for event in run["events"][-4:]:
            self.assertEqual(event["executionStatus"], "succeeded")
            self.assertTrue(event["toolBodyExecuted"])
            self.assertNotIn("result", event)
        self.assertIn("- succeeded: 4", run["report"])
        self.assertIn("Shared resources with successful tool writes: sandbox:board, sandbox:ledger", run["report"])

    def test_denied_permission_only_tool_is_not_started_and_public_demo_unchanged(self):
        response = self.client.post(self.path + "/evaluate", headers=self.owner, json=dict(
            agentId="reader", spanId="reader-span", parentId="orchestrator",
            parentSpanId="root-span", action="tool", target="demo:payments.transfer"))
        self.assertFalse(response.json()["allowed"])
        event = self.client.get(self.path, headers=self.owner).json()["events"][-1]
        self.assertEqual(event["executionStatus"], "not-started")
        self.assertFalse(event["toolBodyExecuted"])
        from live import CallInput, SessionInput
        from models import RunInput
        demo = server.DEMOS.create(SessionInput())
        demo.evaluate(CallInput(agentId="orchestrator", spanId="demo-span", action="tool", target="mock:fs.read"))
        demo_run = server._snapshot(demo, demo=True)
        synthetic = server.simulate(RunInput(scenario="normal"))
        for run in (demo_run, synthetic):
            self.assertNotIn("executionEvidence", run)
            self.assertNotIn("Tool execution evidence", run["report"])
            for event in run["events"]:
                self.assertNotIn("executionStatus", event)
                self.assertNotIn("toolBodyExecuted", event)
                self.assertNotIn("executionError", event)

    def test_unexpected_failure_after_side_effect_is_not_dispatched_again(self):
        body = self.call()
        original_dispatch = self.session.sandbox.dispatch
        def interrupted(*args):
            original_dispatch(*args)
            raise OSError("private internal path")
        with patch.object(self.session.sandbox, "dispatch", side_effect=interrupted) as dispatch:
            first = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
            second = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
            self.assertEqual(first, second)
            self.assertEqual(first["error"], "tool_execution_failed")
            self.assertNotIn("private", first["reason"])
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 1)
        run = self.client.get(self.path, headers=self.owner).json()
        self.assertEqual(run["events"][-1]["executionStatus"], "failed")
        self.assertTrue(run["events"][-1]["toolBodyExecuted"])
        self.assertIn("failure does not prove rollback", run["report"])

    def test_concurrent_duplicates_wait_for_original_without_dispatching_twice(self):
        entered, release, attempted = [threading.Event() for _ in range(3)]
        body = self.call()
        original_dispatch = self.session.sandbox.dispatch
        def slow(*args):
            entered.set()
            self.assertTrue(release.wait(5))
            return original_dispatch(*args)
        def retry():
            attempted.set()
            return self.client.post(self.path + "/execute", headers=self.owner, json=body)
        with patch.object(self.session.sandbox, "dispatch", side_effect=slow) as dispatch:
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(self.client.post, self.path + "/execute", headers=self.owner, json=body)
                try:
                    self.assertTrue(entered.wait(5))
                    second = pool.submit(retry)
                    self.assertTrue(attempted.wait(5))
                    self.assertFalse(second.done())
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5).json(), second.result(timeout=5).json())
            self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.session.sandbox.snapshot()["ledger"]["credits"], 9875)

    def test_board_write_replay_and_keys_are_session_bound(self):
        body = self.call(agent="reporter", tool="post_status", arguments={"text": "paid"})
        first = self.client.post(self.path + "/execute", headers=self.owner, json=body).json()
        self.assertTrue(first["toolBodyExecuted"])
        self.assertEqual(self.client.post(self.path + "/execute", headers=self.owner, json=body).json(), first)
        self.assertEqual(self.session.sandbox.snapshot()["board"], [{"text": "paid"}])
        other_id = self.client.post("/api/swarm/sessions", headers=self.owner,
                                    json={"policy": "agents"}).json()["sessionId"]
        other_path = f"/api/swarm/sessions/{other_id}"
        self.client.post(other_path + "/sandbox", headers=self.owner, json={})
        self.refuse_unchanged(body, "lineage.unknown", path=other_path)

    def test_cached_outcome_still_requires_current_authentication_and_owner(self):
        body = self.call()
        self.client.post(self.path + "/execute", headers=self.owner, json=body)
        for headers, status in (({}, 401), (self.other, 404), (self.headers(exp=1), 401)):
            self.refuse_unchanged(body, headers=headers, status=status)
        with patch.object(server, "still_valid", return_value=False):
            self.refuse_unchanged(body, status=401)

    def test_required_key_validation_and_invalid_arguments_do_not_reserve_key(self):
        for key in (None, "", 123, "x" * 201, "bad key"):
            self.refuse_unchanged(self.call(idempotencyKey=key), status=422)
        missing = self.call()
        del missing["idempotencyKey"]
        self.refuse_unchanged(missing, status=422)
        body = self.call(arguments={})
        self.refuse_unchanged(body, status=422)
        body["arguments"] = {"amount": 125, "recipient": "approved-supplier"}
        self.assertTrue(self.client.post(self.path + "/execute", headers=self.owner,
                                         json=body).json()["toolBodyExecuted"])

    def test_interrupted_evaluation_keeps_key_reserved(self):
        body = self.call()
        with patch.object(self.session, "evaluate", side_effect=RuntimeError("interrupted")):
            with self.assertRaises(RuntimeError):
                self.session.execute(ExecutionInput(**body))
        self.refuse_unchanged(body, status=409)
        self.assertTrue(self.client.post(self.path + "/execute", headers=self.owner,
                                         json=self.call()).json()["toolBodyExecuted"])

    def test_local_sdk_replay_does_not_alias_the_saved_outcome(self):
        guard = Guard.local(policy="agents")
        self.addCleanup(lambda: guard.transport.store.delete(guard.session_id))
        guard.configure_sandbox()
        reviewer = guard.root().spawn("reviewer", scope=["demo:ledger.read", "demo:payments.transfer"])
        original = reviewer.execute("read_ledger", idempotency_key="read")
        original["result"]["credits"] = -1
        original["decision"]["reason"] = "modified"
        reviewer.execute("transfer_funds", {"amount": 125, "recipient": "approved-supplier"})
        replay = reviewer.execute("read_ledger", idempotency_key="read")
        self.assertEqual(replay["result"]["credits"], 10000)
        self.assertNotEqual(replay["decision"]["reason"], "modified")
        self.assertEqual(guard.sandbox_snapshot()["ledger"]["credits"], 9875)

    def test_execution_transport_recovers_lost_response_after_dispatch(self):
        import json
        remote = _Remote("http://localhost", 5)
        first, second = Mock(), Mock()
        responses, sent = [], []
        def send(method, path, payload, headers):
            sent.append(json.loads(payload))
            responses.append(self.client.request(method, path, content=payload,
                                                  headers={**headers, **self.owner}))
        first.request.side_effect = second.request.side_effect = send
        first.getresponse.side_effect = http.client.RemoteDisconnected("response lost after write")
        second.getresponse.side_effect = lambda: Mock(status=responses[-1].status_code,
                                                       read=lambda: responses[-1].content)
        remote.conn = first
        guard = Guard(remote, session_id=self.id)
        root = Agent(guard, "orchestrator", None)
        root.span = "root-span"
        reviewer = Agent(guard, "reviewer", root)
        reviewer.span = "reviewer-span"
        with patch.object(remote, "connection_class", return_value=second):
            outcome = reviewer.execute("transfer_funds", {"amount": 125, "recipient": "approved-supplier"})
        self.assertEqual(outcome, responses[0].json())
        self.assertEqual(sent[0], sent[1])
        self.assertTrue(sent[0]["idempotencyKey"])
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 1)

    def test_exhausted_connection_recovery_exposes_key_for_manual_retry(self):
        remote = _Remote("http://localhost", 5)
        first, second = Mock(), Mock()
        first.getresponse.side_effect = second.getresponse.side_effect = http.client.RemoteDisconnected("lost")
        remote.conn = first
        guard = Guard(remote, session_id=self.id)
        actor = Agent(guard, "reviewer", None)
        with patch.object(remote, "connection_class", return_value=second):
            with self.assertRaises(ExecutionUncertain) as error:
                actor.execute("transfer_funds", {"amount": 125, "recipient": "approved-supplier"})
        key = error.exception.idempotency_key
        self.assertTrue(key)
        with patch.object(remote, "execute", return_value={}) as execute:
            actor.guard.on_decision = None
            actor.execute("transfer_funds", {"amount": 125, "recipient": "approved-supplier"},
                          idempotency_key=key)
            self.assertEqual(execute.call_args.args[1]["idempotencyKey"], key)
        self.assertEqual(first.request.call_count, 1)
        self.assertEqual(second.request.call_count, 1)