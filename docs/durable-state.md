# Durable state and access control

Two changes that take live sessions past prototype-grade state and identity: an append-only journal that makes sessions survive restarts and eviction, and tenancy with roles at the HTTP boundary. Code: `persistence.py`, `live.py` (`SessionStore`, `LiveSession.restore`), `auth.py`, `server.py`. Tests: `tests/test_persistence.py`, `tests/test_rbac.py`.

## Durable sessions

Set `SWARM_STATE_DB` to a file path. Without it, nothing changes: sessions stay in memory and are refused at capacity.

```bash
SWARM_STATE_DB=data/state/swarm.db .venv/bin/python -m uvicorn server:app --app-dir artifacts/swarm-sentinel/python
```

### How it works

Every state change in a private live session is appended to a SQLite journal, in order:

| Entry | Written when | Replayed as |
|---|---|---|
| `evaluate` | Any evaluation (SDK, sandbox, MCP), with the timestamp actually used | The same evaluation, which rebuilds the recorder row, gateway counters, taint, Sentinel graph, alerts and revocations |
| `evidence` | Outcome fields written after evaluation (sandbox, MCP, completion receipts), plus inspection violations | The same fields on the same row |
| `taint` | Taint set outside the gateway's rules (engine-run untrusted reads, inspected results) | The same pin |
| `mcp_agent` | An MCP agent is minted | Its lineage and credential digest |
| `sandbox` | Sandbox fixtures are created | Nothing (see below) |

The gateway and Sentinel are deterministic given event timestamps, so replay reproduces the session exactly. Tests compare recorder rows, alerts, policy changes, revocations, taint maps, the agent registry and Sentinel edges before and after a restart.

### What this gives

- Sessions survive engine restarts. An MCP agent keeps using its credential across a restart, and its taint still applies (tested over HTTP).
- The in-memory limit (`SWARM_MAX_SESSIONS`, default 200) becomes a cache. The least recently used session is evicted and replayed on next use, instead of new sessions being refused.
- Quotas are per tenant (`SWARM_MAX_SESSIONS_PER_TENANT`, default 20) and counted in the database.
- Session listings include durable sessions that are not loaded.
- Deleting a session hard-deletes its journal and credentials.
- The database can be read by another process (WAL mode) for reporting without touching the decision path.

### What is not durable

- Sandbox fixtures. A restored session reports the sandbox unavailable rather than silently resetting the fictional ledger.
- Downstream MCP server workspaces. A restored session launches its servers with fresh workspaces. The decision record is durable; what the tools wrote is not.
- Completion tokens that were never used, and missing-completion timers. Those rows stay unobserved.

### Handling

The journal holds tool arguments, as the recorder does. The file is created with mode 0600; keep it on private storage (`data/` is the intended place and is not committed). AI Village replays are not live sessions and are never journaled. Restoring a large session replays every entry while holding the store lock, which is fine for prototype volumes and is the first thing to change for heavy use.

Policy evaluation and recording still run in one process. That is deliberate: the decision is on the hot path and stays sub-millisecond. The journal is what separates the record from the process lifetime.

## Tenancy and roles

Identity still comes only from verified Clerk JWTs. What changed is how access is decided:

- A user without an organization is their own tenant (`user:<subject>`) with the `admin` role. This is exactly the previous owner-only behaviour.
- A user acting in a Clerk organization belongs to tenant `org:<id>`. Both v2 claims (`o.id`, `o.rol`) and v1 claims (`org_id`, `org_role`) are read.
- Organization roles map to engine roles through `SWARM_ORG_ROLES` (default `admin=admin,member=operator`). Unmapped roles become `viewer`.

| Role | Can |
|---|---|
| viewer | list, read snapshots, stream |
| operator | viewer, plus create sessions, evaluate, execute, complete, mint MCP agents, delete own sessions |
| admin | operator, plus delete any session in the tenant |

Sessions from another tenant answer 404, exactly like missing ones, so their existence is not disclosed. A session the caller can see but not act on answers 403. `GET /api/swarm/me` returns the caller's tenant, role and permissions.

Not included: cross-tenant administration, audit records of access decisions, per-agent service credentials outside MCP proxy mode, and immediate token revocation (threat model G8 still applies).
