"""Live interception: a stateful gateway + Sentinel that agents consult before each action.

Unlike /simulate (a recorded stream replayed in one request), a live session keeps gateway and
Sentinel state between calls, so every call is decided before the agent executes it.
"""
import threading
import time
import json
import hmac
from time import monotonic as completion_clock
from copy import deepcopy
from collections import OrderedDict, deque
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Literal, Optional
from uuid import uuid4
from pydantic import BaseModel, ConfigDict, Field
from asp_gateway import ASPGateway
from asp_policy import ASPPolicy
from models import Event
from sentinel import Sentinel
from simulator import intent_vector
from sandbox import ExecutionInput, REGISTRY, Sandbox

MAX_SESSIONS = 200


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["mock", "ai-village", "agents"] = "mock"
    feedbackEnabled: bool = True
    root: str = Field(default="orchestrator", min_length=1, max_length=120)
    label: str = Field(default="", max_length=200)
    completionGraceSeconds: int = Field(default=60, ge=1, le=86400)


class CallInput(BaseModel):
    """One proposed agent action, sent before the agent executes it."""
    model_config = ConfigDict(extra="forbid")
    agentId: str = Field(min_length=1, max_length=120)
    action: Literal["spawn", "tool", "message"]
    target: str = Field(min_length=1, max_length=300)
    intent: str = Field(default="", max_length=2000)
    parentId: str = ""
    spanId: str = Field(min_length=1, max_length=200)
    parentSpanId: str = ""
    detail: str = Field(default="", max_length=4000)
    network: str = ""
    reads: Literal["", "high", "medium", "low", "untrusted"] = ""
    write: Optional[bool] = None
    resource: str = ""
    mentions: list[str] = []
    scope: list[str] = []
    timestamp: Optional[str] = None


