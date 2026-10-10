# Framework adapters

Agent frameworks connect to SwarmSentinel through the engine's MCP endpoint (`mcp-proxy-design.md`). Because enforcement happens in the engine, an adapter only has to deliver a connection and keep the agent from using other tools. Code: `artifacts/swarm-sentinel/python/adapters/connect.py` (standard library only). Tests: `tests/test_adapters.py`.

| Framework | Helper | Verified against |
|---|---|---|
| Claude Agent SDK | `claude_agent_sdk_options(agent)` returns `ClaudeAgentOptions` keyword arguments | `claude-agent-sdk` 0.2.165 (options accepted in the interop test) |
| LangChain | `langchain_connections(agent)` for `MultiServerMCPClient` | `langchain-mcp-adapters` 0.3.2 (tools listed, called and refused in the interop test) |
| Claude Code and other MCP clients | `claude_code_mcp_json(agent)` writes the common `mcpServers` format | Format only |
| Any Streamable HTTP MCP client | `agent.url` plus `Authorization: Bearer <credential>` | Engine tests with the `mcp` SDK |

## Flow

```python
from adapters.connect import mint_agent, claude_agent_sdk_options, langchain_connections

agent = mint_agent(engine_url, owner_token, session_id, "writer", scope=["mcp:notes:*"])
options = ClaudeAgentOptions(**claude_agent_sdk_options(agent))   # Claude Agent SDK
client = MultiServerMCPClient(langchain_connections(agent))        # LangChain
```

`mint_agent` spawns the agent through the gateway (scope, depth and revocation apply) and returns a `ProxyAgent` holding the credential. The credential is not part of the object's repr, and the helper refuses to send it over plain HTTP except to localhost.

## Confinement in the Claude Agent SDK

`claude_agent_sdk_options` sets:

- `tools=[]`, which removes every built-in tool (Bash, Read, Write, WebFetch and the rest);
- `mcp_servers` with only the proxy;
- `strict_mcp_config=True` and `setting_sources=[]`, so project, user and plugin configuration cannot add servers or tools;
- `allowed_tools` listing exactly the proxy tools this agent can currently use (`mcp__swarm__<server>__<tool>`), fetched from the engine.

This confines the agent at the host level as far as the SDK allows. It is not an operating-system boundary: the process running the SDK still has whatever filesystem and network access its environment gives it. Run it in the confined environment described in `mcp-proxy-design.md` section 10 for the full property.

## Environment

The adapter SDKs pin `mcp` 1.x, while the engine uses `mcp` 2.x, so they live in a separate environment:

```bash
python3.13 -m venv .venv-adapters
.venv-adapters/bin/pip install -r artifacts/swarm-sentinel/python/adapters/requirements.txt
# Interop test (from artifacts/swarm-sentinel/python); skipped when the environment is absent
../../../.venv/bin/python -m unittest discover tests -p test_adapters.py
```

Examples: `examples/claude_agent_sdk_proxy.py` (needs Claude credentials) and `examples/langchain_mcp_proxy.py` (no model needed). Both read the owner token from `SWARM_OWNER_TOKEN`; never paste it into a chat or commit it.

## Tool names

Proxy tools are exposed as `<server>__<tool>`. Claude and OpenAI tool-name rules allow only letters, digits, `_` and `-`, so dotted names would break once a framework hands the tools to a model.
