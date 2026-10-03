"""Live interception: a stateful gateway + Sentinel that agents consult before each action.

Unlike /simulate (a recorded stream replayed in one request), a live session keeps gateway and
Sentinel state between calls, so every call is decided before the agent executes it.
"""
import threading
import time
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

MAX_SESSIONS = 200


class SessionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    policy: Literal["mock", "ai-village"] = "mock"
    feedbackEnabled: bool = True
    root: str = Field(default="orchestrator", min_length=1, max_length=120)


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
    def __init__(self, spec: SessionInput):
        self.id = str(uuid4())
        self.spec = spec
        self.policy = ASPPolicy.load(spec.policy)
        self.settings = SimpleNamespace(feedbackEnabled=spec.feedbackEnabled)
        self.gateway = ASPGateway(self.settings, self.policy, mode="enforce", root=spec.root)
        self.sentinel = Sentinel(self.policy)
        self.lock = threading.Lock()
        self.created = datetime.now(timezone.utc).isoformat()
        self.timings_ms = deque(maxlen=10_000)

    def evaluate(self, call: CallInput):
        with self.lock:
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


class SessionStore:
    """In-memory and bounded: the oldest session is evicted once MAX_SESSIONS is reached."""

    def __init__(self, limit=MAX_SESSIONS):
        self.limit = limit
        self.sessions = OrderedDict()
        self.lock = threading.Lock()

    def create(self, spec):
        session = LiveSession(spec)
        with self.lock:
            self.sessions[session.id] = session
            while len(self.sessions) > self.limit:
                self.sessions.popitem(last=False)
        return session

    def get(self, session_id):
        with self.lock:
            return self.sessions.get(session_id)

    def delete(self, session_id):
        with self.lock:
            return self.sessions.pop(session_id, None) is not None
