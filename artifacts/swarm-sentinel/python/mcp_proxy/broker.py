"""MCP proxy admission for one live session (docs/mcp-proxy.md).

Identity comes from an engine-issued credential, never from the caller. Enforcement metadata comes
from the policy's mcp section, never from the downstream server or the agent. Admission and
recording hold the session lock; the downstream call between them does not, so one slow server
cannot queue every agent in the session.
"""
import hashlib
import json
import re
import threading
from dataclasses import dataclass
from secrets import token_hex, token_urlsafe
from urllib.parse import urlsplit

import mcp_types as types

from asp_policy import tool_matches
from content_scan import redact, scan, summary
from live import CallInput
from models import Violation
from .downstream import DownstreamPool

AGENT_ID = re.compile(r"^[A-Za-z0-9._-]{1,80}$")
MAX_DETAIL = 4000
# Exposed names are <server>__<tool>: model tool-name rules (Claude, OpenAI) allow only [A-Za-z0-9_-].
SEPARATOR = "__"

# Credential hash -> (session id, agent id). Shared by every session so the endpoint can route a call.
_credentials = {}
_credentials_lock = threading.Lock()


def _digest(credential):
    return hashlib.sha256(credential.encode()).hexdigest()


def lookup(credential):
    if not credential or not credential.startswith("ssa_"):
        return None
    digest = _digest(credential)
    with _credentials_lock:
        bound = _credentials.get(digest)
    if bound is None:
        # Durable sessions keep credentials across restarts and eviction (persistence.py).
        import persistence
        bound = persistence.STORE.credential(digest) if persistence.STORE else None
    return bound


@dataclass(frozen=True)
class McpAgent:
    agentId: str
    parentId: str
    parentSpanId: str
    spanId: str


@dataclass
class Admission:
    allowed: bool
    decision: dict
    server: str = ""
    tool: str = ""
    trace: object = None


