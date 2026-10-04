"""Caller-process recovery against the real owner-private execution routes."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from sdk.guard import Agent, ExecutionUncertain, GatewayError, Guard, JournalError, SessionUnavailable
from sdk.journal import PendingJournal
import test_execution as execution
import server


class CallerRecovery(unittest.TestCase):
    token = execution.GatewayExecution.token
    headers = execution.GatewayExecution.headers
    setUpClass = classmethod(execution.GatewayExecution.setUpClass.__func__)
    spawn = execution.GatewayExecution.spawn

    def setUp(self):
        execution.GatewayExecution.setUp(self)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pathname = Path(self.directory.name) / "pending.json"
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                self.forward()

            def do_GET(self):
                self.forward()

            def forward(self):
                content = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                if self.path.endswith("/execute"):
                    # The journal must be durable before even the first server dispatch.
                    saved = json.loads(fixture.pathname.read_text())
                    call = json.loads(content)
                    entry = saved["executions"][call["idempotencyKey"]]
                    fixture.assertEqual(entry["payload"], call)
                    fixture.assertIn(entry["state"], ("pending", "completed"))
                    if fixture.request_barrier is not None:
                        fixture.request_barrier.wait(timeout=10)
                response = fixture.client.request(
                    self.command, self.path, content=content,
                    headers={"Authorization": self.headers.get("Authorization", ""),
                             "Content-Type": "application/json"})
                self.send_response(response.status_code)
                self.end_headers()
                self.wfile.write(response.content)

            def log_message(self, *args):
                pass

        self.request_barrier = None
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(thread.join)
        self.addCleanup(self.http.shutdown)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.guard = self.open(session_id=self.id)
        root = Agent(self.guard, "orchestrator", None)
        root.span = "root-span"
        self.actor = Agent(self.guard, "reviewer", root)
        self.actor.span = "reviewer-span"
        self.arguments = {"amount": 125, "recipient": "approved-supplier"}

    def open(self, **kwargs):
        return Guard.remote(self.url, journal_path=self.pathname,
                            token_provider=lambda: self.owner["Authorization"][7:], **kwargs)

    def leave_pending(self, tool="transfer_funds", arguments=None):
        with patch.object(self.guard.transport, "execute", side_effect=ConnectionError("lost")):
            with self.assertRaises(ExecutionUncertain) as error:
                self.actor.execute(tool, self.arguments if arguments is None else arguments)
        return error.exception.idempotency_key

    def test_real_process_exit_after_dispatch_before_receipt_commit(self):
        # A fresh interpreter is killed after it receives the server's response,
        # without finally blocks or any opportunity to retain in-memory context.
        source = """
import os
from sdk.guard import Guard, Agent
guard = Guard.remote(os.environ["TEST_ENGINE"], journal_path=os.environ["TEST_JOURNAL"],
                     token_provider=lambda: os.environ["TEST_OWNER"])
root = Agent(guard, "orchestrator", None)
root.span = "root-span"
actor = Agent(guard, "reviewer", root)
actor.span = "reviewer-span"
write = guard.journal.write
def crash(record):
    if any(e["state"] == "completed" for e in record["executions"].values()):
        os._exit(73)
    write(record)
