"""Connect agent frameworks to SwarmSentinel's MCP proxy (docs/adapters.md). Standard library only.

    agent = mint_agent("https://engine.example", owner_token, session_id, "writer", scope=["mcp:files:*"])
    options = ClaudeAgentOptions(**claude_agent_sdk_options(agent))          # Claude Agent SDK
    client = MultiServerMCPClient(langchain_connections(agent))              # LangChain
    json.dump(claude_code_mcp_json(agent), open(".mcp.json", "w"))           # Claude Code / other clients

The credential grants exactly one agent's identity. Pass it to the agent host through its config,
never through the model's context, and do not log it.
"""
import http.client
import json
from dataclasses import dataclass, field
from urllib.parse import urlsplit

SERVER_NAME = "swarm"


@dataclass(frozen=True)
class ProxyAgent:
    base_url: str
    agentId: str
    credential: str = field(repr=False)
    server_name: str = SERVER_NAME

    @property
    def url(self):
        return self.base_url.rstrip("/") + "/api/swarm/mcp"

    @property
    def headers(self):
        return {"Authorization": f"Bearer {self.credential}"}


class AdapterError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def _post(url, body, headers, timeout=30):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("Expected an http(s) engine URL")
    if parts.scheme == "http" and parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Credentials may only be sent over HTTPS, except to localhost")
    connection = (http.client.HTTPSConnection if parts.scheme == "https" else http.client.HTTPConnection)(
        parts.hostname, parts.port, timeout=timeout)
    try:
        connection.request("POST", parts.path or "/", json.dumps(body).encode(),
                           {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                            **headers})
        response = connection.getresponse()
        data = response.read()
    finally:
        connection.close()
    if response.status >= 400:
        # Never echo request bodies or headers: they carry credentials.
        raise AdapterError(response.status, f"POST {parts.path} failed with HTTP {response.status}")
    return json.loads(data) if data else None


def mint_agent(base_url, owner_token, session_id, agent_id, scope=(), parent_id="", server_name=SERVER_NAME):
    """Spawn an agent through the gateway (owner-authenticated) and return its proxy connection."""
    body = _post(f"{base_url.rstrip('/')}/api/swarm/sessions/{session_id}/mcp/agents",
                 {"agentId": agent_id, "scope": list(scope), "parentId": parent_id},
                 {"Authorization": f"Bearer {owner_token}"})
    return ProxyAgent(base_url, body["agentId"], body["credential"], server_name)


def list_tools(agent):
    """Names the engine currently exposes to this agent (scope, allowlist and revocation applied)."""
    body = _post(agent.url, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, agent.headers)
    if "error" in body:
        raise AdapterError(200, f"tools/list failed: {body['error'].get('message', 'error')}")
    return [tool["name"] for tool in body["result"]["tools"]]


def claude_agent_sdk_options(agent, tools=None):
    """Keyword arguments for claude_agent_sdk.ClaudeAgentOptions that confine the agent to the proxy.

    tools=[] removes every built-in tool (Bash, Read, Write, WebFetch, ...); strict_mcp_config and
    setting_sources=[] stop project, user and plugin configuration from adding other servers or tools.
    Only the proxy's tools are pre-approved, so anything else would need a permission the SDK cannot
    grant without a can_use_tool callback.
    """
    names = list_tools(agent) if tools is None else tools
    return {
        "tools": [],
        "mcp_servers": {agent.server_name: {"type": "http", "url": agent.url, "headers": agent.headers}},
        "strict_mcp_config": True,
        "setting_sources": [],
        "allowed_tools": [f"mcp__{agent.server_name}__{name}" for name in names],
    }


def langchain_connections(agent):
    """Connections for langchain_mcp_adapters.client.MultiServerMCPClient."""
    return {agent.server_name: {"transport": "streamable_http", "url": agent.url, "headers": agent.headers}}


def claude_code_mcp_json(agent):
    """A .mcp.json document for Claude Code and other clients that read the common mcpServers format.

    It holds the credential: write it with owner-only permissions and keep it out of version control.
    """
    return {"mcpServers": {agent.server_name: {"type": "http", "url": agent.url, "headers": agent.headers}}}
