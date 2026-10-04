"""Live interception: a stateful gateway + Sentinel that agents consult before each action.

Unlike /simulate (a recorded stream replayed in one request), a live session keeps gateway and
Sentinel state between calls, so every call is decided before the agent executes it.
"""
import threading
import time
import json
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
        self.closed = False
        self.created = datetime.now(timezone.utc).isoformat()
        self.timings_ms = deque(maxlen=10_000)
        self.label = spec.label

    def evaluate(self, call: CallInput):
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
            before = len(self.sentinel.alerts)
            self.sentinel.ingest(trace, self.gateway)
            elapsed = (time.perf_counter() - started) * 1000
            self.timings_ms.append(elapsed)
            return dict(
                eventId=trace.id, allowed=trace.executed, decision=trace.decision, rule=trace.rule,
                reason=trace.reason, policyVersion=trace.policyVersion, taintOrigin=trace.taintOrigin,
                violations=[v.model_dump() for v in trace.violations],
                alerts=self.sentinel.alerts[before:], evaluationMs=round(elapsed, 3),
            )

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
            target, schema, options = REGISTRY[request.tool]
            arguments = schema.model_validate(request.arguments).model_dump()
            options = dict(options)
            if request.tool == "read_invoice":
                options["reads"] = "untrusted" if self.sandbox.mode == "adversarial" else "high"
            call = CallInput(
                **request.model_dump(exclude={"tool", "arguments"}),
                action="tool", target=target,
                intent=f"{request.tool} {json_arguments(arguments)}",
                detail=json_arguments(arguments), **options,
            )
            decision = self.evaluate(call)
            outcome = {"decision": decision, "allowed": decision["allowed"], "toolBodyExecuted": False}
            if not decision["allowed"]:
                return {**outcome, "rule": decision["rule"], "reason": decision["reason"]}
            # Pin untrusted read contamination to the admitted actor too: a caller cannot
            # obtain a clean context by making up a fresh span after reading the fixture.
            if options.get("reads") == "untrusted":
                self.gateway.tainted.setdefault(request.agentId, decision["taintOrigin"])
            try:
                result = self.sandbox.dispatch(request.tool, arguments)
            except ValueError as e:
                return {**outcome, "toolBodyExecuted": True,
                        "error": "tool_execution_failed", "reason": str(e)}
            return {**outcome, "toolBodyExecuted": True, "result": result}

    def close(self):
        with self.lock:
            self.closed = True
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