guard.journal.write = crash
actor.execute("transfer_funds", {"amount": 125, "recipient": "approved-supplier"})
"""
        result = subprocess.run(
            [sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "TEST_ENGINE": self.url, "TEST_JOURNAL": str(self.pathname),
                 "TEST_OWNER": self.owner["Authorization"][7:]},
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        journal = json.loads(self.pathname.read_text())
        saved = next(iter(journal["executions"].values()))
        self.assertEqual(saved["state"], "pending")
        self.assertTrue(saved["payload"]["idempotencyKey"])
        self.assertEqual(journal["sessionId"], self.id)
        self.assertEqual(saved["payload"]["parentSpanId"], "root-span")
        self.assertEqual(saved["payload"]["spanId"], "reviewer-span")
        telemetry = len(self.session.gateway.telemetry)
        with patch("sdk.guard._Remote.create", side_effect=AssertionError("must not create")), \
                patch.object(self.session.sandbox, "dispatch", wraps=self.session.sandbox.dispatch) as dispatch:
            restarted = self.open()
            original = self.session.execution_outcomes[saved["payload"]["idempotencyKey"]][1]
            self.assertEqual(restarted.recover_execution(), original)
            dispatch.assert_not_called()
        self.assertEqual(len(self.session.gateway.telemetry), telemetry)
        self.assertEqual(self.session.sandbox.snapshot()["ledger"]["credits"], 9875)
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 1)
        self.assertIsNone(restarted.pending_execution())
        resumed = restarted.resume_agent()
        self.assertEqual((resumed.name, resumed.span, resumed.parent.name, resumed.parent.span),
                         ("reviewer", "reviewer-span", "orchestrator", "root-span"))
        # Completed receipts are exact replays too, not new invocations.
        self.assertEqual(restarted.recover_execution(), original)
        resumed.execute("transfer_funds", self.arguments)
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 2)

    def test_pending_payload_and_lineage_cannot_change(self):
        key = self.leave_pending()
        restarted = self.open()
        actor = restarted.resume_agent()
        for tool, arguments, changed_key in (
                ("transfer_funds", {**self.arguments, "amount": 126}, key),
                ("transfer_funds", {**self.arguments, "amount": 125.0}, key),
                ("post_status", {"text": "paid"}, key)):
            with self.subTest(tool=tool, arguments=arguments, key=changed_key), \
                    patch.object(restarted.transport, "execute") as execute:
                with self.assertRaises(JournalError):
                    actor.execute(tool, arguments, idempotency_key=changed_key)
                execute.assert_not_called()
        actor.span = "fresh-span"
        with self.assertRaises(JournalError):
            actor.execute("transfer_funds", self.arguments, idempotency_key=key)
        actor.span = "reviewer-span"
        actor.parent.span = "fresh-parent"
        with self.assertRaises(JournalError):
            actor.execute("transfer_funds", self.arguments, idempotency_key=key)
        actor.parent.span = "root-span"
        # Explicit retry reuses the pending key; reordered JSON is unchanged.
        outcome = actor.execute("transfer_funds", dict(reversed(list(self.arguments.items()))),
                                idempotency_key=key)
        self.assertTrue(outcome["toolBodyExecuted"])
        self.assertEqual(next(iter(restarted.saved_executions())), key)

    def test_unavailable_session_never_creates_or_rekeys(self):
        self.leave_pending()
        saved = self.pathname.read_bytes()
        server.SESSIONS.delete(self.id)
        with patch("sdk.guard._Remote.create", side_effect=AssertionError("must not create")):
            restarted = self.open()
            with self.assertRaises(SessionUnavailable) as error:
                restarted.recover_execution()
        self.assertEqual(error.exception.status, 404)
        self.assertEqual(error.exception.session_id, self.id)
        self.assertIn("expired, deleted, or inaccessible", str(error.exception))
        self.assertEqual(self.pathname.read_bytes(), saved)
        with patch.object(restarted.transport, "execute", side_effect=GatewayError(410, "expired")):
            with self.assertRaises(SessionUnavailable):
                restarted.recover_execution()
        self.assertEqual(self.pathname.read_bytes(), saved)

    def test_recovery_still_requires_current_owner_credentials(self):
        self.leave_pending()
        saved = self.pathname.read_bytes()
        for headers, status, error in (({}, 401, GatewayError),
                                      (self.other, 404, SessionUnavailable)):
            restarted = Guard.remote(
                self.url, journal_path=self.pathname,
                token_provider=lambda: headers.get("Authorization", "")[7:])
            with self.assertRaises(error) as failure:
                restarted.recover_execution()
            self.assertEqual(failure.exception.status, status)
            self.assertEqual(self.pathname.read_bytes(), saved)
        self.assertTrue(self.open().recover_execution()["toolBodyExecuted"])

    def test_journal_has_no_credentials_and_private_permissions(self):
        self.actor.execute("transfer_funds", self.arguments)
        text = self.pathname.read_text()
        self.assertNotIn(self.owner["Authorization"], text)
        self.assertNotIn(self.owner["Authorization"][7:], text)
        self.assertNotIn("token_provider", text)
        for path in (self.pathname, Path(str(self.pathname) + ".lock")):
            if os.name == "nt":
                # Native open checks owner, protected DACL, reparse points and links.
                os.close(self.guard.journal.windows.open_fd(path, read=True))
            else:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for suffix in ("?token=secret", "#secret"):
            with self.assertRaises(ValueError):
                Guard.remote(self.url + suffix, journal_path=self.pathname)
        with self.assertRaises(ValueError):
            Guard.remote("http://user:secret@localhost", journal_path=self.pathname)

    def test_mismatched_binding_or_malformed_file_fails_before_network(self):
        self.leave_pending()
        for kwargs in ({"session_id": "different"}, {"root": "different"}):
            with self.assertRaises(JournalError):
                self.open(**kwargs)
        with self.assertRaises(JournalError):
            Guard.remote("http://localhost:12345", journal_path=self.pathname)
        original = json.loads(self.pathname.read_text())
        key, entry = next(iter(original["executions"].items()))
        for bad in ("{", "null", json.dumps({**original, "version": 3}),
                    json.dumps({**original, "executions": {key: {**entry, "lineage": []}}}),
                    json.dumps({**original, "executions": {key: {
                        **entry, "payload": {**entry["payload"], "spanId": "different"}}}}),
                    json.dumps({**original, "executions": {"wrong-key": entry}})):
            self.pathname.write_text(bad)
            with patch("sdk.guard._Remote.create") as create:
                with self.assertRaises(JournalError):
                    self.open()
                create.assert_not_called()

    def test_missing_or_unwritable_journal_blocks_dispatch(self):
        with patch.object(self.guard.journal, "write", side_effect=JournalError("disk full")), \
                patch.object(self.guard.transport, "execute") as execute:
            with self.assertRaises(JournalError):
                self.actor.execute("transfer_funds", self.arguments)
            execute.assert_not_called()
        self.pathname.unlink()
        with patch.object(self.guard.transport, "execute") as execute:
            with self.assertRaises(JournalError):
                self.actor.execute("transfer_funds", self.arguments)
            execute.assert_not_called()

    def test_failed_receipt_write_preserves_pending_and_recovers(self):
        write = self.guard.journal.write
        def fail_completed(record):
            if any(e["state"] == "completed" for e in record["executions"].values()):
                raise JournalError("disk full")
            write(record)
        with patch.object(self.guard.journal, "write", side_effect=fail_completed):
            with self.assertRaises(JournalError):
                self.actor.execute("transfer_funds", self.arguments)
        self.assertIsNotNone(self.guard.pending_execution())
        self.assertTrue(self.open().recover_execution()["toolBodyExecuted"])
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 1)

    def test_denied_and_failed_results_are_completed_not_new_submissions(self):
        for arguments, revoke in (({**self.arguments, "amount": 20000}, False),
                                  (self.arguments, True)):
            if revoke:
                self.session.gateway.revoke(["reviewer"])
            key = "denied" if revoke else "failed"
            outcome = self.actor.execute("transfer_funds", arguments, idempotency_key=key)
            self.assertIsNone(self.guard.pending_execution())
            telemetry = len(self.session.gateway.telemetry)
            self.assertEqual(self.open().recover_execution(key), outcome)
            self.assertEqual(len(self.session.gateway.telemetry), telemetry)
            if revoke:
                self.assertFalse(outcome["allowed"])
            else:
                self.assertEqual(outcome["error"], "tool_execution_failed")

    def test_concurrent_callers_cannot_overwrite_pending(self):
        restarted = self.open()
        with self.guard.journal.locked():
            with self.assertRaises(JournalError):
                restarted.recover_execution()
        self.leave_pending()
        payload = self.guard.pending_execution()
        payload["arguments"]["amount"] = 999
        self.assertEqual(self.guard.pending_execution()["arguments"]["amount"], 125)

    def test_atomic_write_failure_retains_original_record(self):
        key = self.leave_pending()
        saved = self.pathname.read_bytes()
        updated = json.loads(saved)
        updated["executions"][key].update(state="completed", outcome={})
        with patch.object(self.guard.journal, "_replace", side_effect=OSError("disk full")):
            with self.assertRaises(JournalError):
                self.guard.journal.write(updated)
        self.assertEqual(self.pathname.read_bytes(), saved)
        self.assertEqual(sorted(p.name for p in Path(self.directory.name).iterdir()),
                         ["pending.json", "pending.json.lock"])

    def test_idle_journal_and_default_sdk_behavior(self):
        self.assertIsNone(self.guard.pending_execution())
        with self.assertRaises(JournalError):
            self.guard.recover_execution()
        with self.assertRaises(JournalError):
            self.guard.resume_agent()
        guard = Guard.remote(self.url, session_id=self.id)
        self.assertIsNone(guard.journal)
        with self.assertRaises(JournalError):
            guard.recover_execution()
        self.assertEqual(json.loads(self.pathname.read_text())["executions"], {})

    def test_summary_counts_pending_completed_recovery_and_acknowledgement(self):
        def check(pending, completed):
            before = self.pathname.read_bytes()
            with patch.object(self.guard.transport, "request") as request, \
                    patch.object(self.guard.journal, "write") as write:
                summary = self.guard.journal_summary()
                request.assert_not_called()
                write.assert_not_called()
            self.assertEqual(summary, {
                "size_bytes": len(before), "total_entries": pending + completed,
                "pending_entries": pending, "completed_entries": completed,
            })
            self.assertEqual(self.pathname.read_bytes(), before)
            summary["pending_entries"] = -1  # Detached inspection cannot change storage.

        check(0, 0)
        key = self.leave_pending()
        check(1, 0)
        self.actor.execute("transfer_funds", self.arguments, idempotency_key="done")
        check(1, 1)
        restarted = self.open()
        self.assertEqual(restarted.journal_summary(), self.guard.journal_summary())
        restarted.recover_execution(key)
        check(0, 2)
        restarted.acknowledge_execution("done")
        check(0, 1)
        restarted.acknowledge_execution(key)
        check(0, 0)

    def test_summary_large_results_is_private_opt_in_and_never_evicts(self):
        secret = "sensitive-argument-outcome-credential"
        pending = self.leave_pending(arguments={"secret": secret})
        # Simulate a large completed response without exercising engine traffic limits.
        outcome = {"private": secret, "large_result": "x" * (1024 * 1024)}
        with patch.object(self.guard.transport, "execute", return_value=outcome), \
                patch.object(self.guard.journal, "summary") as inspect:
            self.actor.execute("read_invoice", {"secret": secret}, idempotency_key="private-key")
            inspect.assert_not_called()  # Existing execution never enables inspection/logging.
        before = self.pathname.read_bytes()
        output, errors = io.StringIO(), io.StringIO()
        with redirect_stdout(output), redirect_stderr(errors), \
                patch.object(self.guard.transport, "token_provider") as credentials:
            summary = self.guard.journal_summary()
            credentials.assert_not_called()
        self.assertEqual(output.getvalue() + errors.getvalue(), "")
        self.assertEqual(set(summary), {
            "size_bytes", "total_entries", "pending_entries", "completed_entries"})
        self.assertTrue(all(type(value) is int for value in summary.values()))
        self.assertGreater(summary["size_bytes"], 1024 * 1024)
        self.assertEqual((summary["pending_entries"], summary["completed_entries"]), (1, 1))
        self.assertEqual(self.pathname.read_bytes(), before)
        self.assertEqual(set(self.guard.saved_executions()), {pending, "private-key"})
        with self.assertRaises(JournalError):
            self.guard.acknowledge_execution(pending)
        self.assertEqual(self.pathname.read_bytes(), before)
        self.guard.acknowledge_execution("private-key")
        self.assertEqual(self.guard.journal_summary()["pending_entries"], 1)
        self.assertEqual(self.guard.journal_summary()["completed_entries"], 0)

    def test_summary_refuses_unjournaled_missing_corrupt_and_changed_binding(self):
        guard = Guard.remote(self.url, session_id=self.id)
        with self.assertRaisesRegex(JournalError, "requires journal_path"):
            guard.journal_summary()
        before = self.pathname.read_bytes()
        for field in ("sessionId", "endpoint", "root"):
            record = json.loads(before)
            record[field] = "different"
            self.pathname.write_text(json.dumps(record))
            with patch.object(self.guard.transport, "request") as request:
                with self.assertRaises(JournalError):
                    self.guard.journal_summary()
                request.assert_not_called()
        self.pathname.write_text("{")
        with self.assertRaises(JournalError):
            self.guard.journal_summary()
        self.assertEqual(self.pathname.read_text(), "{")
        self.pathname.unlink()
        with self.assertRaises(JournalError):
            self.guard.journal_summary()
        self.assertFalse(self.pathname.exists())

    def test_completed_key_changes_are_refused_without_losing_receipt(self):
        original = self.actor.execute("transfer_funds", self.arguments, idempotency_key="saved")
        saved = self.pathname.read_bytes()
        with patch.object(self.guard.transport, "execute") as execute:
            with self.assertRaises(JournalError):
                self.actor.execute("transfer_funds", {**self.arguments, "amount": 126},
                                   idempotency_key="saved")
            execute.assert_not_called()
        self.assertEqual(self.pathname.read_bytes(), saved)
        self.assertEqual(self.open().recover_execution(), original)

    def test_first_opt_in_creates_once_then_reattaches(self):
        self.pathname.unlink()
        with patch("sdk.guard._Remote.create", wraps=self.guard.transport.create) as create:
            guard = self.open(policy="agents", root="dispatcher")
            create.assert_called_once()
        session_id = guard.session_id
        guard.configure_sandbox()
        actor = guard.root().spawn("payer", scope=["demo:payments.transfer"])
        self.assertTrue(actor.execute("transfer_funds", self.arguments)["toolBodyExecuted"])
        with patch("sdk.guard._Remote.create", side_effect=AssertionError("must not create")):
            restarted = self.open()
            self.assertEqual(restarted.session_id, session_id)
            self.assertEqual(restarted.root_name, "dispatcher")
            self.assertEqual(restarted.resume_agent().parent.span, actor.parent.span)
            self.assertTrue(restarted.recover_execution()["toolBodyExecuted"])

    def test_pending_key_conflict_is_explicit_and_retained(self):
        self.leave_pending()
        saved = self.pathname.read_bytes()
        with patch.object(self.guard.transport, "execute", side_effect=GatewayError(409, "conflict")):
            with self.assertRaises(GatewayError) as error:
                self.guard.recover_execution()
        self.assertEqual(error.exception.status, 409)
        self.assertEqual(self.pathname.read_bytes(), saved)

    def test_result_commit_precedes_callback_failure(self):
        self.guard.on_decision = lambda *args: (_ for _ in ()).throw(RuntimeError("logger failed"))
        with self.assertRaisesRegex(RuntimeError, "logger failed"):
            self.actor.execute("transfer_funds", self.arguments)
        self.assertEqual(next(iter(self.guard.saved_executions().values()))["state"], "completed")
        self.assertTrue(self.open().recover_execution()["toolBodyExecuted"])
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 1)

    def test_parallel_http_calls_commit_without_overwriting_each_other(self):
        # Every HTTP request must arrive before any response: proves both the
        # journal and transport release shared locks before waiting on the network.
        self.request_barrier = threading.Barrier(3)
        reporter = Agent(self.guard, "reporter", self.actor.parent)
        reporter.span = "reporter-span"
        reader = Agent(self.guard, "reader", self.actor.parent)
        reader.span = "reader-span"
        calls = [(self.actor, "transfer_funds", self.arguments),
                 (reporter, "post_status", {"text": "parallel update"}),
                 (reader, "read_invoice", {})]
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(actor.execute, tool, arguments, idempotency_key=f"parallel-{i}")
                       for i, (actor, tool, arguments) in enumerate(calls)]
            outcomes = [future.result(timeout=15) for future in futures]
        self.request_barrier = None
        saved = self.guard.saved_executions()
        self.assertEqual(set(saved), {"parallel-0", "parallel-1", "parallel-2"})
        self.assertEqual(self.guard.pending_executions(), {})
        for i, outcome in enumerate(outcomes):
            entry = saved[f"parallel-{i}"]
            self.assertEqual(entry["outcome"], outcome)
            self.assertEqual(entry["lineage"][-1]["span"], calls[i][0].span)
            self.assertTrue(outcome["toolBodyExecuted"])
        telemetry = len(self.session.gateway.telemetry)
        with patch.object(self.session.sandbox, "dispatch", wraps=self.session.sandbox.dispatch) as dispatch:
            for i, outcome in enumerate(outcomes):
                self.assertEqual(self.open().recover_execution(f"parallel-{i}"), outcome)
            dispatch.assert_not_called()
        self.assertEqual(len(self.session.gateway.telemetry), telemetry)

    def test_process_crash_preserves_several_unfinished_responses(self):
        source = """
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from sdk.guard import Guard, Agent
guard = Guard.remote(os.environ["TEST_ENGINE"], journal_path=os.environ["TEST_JOURNAL"],
                     token_provider=lambda: os.environ["TEST_OWNER"])