class ProxyBroker:
    def __init__(self, session):
        self.session = session
        self.mcp = session.policy.mcp
        # Derived from the session so a restored session keeps the same root lineage.
        self.root_span = f"mcp-root-{session.id[:8]}"
        self.agents = {}
        self.digests = set()
        self.pool = DownstreamPool(self.mcp)

    def _identity(self, agent_id):
        root = self.session.gateway.root
        if agent_id == root:
            return McpAgent(root, "", "", self.root_span)
        return self.agents[agent_id]

    def mint(self, agent_id, parent_id, scope):
        """Spawn an agent through the gateway and issue its credential. Returns (decision, credential or None)."""
        if not AGENT_ID.match(agent_id):
            raise ValueError("Agent IDs use letters, digits, '.', '_' and '-' (at most 80)")
        s = self.session
        with s.lock:
            if s.closed:
                raise ValueError("Session is closed")
            if parent_id != s.gateway.root and parent_id not in self.agents:
                raise LookupError("Parent is not the session root or an agent minted for this proxy")
            parent = self._identity(parent_id)
            decision = s.evaluate(CallInput(
                agentId=parent.agentId, action="spawn", target=agent_id, intent=f"spawn {agent_id}",
                spanId=parent.spanId, parentId=parent.parentId, parentSpanId=parent.parentSpanId, scope=scope,
            ), completion_receipt=False, source="mcp")
            if not decision["allowed"]:
                return decision, None
            credential = f"ssa_{token_urlsafe(32)}"
            record = dict(agentId=agent_id, parentId=parent.agentId, parentSpanId=parent.spanId,
                          spanId=f"mcp-{agent_id}-{token_hex(4)}", digest=_digest(credential))
            self.register(record)
            s._log("mcp_agent", record)
            if s.journal is not None:
                import persistence
                persistence.STORE.add_credential(record["digest"], s.id, agent_id)
            return decision, credential

    def register(self, record):
        record = dict(record)
        digest = record.pop("digest")
        self.agents[record["agentId"]] = McpAgent(**record)
        self.digests.add(digest)
        with _credentials_lock:
            _credentials[digest] = (self.session.id, record["agentId"])

    def resolve(self, exposed):
        server, _, tool = exposed.partition(SEPARATOR)
        declared = self.mcp.servers.get(server)
        if declared is None or tool not in declared.tools:
            return None
        return server, tool, declared.tools[tool]

    def visible(self, agent_id):
        """Exposed names this agent could be allowed to call now; the gateway still decides each call."""
        gateway, tools = self.session.gateway, self.session.policy.tools
        with self.session.lock:
            if gateway.is_revoked(agent_id):
                return []
            chain = [scope for _, scope in gateway._scope_chain(agent_id)]
            return [f"{server}{SEPARATOR}{tool}" for server, declared in self.mcp.servers.items()
                    for tool, spec in declared.tools.items()
                    if not tool_matches(spec.target, tools.deny)
                    and (tools.default == "allow" or tool_matches(spec.target, tools.allow))
                    and all(tool_matches(spec.target, scope) for scope in chain)]

    def admit(self, agent_id, exposed, arguments):
        resolved = self.resolve(exposed)
        if resolved is None:
            return Admission(False, dict(rule="mcp.undeclared", reason="Tool is not declared in this session's MCP policy"))
        server, tool, spec = resolved
        detail = json.dumps(arguments, sort_keys=True)
        if len(detail) > MAX_DETAIL:
            # Truncating would hide text from argument rules, so refuse instead.
            return Admission(False, dict(rule="mcp.arguments_too_large",
                                         reason=f"Arguments exceed {MAX_DETAIL} characters"))
        network = ""
        if spec.network_argument and arguments.get(spec.network_argument) not in (None, ""):
            value = str(arguments[spec.network_argument])
            network = urlsplit(value).hostname or urlsplit(f"//{value}").hostname or value
        s = self.session
        with s.lock:
            if s.closed:
                raise ValueError("Session is closed")
            agent = self._identity(agent_id)
            decision = s.evaluate(CallInput(
                agentId=agent.agentId, action="tool", target=spec.target, intent=f"{exposed} {detail}",
                spanId=agent.spanId, parentId=agent.parentId, parentSpanId=agent.parentSpanId,
                detail=detail, network=network, reads=spec.reads, write=spec.write, resource=spec.resource,
            ), completion_receipt=False, source="mcp")
            trace = s.gateway.telemetry[-1]
            trace.executionProvenance = "engine-observed"
            if not decision["allowed"]:
                trace.executionStatus, trace.toolBodyExecuted = "not-started", False
                s.record_evidence(trace)
                return Admission(False, decision, server, tool, trace)
            # Pin untrusted reads to the actor, as /execute does: a fresh span must not yield a clean context.
            if spec.reads == "untrusted":
                s.pin_taint(agent_id, decision["taintOrigin"])
            # Body entry is recorded before dispatch; the outcome is unknown until record() runs.
            trace.toolBodyExecuted, trace.executionStatus = True, None
            s.record_evidence(trace)
            return Admission(True, decision, server, tool, trace)

    def record(self, admission, status, result=None):
        """Record the outcome and apply result inspection. Returns the result the agent may see."""
        with self.session.lock:
            if self.session.closed:
                return result
            trace = admission.trace
            trace.executionStatus = status
            if status == "failed":
                trace.executionError = "tool_execution_failed"
            visible = self._inspect_result(trace, result) if result is not None else None
            self.session.record_evidence(trace)
            return visible

    def _inspect_result(self, trace, result):
        inspection = self.session.policy.inspection
        if inspection is None or not tool_matches(trace.target, inspection.tools):
            return result
        texts = [block.text for block in result.content if getattr(block, "type", "") == "text"]
        if result.structured_content is not None:
            texts.append(json.dumps(result.structured_content, sort_keys=True))
        corpus = "\n".join(texts)
        redact_families = []
        for family, action in inspection.results.model_dump().items():
            findings = action != "off" and scan(corpus, (family,))
            if not findings:
                continue
            found = f"{family} ({summary(findings)})"
            if action == "block":
                trace.violations.append(Violation(directive=f"content-inspection.{family}", attempted_action=trace.target,
                                                  detail=f"result withheld: {found}", enforced=True))
                return types.CallToolResult(is_error=True, content=[types.TextContent(
                    type="text", text=f"The tool ran, but its result was withheld by content inspection ({family}).")])
            if action == "taint":
                self.session.pin_taint(trace.agentId, trace.id, span=trace.spanId, group=True)
            if action == "redact":
                redact_families.append(family)
            verb = {"report": "result contains", "taint": "context tainted by", "redact": "redacted"}[action]
            trace.violations.append(Violation(directive=f"content-inspection.{family}", attempted_action=trace.target,
                                              detail=f"{verb} {found}", enforced=action != "report"))
        if not redact_families:
            return result
        content = [types.TextContent(type="text", text=redact(block.text, redact_families))
                   if getattr(block, "type", "") == "text" else block for block in result.content]
        # Structured content cannot be redacted reliably in place, so it is withheld when it matched.
        structured = result.structured_content
        if structured is not None and scan(json.dumps(structured, sort_keys=True), redact_families):
            structured = None
        return result.model_copy(update={"content": content, "structured_content": structured})

    def close(self):
        with _credentials_lock:
            for digest in self.digests:
                _credentials.pop(digest, None)
        self.digests.clear()
        self.pool.close()