class LiveSession:
    def __init__(self, spec: SessionInput, owner=None):
        self.id = str(uuid4())
        self.owner = owner
        self.spec = spec
        self.policy = ASPPolicy.load(spec.policy)
        self.settings = SimpleNamespace(feedbackEnabled=spec.feedbackEnabled)
        self.gateway = ASPGateway(self.settings, self.policy, mode="enforce", root=spec.root)
        self.sentinel = Sentinel(self.policy)
        self.lock = threading.RLock()
        self.sandbox = None
        # Never evict a key while its disposable session is active: doing so would
        # turn a delayed replay into another write.
        self.execution_outcomes = {}
        # Opaque capabilities bind permission-only completion to this exact
        # session/event. Never export these tokens to recorder rows or reports.
        self.completion_tokens = {}
        # Server admission time only: caller timestamps cannot age this warning.
        self.completion_pending = {}
        self.closed = False
        self.created = datetime.now(timezone.utc).isoformat()
        self.timings_ms = deque(maxlen=10_000)
        self.label = spec.label

    def evaluate(self, call: CallInput, *, completion_receipt=True):
        with self.lock:
            if self.closed:
                raise ValueError("Session is closed")
            started = time.perf_counter()
            event = Event(
                id=f"live-{len(self.gateway.telemetry) + 1:05d}",
                timestamp=call.timestamp or datetime.now(timezone.utc).isoformat(),
                channel="in_band", source="live", intentVector=intent_vector(call.intent or call.target), depth=0,
                **call.model_dump(exclude={"timestamp"}),
            )
            trace = self.gateway.evaluate_and_log(event)
            if self.owner is not None and trace.action == "tool" and not trace.executed:
                trace.executionStatus = "not-started"
                trace.toolBodyExecuted = False
                trace.executionProvenance = "engine-observed"
            before = len(self.sentinel.alerts)
            self.sentinel.ingest(trace, self.gateway)
            elapsed = (time.perf_counter() - started) * 1000
            self.timings_ms.append(elapsed)
            response = dict(
                eventId=trace.id, allowed=trace.executed, decision=trace.decision, rule=trace.rule,
                reason=trace.reason, policyVersion=trace.policyVersion, taintOrigin=trace.taintOrigin,
                violations=[v.model_dump() for v in trace.violations],
                alerts=self.sentinel.alerts[before:], evaluationMs=round(elapsed, 3),
            )
            if completion_receipt and self.owner is not None and trace.action == "tool" and trace.executed:
                token = uuid4().hex
                self.completion_tokens[trace.id] = token
                self.completion_pending[trace.id] = (
                    completion_clock(),
                    dict(eventId=trace.id, agentId=trace.agentId, spanId=trace.spanId,
                         target=trace.target, admittedAt=datetime.now(timezone.utc).isoformat(),
                         kind="missing-completion"),
                )
                response["completionToken"] = token
            return response

    def completion_warnings(self):
        """Visibility only; do not change traces, detector state or retry receipts."""
        with self.lock:
            if self.closed or self.owner is None:
                return []
            now = completion_clock()
            return [dict(warning) for admitted, warning in self.completion_pending.values()
                    if now - admitted >= self.spec.completionGraceSeconds]

    def complete(self, receipt):
        with self.lock:
            if self.closed or self.owner is None:
                raise ValueError("Completion requires an active owner-private session")
            trace = next((t for t in self.gateway.telemetry if t.id == receipt.eventId), None)
            if trace is None:
                raise LookupError("Unknown evaluated event")
            token = self.completion_tokens.get(trace.id)
            if (trace.action != "tool" or not trace.executed
                    or trace.executionProvenance is not None
                    or token is None
                    or not hmac.compare_digest(token, receipt.completionToken)
                    or trace.agentId != receipt.agentId or trace.spanId != receipt.spanId):
                raise ValueError("Completion receipt is ineligible, mismatched, or already recorded")
            trace.executionStatus = receipt.executionStatus
            trace.toolBodyExecuted = True
            trace.executionProvenance = "caller-reported"
            if receipt.executionStatus == "failed":
                trace.executionError = "tool_execution_failed"
            del self.completion_tokens[trace.id]
            self.completion_pending.pop(trace.id, None)
            return trace.recorder_dump()

    def configure_sandbox(self, mode):
        with self.lock:
            if self.closed or self.spec.policy != "agents":
                raise ValueError("Sandbox tools require an active agents-policy session")
            if self.sandbox is not None or self.gateway.telemetry:
                raise ValueError("Initialize the sandbox once, before any agent actions")
            self.sandbox = Sandbox(mode)
            return self.sandbox.snapshot()

    def sandbox_snapshot(self):
        with self.lock:
            if self.closed or self.sandbox is None:
                raise ValueError("Sandbox is not available")
            return self.sandbox.snapshot()

    def execute(self, request: ExecutionInput):
        with self.lock:
            if self.closed or self.sandbox is None:
                raise ValueError("Sandbox is not available")
            fingerprint = json_arguments(request.model_dump(exclude={"idempotencyKey"}))
            if request.idempotencyKey in self.execution_outcomes:
                original, outcome = self.execution_outcomes[request.idempotencyKey]
                if original != fingerprint:
                    raise ValueError("Idempotency key already used with a different execution payload")
                if outcome is None:
                    raise ValueError("Execution outcome unavailable; this key cannot be dispatched again")
                return deepcopy(outcome)
            target, schema, options = REGISTRY[request.tool]
            arguments = schema.model_validate(request.arguments).model_dump()
            # Reserve before evaluation/dispatch. Even an unexpected interruption
            # must not permit this key to execute twice.
            self.execution_outcomes[request.idempotencyKey] = (fingerprint, None)
            options = dict(options)
            if request.tool == "read_invoice":
                options["reads"] = "untrusted" if self.sandbox.mode == "adversarial" else "high"
            call = CallInput(
                **request.model_dump(exclude={"tool", "arguments", "idempotencyKey"}),
                action="tool", target=target,
                intent=f"{request.tool} {json_arguments(arguments)}",
                detail=json_arguments(arguments), **options,
            )
            decision = self.evaluate(call, completion_receipt=False)
            # The session lock covers admission, dispatch, and completion, so
            # snapshots/SSE cannot send a premature success or an incomplete row.
            trace = self.gateway.telemetry[-1]
            trace.executionStatus = "not-started"
            trace.toolBodyExecuted = False
            trace.executionProvenance = "engine-observed"
            outcome = {"decision": decision, "allowed": decision["allowed"], "toolBodyExecuted": False}
            if not decision["allowed"]:
                outcome.update(rule=decision["rule"], reason=decision["reason"])
                return self._remember_execution(request.idempotencyKey, fingerprint, outcome)
            # Pin untrusted read contamination to the admitted actor too: a caller cannot
            # obtain a clean context by making up a fresh span after reading the fixture.
            if options.get("reads") == "untrusted":
                self.gateway.tainted.setdefault(request.agentId, decision["taintOrigin"])
            # If execution is interrupted outside ordinary exception handling,
            # retain body-entry evidence without falsely claiming not-started.
            trace.toolBodyExecuted = True
            trace.executionStatus = None
            try:
                result = self.sandbox.dispatch(request.tool, arguments)
            except Exception:
                # A body can fail after a side effect. Retain failures too, and
                # do not expose internal paths or other unexpected error details.
                outcome.update(toolBodyExecuted=True, error="tool_execution_failed",
                               reason="Registered tool execution failed")
                trace.executionStatus = "failed"
                trace.executionError = "tool_execution_failed"
            else:
                outcome.update(toolBodyExecuted=True, result=result)
                trace.executionStatus = "succeeded"
            return self._remember_execution(request.idempotencyKey, fingerprint, outcome)

    def _remember_execution(self, key, fingerprint, outcome):
        self.execution_outcomes[key] = (fingerprint, deepcopy(outcome))
        return deepcopy(outcome)

    def close(self):
        with self.lock:
            self.closed = True
            self.execution_outcomes.clear()
            self.completion_tokens.clear()
            self.completion_pending.clear()
            if self.sandbox:
                self.sandbox.close()


def json_arguments(arguments):
    return json.dumps(arguments, sort_keys=True)


class SessionStore:
    """In-memory and bounded: the oldest session is evicted once MAX_SESSIONS is reached."""

    def __init__(self, limit=MAX_SESSIONS):
        self.limit = limit
        self.sessions = OrderedDict()
        self.lock = threading.Lock()

    def create(self, spec, owner=None):
        session = LiveSession(spec, owner)
        with self.lock:
            if owner is not None and (len(self.sessions) >= self.limit or
                                      sum(s.owner == owner for s in self.sessions.values()) >= 20):
                # Private traffic must not evict another owner's traces.
                raise OverflowError("Private session capacity reached")
            self.sessions[session.id] = session
            while len(self.sessions) > self.limit:
                self.sessions.popitem(last=False)
        return session

    def get(self, session_id):
        with self.lock:
            return self.sessions.get(session_id)

    def delete(self, session_id):
        with self.lock:
            session = self.sessions.get(session_id)
            if session is None:
                return False
            # Close before removal while holding its execution lock. Requests which
            # already fetched this object cannot dispatch after deletion completes.
            with session.lock:
                session.close()
                self.sessions.pop(session_id)
            return True
