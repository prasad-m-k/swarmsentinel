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
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import uuid4


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


class PolicyViolation(Exception):
    def __init__(self, decision):
        super().__init__(f"{decision.rule}: {decision.reason}")
        self.decision = decision


class _Remote:
    def __init__(self, base_url, timeout):
        parts = urlsplit(base_url)
        self.host, self.port, self.timeout = parts.hostname, parts.port or 80, timeout
        self.prefix = parts.path.rstrip("/")
        self.conn, self.lock = None, threading.Lock()

    def request(self, method, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        with self.lock:
            for attempt in (1, 2):   # one reconnect if the kept-alive socket was closed
                try:
                    if self.conn is None:
                        self.conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
                    self.conn.request(method, self.prefix + path, payload, {"Content-Type": "application/json"})
                    response = self.conn.getresponse()
                    data = response.read()
                    break
                except (http.client.HTTPException, ConnectionError, OSError):
                    self.conn = None
                    if attempt == 2:
                        raise
        if response.status >= 400:
            raise RuntimeError(f"SwarmSentinel {method} {path} failed: {response.status} {data[:300]!r}")
        return json.loads(data) if data else None

    def create(self, spec):
        return self.request("POST", "/api/swarm/sessions", spec)["sessionId"]

    def evaluate(self, session_id, call):
        return self.request("POST", f"/api/swarm/sessions/{session_id}/evaluate", call)


class _Local:
    def __init__(self, store=None):
        from live import CallInput, SessionInput, SessionStore   # engine modules; only needed in-process
        self.CallInput, self.SessionInput, self.store = CallInput, SessionInput, store or SessionStore()

    def create(self, spec):
        return self.store.create(self.SessionInput(**spec)).id

    def evaluate(self, session_id, call):
        return self.store.get(session_id).evaluate(self.CallInput(**call))


class Guard:
    def __init__(self, transport, policy="mock", feedback=True, root="orchestrator", on_decision=None, session_id=None):
        self.transport, self.root_name, self.on_decision = transport, root, on_decision
        self.session_id = session_id or transport.create({"policy": policy, "feedbackEnabled": feedback, "root": root})

    @classmethod
    def remote(cls, base_url, timeout=5.0, **kwargs):
        return cls(_Remote(base_url, timeout), **kwargs)

    @classmethod
    def local(cls, **kwargs):
        return cls(_Local(), **kwargs)

    def root(self):
        return Agent(self, self.root_name, parent=None)

    def _evaluate(self, call):
        decision = Decision(**self.transport.evaluate(self.session_id, call))
        if self.on_decision:
            self.on_decision(call, decision)
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
        """Evaluate, then run fn(*args, **kwargs) only if allowed."""
        decision = self._call("tool", target, intent or _describe(fn, args, kwargs),
                              detail=_describe(fn, args, kwargs, 1000), reads=reads, write=write,
                              resource=resource, network=network)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return fn(*args, **kwargs)

    def tool(self, target, **options):
        """Decorator form of call(): every invocation is checked before the body runs."""
        def wrap(fn):
            @functools.wraps(fn)
            def guarded(*args, **kwargs):
                return self.call(target, fn, *args, **options, **kwargs)
            return guarded
        return wrap