root = Agent(guard, "orchestrator", None)
root.span = "root-span"
actor = Agent(guard, "reviewer", root)
actor.span = "reviewer-span"
barrier = threading.Barrier(3)
execute = guard.transport.execute
def receive(*args):
    response = execute(*args)
    barrier.wait(timeout=10)
    return response
guard.transport.execute = receive
write = guard.journal.write
def crash(record):
    if any(e["state"] == "completed" for e in record["executions"].values()):
        os._exit(73)
    write(record)
guard.journal.write = crash
with ThreadPoolExecutor(max_workers=3) as pool:
    futures = [pool.submit(actor.execute, "transfer_funds",
               {"amount": amount, "recipient": "approved-supplier"})
               for amount in (125, 126, 127)]
    for future in futures:
        future.result()
"""
        result = subprocess.run(
            [sys.executable, "-c", source], cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "TEST_ENGINE": self.url, "TEST_JOURNAL": str(self.pathname),
                 "TEST_OWNER": self.owner["Authorization"][7:]},
            capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        saved = self.guard.saved_executions()
        self.assertEqual(len(saved), 3)
        self.assertTrue(all(e["state"] == "pending" for e in saved.values()))
        self.assertEqual({e["payload"]["arguments"]["amount"] for e in saved.values()}, {125, 126, 127})
        telemetry = len(self.session.gateway.telemetry)
        with patch("sdk.guard._Remote.create", side_effect=AssertionError("must not create")), \
                patch.object(self.session.sandbox, "dispatch", wraps=self.session.sandbox.dispatch) as dispatch:
            restarted = self.open()
            self.assertEqual(restarted.session_id, self.id)
            for key, entry in saved.items():
                actor = restarted.resume_agent(key)
                self.assertEqual(actor.span, entry["payload"]["spanId"])
                self.assertEqual(actor.parent.span, entry["payload"]["parentSpanId"])
                expected = self.session.execution_outcomes[key][1]
                self.assertEqual(restarted.recover_execution(key), expected)
                self.assertEqual(restarted.saved_executions()[key]["payload"], entry["payload"])
            dispatch.assert_not_called()
        self.assertEqual(len(self.session.gateway.telemetry), telemetry)
        self.assertEqual(self.session.sandbox.snapshot()["ledger"]["credits"], 9622)
        self.assertEqual(len(self.session.sandbox.snapshot()["ledger"]["transfers"]), 3)
        self.assertEqual(restarted.pending_executions(), {})
        self.assertNotIn(self.owner["Authorization"][7:], self.pathname.read_text())
        for key in saved:
            restarted.acknowledge_execution(key)
        self.assertEqual(self.open().saved_executions(), {})

    def test_new_calls_never_evict_unresolved_or_completed_entries(self):
        old_key = self.leave_pending()
        self.actor.execute("transfer_funds", self.arguments, idempotency_key="second")
        self.actor.execute("transfer_funds", {**self.arguments, "amount": 126})
        entries = self.guard.saved_executions()
        self.assertEqual(len(entries), 3)
        self.assertEqual(entries[old_key]["state"], "pending")
        self.assertEqual(entries["second"]["state"], "completed")
        self.assertEqual(set(self.guard.pending_executions()), {old_key})
        for method in (self.guard.recover_execution, self.guard.resume_agent):
            with patch.object(self.guard.transport, "execute") as execute:
                with self.assertRaises(JournalError):
                    method()
                execute.assert_not_called()
        self.guard.acknowledge_execution("second")
        self.assertEqual(set(self.guard.saved_executions()), set(entries) - {"second"})

    def test_acknowledgement_rejects_pending_unknown_and_failed_writes(self):
        key = self.leave_pending()
        saved = self.pathname.read_bytes()
        for unknown in (key, "unknown", None):
            with self.assertRaises(JournalError):
                self.guard.acknowledge_execution(unknown)
            self.assertEqual(self.pathname.read_bytes(), saved)
        self.guard.recover_execution(key)
        saved = self.pathname.read_bytes()
        with self.assertRaises(JournalError):
            self.guard.acknowledge_execution(None)
        self.assertEqual(self.pathname.read_bytes(), saved)
        with patch.object(self.guard.journal, "write", side_effect=JournalError("disk full")):
            with self.assertRaises(JournalError):
                self.guard.acknowledge_execution(key)
        self.assertEqual(self.pathname.read_bytes(), saved)
        self.open().acknowledge_execution(key)
        self.assertEqual(self.guard.saved_executions(), {})
        with self.assertRaises(JournalError):
            self.guard.recover_execution(key)

    def test_multiple_pending_selectors_and_detached_inspection(self):
        with patch.object(self.guard.transport, "execute", side_effect=ConnectionError("lost")):
            for key in ("one", "two"):
                with self.assertRaises(ExecutionUncertain):
                    self.actor.execute("transfer_funds", self.arguments, idempotency_key=key)
        saved = self.pathname.read_bytes()
        with self.assertRaises(JournalError):
            self.guard.pending_execution()
        for method in (self.guard.resume_agent, self.guard.recover_execution):
            for key in (None, "absent"):
                with self.assertRaises(JournalError):
                    method(key)
        self.assertEqual(self.pathname.read_bytes(), saved)
        detached = self.guard.saved_executions()
        detached["one"]["lineage"][0]["span"] = "changed"
        detached["two"]["payload"]["arguments"]["amount"] = 999
        pending = self.guard.pending_executions()
        pending["one"]["arguments"]["amount"] = 888
        self.assertEqual(self.pathname.read_bytes(), saved)
        self.assertTrue(self.guard.recover_execution("two")["toolBodyExecuted"])
        self.assertEqual(set(self.guard.pending_executions()), {"one"})
        self.assertEqual(self.guard.pending_execution("one")["arguments"], self.arguments)
        self.assertIsNone(self.guard.pending_execution("two"))

    def test_legacy_journals_migrate_idle_pending_and_completed_without_rebinding(self):
        key = self.leave_pending()
        entry = self.guard.saved_executions()[key]
        header = {k: v for k, v in json.loads(self.pathname.read_text()).items() if k != "executions"}
        for state in ("idle", "pending", "completed"):
            with self.subTest(state=state):
                legacy = {**header, "version": 1, "state": state}
                if state != "idle":
                    legacy.update(payload=entry["payload"], lineage=entry["lineage"])
                if state == "completed":
                    legacy["outcome"] = {"saved": "receipt"}
                self.pathname.write_text(json.dumps(legacy))
                with patch("sdk.guard._Remote.create", side_effect=AssertionError("must not create")):
                    restarted = self.open()
                migrated = json.loads(self.pathname.read_text())
                self.assertEqual(migrated["version"], 2)
                self.assertEqual(migrated["sessionId"], self.id)
                if state == "idle":
                    self.assertEqual(restarted.saved_executions(), {})
                else:
                    self.assertEqual(restarted.saved_executions()[key]["payload"], entry["payload"])
                    self.assertEqual(restarted.saved_executions()[key]["lineage"], entry["lineage"])
                    self.assertEqual(restarted.saved_executions()[key]["state"], state)

    def test_duplicate_keys_and_corrupt_sibling_are_not_silently_dropped(self):
        key = self.leave_pending()
        original = json.loads(self.pathname.read_text())
        entry = original["executions"][key]
        serialized = json.dumps(entry)
        header = json.dumps({k: v for k, v in original.items() if k != "executions"})[:-1]
        duplicate = header + ', "executions": {' + json.dumps(key) + ':' + serialized + ',' + \
            json.dumps(key) + ':' + serialized + '}}'
        corrupt = json.dumps({**original, "executions": {key: entry, "bad": []}})
        for text in (duplicate, corrupt):
            self.pathname.write_text(text)
            with patch("sdk.guard._Remote.create") as create:
                with self.assertRaises(JournalError):
                    self.open()
                create.assert_not_called()
            self.assertEqual(self.pathname.read_text(), text)


class JournalStorage(unittest.TestCase):
    """Real subprocesses exercise the native storage branch on each test host."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.pathname = Path(self.directory.name) / "pending.json"
        self.journal = PendingJournal(self.pathname)
        self.record = dict(version=2, sessionId="original-session", root="root",
                           endpoint="https://engine.example", executions={})
        with self.journal.locked():
            self.journal.write(self.record)

    def run_child(self, source):
        return subprocess.run(
            [sys.executable, "-c", source, str(self.pathname)],
            cwd=Path(__file__).resolve().parents[1], capture_output=True, timeout=20)

    def test_summary_measures_actual_bytes_and_normalizes_legacy_without_writes(self):
        for state in ("idle", "pending", "completed"):
            with self.subTest(state=state):
                record = {k: v for k, v in self.record.items() if k != "executions"}
                record.update(version=1, state=state)
                if state != "idle":
                    record.update(
                        payload=dict(agentId="root", spanId="root-span", parentId="",
                                     parentSpanId="", tool="read_invoice", arguments={"text": "é"},
                                     idempotencyKey="key"),
                        lineage=[{"name": "root", "span": "root-span"}])
                if state == "completed":
                    record["outcome"] = {"sensitive": "résultat"}
                # Whitespace and literal UTF-8 must count as actual file bytes,
                # rather than the normalized version-2 JSON serialization size.
                with self.journal.locked():
                    self.journal.write(record)
                before = (json.dumps(record, ensure_ascii=False, indent=4) + "\n").encode("utf-8")
                self.pathname.write_bytes(before)
                with patch.object(self.journal, "write") as write:
                    summary = self.journal.summary()
                    write.assert_not_called()
                self.assertEqual(summary, {
                    "size_bytes": len(before), "total_entries": int(state != "idle"),
                    "pending_entries": int(state == "pending"),
                    "completed_entries": int(state == "completed"),
                })
                self.assertEqual(self.pathname.read_bytes(), before)

    def test_summary_refuses_lock_contention_and_size_read_failure(self):
        before = self.pathname.read_bytes()
        with self.journal.locked():
            with self.assertRaises(JournalError):
                PendingJournal(self.pathname).summary()
        with patch("sdk.journal.os.fstat", side_effect=OSError("unavailable")):
            with self.assertRaises(JournalError):
                self.journal.summary()
        self.assertEqual(self.pathname.read_bytes(), before)
        self.assertEqual(self.journal.summary()["total_entries"], 0)  # Failure released lock.

    def test_competing_process_cannot_lock_or_change_journal(self):
        before = self.pathname.read_bytes()
        with self.journal.locked():
            # Replacing the data file must not release the stable sibling lock.
            self.journal.write(self.record)
            result = self.run_child("""
import sys
from sdk.journal import PendingJournal, JournalError
journal = PendingJournal(sys.argv[1])
try:
    with journal.locked():
        journal.write({})
except JournalError:
    sys.exit(73)
sys.exit(1)
""")
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        self.assertEqual(self.pathname.read_bytes(), before)
        with PendingJournal(self.pathname).locked():
            self.assertEqual(self.journal.read(), self.record)

    def test_forced_process_termination_releases_lock(self):
        ready = self.pathname.with_name("ready")
        process = subprocess.Popen(
            [sys.executable, "-c", """
import sys, time
from pathlib import Path
from sdk.journal import PendingJournal
with PendingJournal(sys.argv[1]).locked():
    Path(sys.argv[2]).write_text("locked")
    time.sleep(60)
""", str(self.pathname), str(ready)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 15
            while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(ready.exists(), "child did not acquire its native lock")
            with self.assertRaises(JournalError):
                with self.journal.locked():
                    self.fail("competing caller acquired the lock")
            process.kill()  # No finally blocks, on POSIX or Windows.
            process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0)
            with self.journal.locked():
                self.assertEqual(self.journal.read(), self.record)
                self.journal.write(self.record)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)

    def test_interruption_before_and_after_replacement_keeps_complete_record(self):
        for after in (False, True):
            with self.subTest(after=after):
                with self.journal.locked():
                    self.journal.write(self.record)
                result = self.run_child(f"""
import os, sys
from sdk.journal import PendingJournal
journal = PendingJournal(sys.argv[1])
replace = journal._replace
def interrupt(temporary):
    if {after!r}:
        replace(temporary)
    os._exit(73)
journal._replace = interrupt
with journal.locked():
    record = journal.read()
    record["root"] = "updated-root"
    journal.write(record)
""")
                self.assertEqual(result.returncode, 73, result.stderr.decode())
                expected = {**self.record, "root": "updated-root"} if after else self.record
                with self.journal.locked():
                    self.assertEqual(self.journal.read(), expected)
                # A pre-replace crash may leave a private temp, never a torn journal.
                for path in self.pathname.parent.glob(".pending.json.*"):
                    if os.name == "nt":
                        os.close(self.journal.windows.open_fd(path, read=True))
                    else:
                        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                    path.unlink()

    def test_failed_file_flush_blocks_replacement(self):
        before = self.pathname.read_bytes()
        target = (patch.object(self.journal.windows, "flush", side_effect=OSError("flush failed"))
                  if os.name == "nt" else patch("sdk.journal.os.fsync", side_effect=OSError("flush failed")))
        with self.journal.locked(), target, patch.object(self.journal, "_replace") as replace:
            with self.assertRaises(JournalError):
                self.journal.write({**self.record, "root": "changed"})
            replace.assert_not_called()
        self.assertEqual(self.pathname.read_bytes(), before)
        self.assertEqual(sorted(p.name for p in self.pathname.parent.iterdir()),
                         ["pending.json", "pending.json.lock"])

    @unittest.skipUnless(os.name == "nt", "native Windows ACL and flush tests")
    def test_windows_private_creation_and_replacement(self):
        for _ in range(3):
            with self.journal.locked():
                self.journal.write(self.record)
                self.assertEqual(self.journal.read(), self.record)
            for path in (self.pathname, Path(str(self.pathname) + ".lock")):
                os.close(self.journal.windows.open_fd(path, read=True))

    @unittest.skipUnless(os.name == "nt", "native Windows ACL test")
    def test_windows_refuses_unprotected_existing_files(self):
        # Even a file in the owner's temp directory must have an explicit private
        # DACL, not inherit a potentially broader directory ACL.
        for path in (self.pathname, Path(str(self.pathname) + ".lock")):
            with self.subTest(path=path):
                path.unlink()
                path.write_text("unprotected", encoding="utf-8")
                with self.assertRaises(JournalError):
                    if path == self.pathname:
                        self.journal.read()
                    else:
                        with self.journal.locked():
                            self.fail("unsafe lock file accepted")
                path.unlink()
                if path == self.pathname:
                    with self.journal.locked():
                        self.journal.write(self.record)

    @unittest.skipUnless(os.name == "nt", "native Windows storage checks")
    def test_windows_rejects_remote_or_alternate_stream_paths(self):
        for path in (r"\\server\share\pending.json", str(self.pathname) + ":stream",
                     str(self.pathname) + "."):
            with self.subTest(path=path), self.assertRaises(JournalError):
                PendingJournal(path)

    @unittest.skipUnless(os.name == "nt", "native Windows hardlink checks")
    def test_windows_rejects_hardlinked_journal_and_lock(self):
        for path in (self.pathname, Path(str(self.pathname) + ".lock")):
            alias = self.pathname.with_name("alias")
            os.link(path, alias)
            try:
                with self.assertRaises(JournalError):
                    if path == self.pathname:
                        self.journal.read()
                    else:
                        with self.journal.locked():
                            self.fail("hardlinked lock accepted")
            finally:
                alias.unlink()