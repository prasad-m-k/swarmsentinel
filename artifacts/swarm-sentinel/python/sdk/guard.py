"""SwarmSentinel guard: ask the ASP gateway before an agent acts. Standard library only.

    guard = Guard.remote("http://127.0.0.1:8000")      # or Guard.local() for in-process
    orchestrator = guard.root()
    parser = orchestrator.spawn("receipt-parser", scope=["mock:fs.read"])

    @parser.tool("mock:fs.read", reads="untrusted")
    def read_invoice(path): ...

    read_invoice("INV-4471.pdf")   # evaluated first; raises PolicyViolation if denied

A denied call never reaches the wrapped function. Lineage (parent, spans) is tracked here and
verified by the gateway, so agents cannot claim a parent they were not spawned by.
"""
import functools
import http.client
import json
import threading
from dataclasses import dataclass, field, replace
from urllib.parse import urlsplit
from uuid import uuid4

from .journal import JournalError, PendingJournal, canonical


@dataclass
class Decision:
    eventId: str
    allowed: bool
    decision: str
    rule: str
    reason: str
    policyVersion: int
    taintOrigin: str = ""
    violations: list = field(default_factory=list)
    alerts: list = field(default_factory=list)
    evaluationMs: float = 0.0
    completionToken: str = field(default="", repr=False)


class PolicyViolation(Exception):
    def __init__(self, decision):
        super().__init__(f"{decision.rule}: {decision.reason}")
        self.decision = decision


class GatewayError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class ExecutionUncertain(RuntimeError):
    """Connection recovery exhausted; retry the same payload with this key."""

    def __init__(self, idempotency_key):
        super().__init__("Execution response unavailable; retry with the same idempotency_key and arguments")
        self.idempotency_key = idempotency_key


class CompletionReportError(RuntimeError):
    """The body finished but its receipt was not acknowledged; do not rerun it."""

    def __init__(self, decision, status):
        super().__init__("Tool body finished but completion receipt was not acknowledged; "
                         "do not rerun the body. Inspect the event snapshot before reporting again.")
        self.decision, self.execution_status = decision, status


class SessionUnavailable(GatewayError):
    """The saved session is expired, deleted, or inaccessible to this owner."""

    def __init__(self, status, session_id):
        super().__init__(status, f"Saved sandbox session {session_id} is expired, deleted, or inaccessible; "
                         "execution was not submitted to a replacement session")
        self.session_id = session_id


class _Remote:
    def __init__(self, base_url, timeout, token_provider=None):
        parts = urlsplit(base_url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("Expected an http(s) engine URL")
        if parts.username or parts.password or parts.query or parts.fragment:
            raise ValueError("Engine URLs must not contain credentials, query strings, or fragments")
        self.connection_class = http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection
        self.host, self.port, self.timeout = parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), timeout
        self.prefix = parts.path.rstrip("/")
        self.endpoint = f"{parts.scheme}://{parts.netloc.lower()}{self.prefix}"
        self.connections = threading.local()
        self.token_provider = token_provider

    @property
    def conn(self):
        return getattr(self.connections, "conn", None)

    @conn.setter
    def conn(self, value):
        self.connections.conn = value

    def request(self, method, path, body=None, retry=True):
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token_provider:
            token = self.token_provider()
            if token and self.connection_class is http.client.HTTPConnection and self.host not in ("localhost", "127.0.0.1", "::1"):
                raise ValueError("Authenticated remote connections require HTTPS")
            if token:
                headers["Authorization"] = f"Bearer {token}"
        # A connection belongs to one calling thread; parallel agents must not
        # interleave HTTP messages or hold a shared lock during network waits.
        for attempt in range(1, (2 if retry else 1) + 1):
            try:
                if self.conn is None:
                    self.conn = self.connection_class(self.host, self.port, timeout=self.timeout)
                self.conn.request(method, self.prefix + path, payload, headers)
                response = self.conn.getresponse()
                data = response.read()
                break
            except (http.client.HTTPException, ConnectionError, OSError):
                self.conn = None
                if not retry or attempt == 2:
                    raise
        if response.status >= 400:
            raise GatewayError(response.status,
                               f"SwarmSentinel {method} {path} failed: {response.status} {data[:300]!r}")
        return json.loads(data) if data else None

    def create(self, spec):
        return self.request("POST", "/api/swarm/sessions", spec)["sessionId"]

    def evaluate(self, session_id, call):
        return self.request("POST", f"/api/swarm/sessions/{session_id}/evaluate", call)

    def complete(self, session_id, receipt):
        # Duplicate updates are refused. A lost response must not rerun the body
        # or automatically resubmit a possibly accepted receipt.
        return self.request("POST", f"/api/swarm/sessions/{session_id}/completion", receipt, retry=False)

    def configure_sandbox(self, session_id, mode):
        return self.request("POST", f"/api/swarm/sessions/{session_id}/sandbox",
                            {"mode": mode}, retry=False)

    def sandbox_snapshot(self, session_id):
        return self.request("GET", f"/api/swarm/sessions/{session_id}/sandbox")

    def execute(self, session_id, call):
        # Both attempts send the same session-bound key, including after dispatch.
        return self.request("POST", f"/api/swarm/sessions/{session_id}/execute", call)


