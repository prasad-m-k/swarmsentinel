# Confinement lab

MCP proxy mode removes the agent's cooperation from enforcement only when the proxy is the agent's sole route to side effects (`docs/mcp-proxy.md`, threat model T11). The engine cannot enforce that; the deployment has to. This lab is one deployment profile that does, and a probe that checks it.

## Profile

| Requirement | How the lab meets it |
|---|---|
| No route to anything but the engine | The agent container joins only `agentnet`, a Docker network with `internal: true`: no default route, no external DNS. The engine is the only other host on it. |
| No downstream credentials or servers | Downstream MCP servers are launched by the engine over stdio inside the engine container, with the MCP SDK's minimal environment. They have no network listener. The engine's own secrets (`DOWNSTREAM_API_TOKEN` stands in for one) exist only in the engine container. |
| No owner authority | The agent receives only its own MCP credential, through a read-only volume. The operator token is in a volume the agent does not mount. |
| No shell or native tools for side effects | The agent image holds an MCP client and nothing from the engine. The root file system is read-only, all Linux capabilities are dropped and `no-new-privileges` is set. |
| No self-declassification | The session policy uses `content-trust.taint-scope: task`. Only the owner API can open a new task (`POST .../mcp/agents/{agentId}/task`). |

## Running it

From the repository root, with Docker running:

```bash
uv export --frozen --no-hashes --no-dev --no-emit-project > deploy/confinement/requirements-engine.txt
cd deploy/confinement
docker compose --profile confined up --build --abort-on-container-exit --exit-code-from agent
docker compose --profile confined down -v
docker compose --profile unconfined up --build --abort-on-container-exit --exit-code-from agent-unconfined
docker compose --profile unconfined down -v
```

Results are written to `results/probe.json` (confined) and `results/probe-unconfined.json` (control).

## What the probe does

`bypass_probe.py` plays a compromised agent with arbitrary code execution inside its container and one engine-issued credential. A control call checks that sanctioned tool calls still work. Sixteen probes then try to reach a side effect, or more authority, without an allowed gateway decision: public IP and DNS egress, an HTTP fetch, a port scan of the engine host, the owner API without a token, with a forged token and with the MCP credential, a forged MCP credential, an undeclared tool, a deny-listed tool its scope pattern covers, two actions withdrawn by taint after an untrusted read, a fresh MCP connection to shed taint, and the engine's secrets.

## Results (11 October 2026, Docker Desktop 28.4 on macOS)

| Profile | Control | Routes closed |
|---|---|---|
| Confined | allowed calls succeeded | 16 of 16 |
| Unconfined (same agent, plus an ordinary network) | allowed calls succeeded | 13 of 16: public IP, DNS and HTTP egress were open |

The gateway-level probes (owner API, credentials, undeclared and denied tools, taint and laundering) were closed in both profiles. Only confinement closes the network routes, which is the condition the threat model states.

## What this does not show

The probe checks the routes it enumerates. It does not show that no other route exists, and it does not test container escapes, kernel vulnerabilities, Docker misconfiguration, or an operator who mounts more into the agent container. The engine still runs as one process (gap G6), and the lab replaces the identity provider with a local signing key held inside the engine container.
