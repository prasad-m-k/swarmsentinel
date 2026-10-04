"""Direct HTTP bypass attempts against file-backed tools, using signed fixture JWTs."""
import concurrent.futures
import http.client
import threading
import unittest
from unittest.mock import Mock, patch

import test_access as access
from sdk.guard import _Remote
from sandbox import ExecutionInput
import server


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
        self.refuse_unchanged(body, "lineage.invalid", path=other_path)
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
        self.assertEqual(self.client.post(self.path + "/execute", headers=self.owner,
                                         json=self.call()).status_code, 404)
        with self.assertRaises(ValueError):
            self.session.execute(ExecutionInput(**self.call()))

    def test_execution_transport_never_retries_ambiguous_write(self):
        remote = _Remote("http://localhost", 5)
        conn = Mock()
        conn.getresponse.side_effect = http.client.RemoteDisconnected("response lost after write")
        remote.conn = conn
        with patch.object(remote, "connection_class") as reconnect:
            with self.assertRaises(http.client.RemoteDisconnected):
                remote.execute("fixture-session", self.call())
            self.assertEqual(conn.request.call_count, 1)
            reconnect.assert_not_called()