class _Local:
    def __init__(self, store=None):
        from live import CallInput, SessionInput, SessionStore   # engine modules; only needed in-process
        self.CallInput, self.SessionInput, self.store = CallInput, SessionInput, store or SessionStore()

    def create(self, spec):
        return self.store.create(self.SessionInput(**spec)).id

    def evaluate(self, session_id, call):
        return self.store.get(session_id).evaluate(self.CallInput(**call))

    def complete(self, session_id, receipt):
        from models import CompletionReceipt
        return self.store.get(session_id).complete(CompletionReceipt(**receipt))

    def configure_sandbox(self, session_id, mode):
        return self.store.get(session_id).configure_sandbox(mode)

    def sandbox_snapshot(self, session_id):
        return self.store.get(session_id).sandbox_snapshot()

    def execute(self, session_id, call):
        from sandbox import ExecutionInput
        return self.store.get(session_id).execute(ExecutionInput(**call))


class Guard:
    def __init__(self, transport, policy="mock", feedback=True, root="orchestrator", on_decision=None, session_id=None, label=""):
        self.transport, self.root_name, self.on_decision = transport, root, on_decision
        self.journal = None
        self.session_id = session_id or transport.create({"policy": policy, "feedbackEnabled": feedback, "root": root, "label": label})

    @classmethod
    def remote(cls, base_url, timeout=5.0, token_provider=None, journal_path=None, **kwargs):
        """token_provider returns a fresh Clerk session JWT for an authorized owner.

        Browser clients use cookies instead. Never embed credentials in URLs.
        journal_path opts into a private, independently keyed execution file. Existing
        files attach to their saved session, never create a replacement. Supply
        fresh credentials after restart, then recover_execution(key)/resume_agent(key).
        Completed entries remain until acknowledge_execution(key).
        Call journal_summary() for payload-free counts and file bytes; no automatic
        logging, size limits, eviction, or acknowledgement is enabled.
        """
        transport = _Remote(base_url, timeout, token_provider)
        if journal_path is None:
            return cls(transport, **kwargs)
        journal = PendingJournal(journal_path)
        with journal.locked():
            record = journal.read()
            if record is not None:
                if (record["endpoint"] != transport.endpoint
                        or kwargs.get("session_id", record["sessionId"]) != record["sessionId"]
                        or kwargs.get("root", record["root"]) != record["root"]):
                    raise JournalError("Journal endpoint, session, or root does not match")
                kwargs.update(session_id=record["sessionId"], root=record["root"])
            guard = cls(transport, **kwargs)
            if record is None:
                journal.write(dict(version=2, endpoint=transport.endpoint,
                                   sessionId=guard.session_id, root=guard.root_name, executions={}))
            else:
                # read() validates and normalizes legacy single-slot journals.
                journal.write(record)
            guard.journal = journal
            return guard

    @classmethod
    def local(cls, **kwargs):
        return cls(_Local(), **kwargs)

    def root(self):
        return Agent(self, self.root_name, parent=None)

    def configure_sandbox(self, mode="normal"):
        return self.transport.configure_sandbox(self.session_id, mode)

    def sandbox_snapshot(self):
        return self.transport.sandbox_snapshot(self.session_id)

    def _journal_record(self):
        record = self.journal.read()
        if (record is None or record["sessionId"] != self.session_id
                or record["endpoint"] != self.transport.endpoint or record["root"] != self.root_name):
            raise JournalError("Saved journal binding changed or disappeared")
        return record

    def _saved_execution(self, record, idempotency_key):
        entries = record["executions"]
        if idempotency_key is None:
            if len(entries) != 1:
                raise JournalError("Select an execution key; journal has zero or multiple saved executions")
            idempotency_key = next(iter(entries))
        if not isinstance(idempotency_key, str) or idempotency_key not in entries:
            raise JournalError("No saved execution for that key")
        return entries[idempotency_key]

    def pending_executions(self):
        """Return detached pending payloads indexed by execution key."""
        if self.journal is None:
            raise JournalError("Recovery requires journal_path on Guard.remote")
        with self.journal.locked():
            record = self._journal_record()
            return {key: entry["payload"] for key, entry in record["executions"].items()
                    if entry["state"] == "pending"}

    def saved_executions(self):
        """List detached pending requests and unacknowledged completed receipts by key."""
        if self.journal is None:
            raise JournalError("Recovery requires journal_path on Guard.remote")
        with self.journal.locked():
            return self._journal_record()["executions"]

    def journal_summary(self):
        """Inspect local file bytes, total, pending, and unacknowledged completed counts.

        No network calls or payloads; explicitly poll to implement your own alerts.
        The snapshot is advisory, not a size limit or permission to discard entries.
        """
        if self.journal is None:
            raise JournalError("Journal inspection requires journal_path on Guard.remote")
        return self.journal.summary(expected_binding={
            "sessionId": self.session_id,
            "endpoint": self.transport.endpoint,
            "root": self.root_name,
        })

    def pending_execution(self, idempotency_key=None):
        """Return one pending payload; omit the key only when it is unambiguous."""
        if self.journal is None:
            raise JournalError("Recovery requires journal_path on Guard.remote")
        with self.journal.locked():
            record = self._journal_record()
            if idempotency_key is None:
                pending = [entry for entry in record["executions"].values() if entry["state"] == "pending"]
                if len(pending) > 1:
                    raise JournalError("Select an execution key; multiple executions are pending")
                return pending[0]["payload"] if pending else None
            entry = self._saved_execution(record, idempotency_key)
            return entry["payload"] if entry["state"] == "pending" else None

    def resume_agent(self, idempotency_key=None):
        """Restore the saved actor and its ancestor spans without spawning again."""
        if self.journal is None:
            raise JournalError("Recovery requires journal_path on Guard.remote")
        with self.journal.locked():
            entry = self._saved_execution(self._journal_record(), idempotency_key)
            agent = None
            for ancestor in entry["lineage"]:
                agent = Agent(self, ancestor["name"], agent)
                agent.span = ancestor["span"]
            return agent

    def recover_execution(self, idempotency_key=None):
        """Replay the saved exact request against its original owner-private session.

        Also replays completed receipts, so authorization and session lifetime are
        checked by the server, not inferred from the local file.
        """
        if self.journal is None:
            raise JournalError("Recovery requires journal_path on Guard.remote")
        with self.journal.locked():
            entry = self._saved_execution(self._journal_record(), idempotency_key)
        return self._dispatch(entry["payload"], entry)

    def acknowledge_execution(self, idempotency_key):
        """Remove only this completed receipt after the caller consumes it.

        Pending/uncertain executions cannot be acknowledged or evicted. Do not
        acknowledge a key while another thread is still recovering that key.
        """
        if self.journal is None:
            raise JournalError("Acknowledgement requires journal_path on Guard.remote")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise JournalError("Acknowledgement requires an explicit execution key")
        with self.journal.locked():
            record = self._journal_record()
            entry = self._saved_execution(record, idempotency_key)
            if entry["state"] != "completed":
                raise JournalError("Cannot acknowledge an unresolved execution; recover it first")
            del record["executions"][idempotency_key]
            self.journal.write(record)

    def _dispatch(self, call, record=None):
        try:
            outcome = self.transport.execute(self.session_id, call)
        except GatewayError as e:
            if record is not None and e.status in (404, 410):
                raise SessionUnavailable(e.status, self.session_id) from e
            raise
        except (http.client.HTTPException, ConnectionError, OSError, json.JSONDecodeError) as e:
            raise ExecutionUncertain(call["idempotencyKey"]) from e
        if record is not None:
            # Commit before invoking caller logging; a crash here leaves a replayable request.
            with self.journal.locked():
                latest = self._journal_record()
                entry = self._saved_execution(latest, call["idempotencyKey"])
                if (canonical(entry["payload"]) != canonical(call)
                        or entry["lineage"] != record["lineage"]):
                    raise JournalError("Saved execution changed during dispatch")
                latest["executions"][call["idempotencyKey"]] = {
                    **entry, "state": "completed", "outcome": outcome}
                self.journal.write(latest)
        if self.on_decision:
            targets = {"read_invoice": "demo:invoice.read", "read_ledger": "demo:ledger.read",
                       "transfer_funds": "demo:payments.transfer", "post_status": "demo:board.write"}
            self.on_decision({**call, "target": targets[call["tool"]]}, Decision(**outcome["decision"]))
        return outcome

    def _execute(self, call, agent):
        if self.journal is None:
            return self._dispatch(call)
        with self.journal.locked():
            record = self._journal_record()
            # Freeze the actual JSON payload before journaling or dispatch.
            call = json.loads(canonical(call))
            lineage = []
            while agent is not None:
                lineage.insert(0, {"name": agent.name, "span": agent.span})
                agent = agent.parent
            if not lineage or lineage[0]["name"] != self.root_name:
                raise JournalError("Execution lineage must start at the saved session root")
            key = call["idempotencyKey"]
            if not isinstance(key, str) or not key:
                raise JournalError("Execution key must be a nonempty string")
            entry = record["executions"].get(key)
            if entry is not None:
                if (canonical(call) != canonical(entry["payload"])
                        or lineage != entry["lineage"]):
                    raise JournalError("Saved execution key cannot be reused with a changed payload or lineage")
            else:
                entry = {"state": "pending", "payload": call, "lineage": lineage}
                record["executions"][key] = entry
                self.journal.write(record)
        return self._dispatch(call, entry)

    def _evaluate(self, call):
        decision = Decision(**self.transport.evaluate(self.session_id, call))
        if self.on_decision:
            # Admission loggers do not need this capability. Keep it on the
            # returned decision only, not in callback logging or repr output.
            self.on_decision(call, replace(decision, completionToken=""))
        return decision


def _describe(fn, args, kwargs, limit=300):
    shown = [repr(a) for a in args] + [f"{k}={v!r}" for k, v in kwargs.items()]
    return f"{fn.__name__}({', '.join(shown)})"[:limit]


class Agent:
    def __init__(self, guard, name, parent):
        self.guard, self.name, self.parent = guard, name, parent
        self.span = f"span-{name}-{uuid4().hex[:8]}"

    def _call(self, action, target, intent, **extra):
        return self.guard._evaluate(dict(
            agentId=self.name, action=action, target=target, intent=intent, spanId=self.span,
            parentId=self.parent.name if self.parent else "", parentSpanId=self.parent.span if self.parent else "",
            **{k: v for k, v in extra.items() if v not in (None, "", [])},
        ))

    def check(self, action, target, intent="", **extra):
        """Evaluate without raising; returns the Decision."""
        return self._call(action, target, intent, **extra)

    def spawn(self, name, scope=None, intent=""):
        """Admit a sub-agent. Its scope may only narrow ours; the gateway refuses anything wider."""
        decision = self._call("spawn", name, intent or f"spawn {name}", scope=scope)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return Agent(self.guard, name, parent=self)

    def send(self, to, text):
        """Evaluate a message before delivering it (taint from a contaminated sender propagates)."""
        recipient = to.name if isinstance(to, Agent) else to
        decision = self._call("message", recipient, text, mentions=[recipient])
        if not decision.allowed:
            raise PolicyViolation(decision)
        return decision

    def call(self, target, fn, *args, intent=None, reads=None, write=None, resource="", network="", **kwargs):
        """Cooperative wrapper; owner-private completion is caller-reported, not observed.

        No result or exception details are sent. Public synthetic/local sessions
        without a receipt token retain their existing unobserved semantics.
        """
        decision = self._call("tool", target, intent or _describe(fn, args, kwargs),
                              detail=_describe(fn, args, kwargs, 1000), reads=reads, write=write,
                              resource=resource, network=network)
        if not decision.allowed:
            raise PolicyViolation(decision)
        try:
            result = fn(*args, **kwargs)
        except Exception as error:
            try:
                if decision.completionToken:
                    self.complete(decision, "failed")
            except CompletionReportError:
                # Preserve the actual body failure, without logging either
                # exception's text or hiding an unacknowledged receipt.
                error.add_note("Completion receipt was not acknowledged; do not rerun the tool body.")
            raise
        if decision.completionToken:
            self.complete(decision, "succeeded")
        return result

    def complete(self, decision, status):
        """Explicit receipt for an externally executed check('tool', ...) call.

        Report only after body entry and termination. A rejected/lost receipt
        raises CompletionReportError, never dispatches or retries a tool body.
        """
        if status not in ("succeeded", "failed") or not decision.allowed or not decision.completionToken:
            raise ValueError("Completion requires an allowed tool decision and succeeded/failed status")
        receipt = dict(eventId=decision.eventId, completionToken=decision.completionToken,
                       agentId=self.name, spanId=self.span, executionStatus=status)
        try:
            return self.guard.transport.complete(self.guard.session_id, receipt)
        except Exception:
            raise CompletionReportError(decision, status) from None

    def execute(self, tool, arguments=None, *, idempotency_key=None):
        """Evaluate and execute a registered tool, with safe connection recovery.

        Each invocation defaults to a new key. For manual retries, supply the same
        idempotency_key and unchanged payload; ExecutionUncertain exposes an
        automatically generated key when both connection attempts fail.
        With a journal, independently keyed calls can run in parallel. An omitted
        key always starts a NEW invocation, never guesses which pending call to
        retry. Use guard.recover_execution(key) after uncertainty or restart,
        rather than creating fresh spans. Acknowledge each completed key explicitly.
        """
        key = uuid4().hex if idempotency_key is None else idempotency_key
        call = dict(agentId=self.name, spanId=self.span,
                    parentId=self.parent.name if self.parent else "",
                    parentSpanId=self.parent.span if self.parent else "",
                    tool=tool, arguments=arguments or {}, idempotencyKey=key)
        return self.guard._execute(call, self)

    def tool(self, target, **options):
        """Decorator form of call(): every invocation is checked before the body runs."""
        def wrap(fn):
            @functools.wraps(fn)
            def guarded(*args, **kwargs):
                return self.call(target, fn, *args, **options, **kwargs)
            return guarded
        return wrap
