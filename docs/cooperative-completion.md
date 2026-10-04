# Completion receipts for tools outside the sandbox

The owner-private gateway still decides **before** a tool runs. Its `allowed`
and recorder `executed` flags mean permission, not successful completion.
Allowed permission-only `/evaluate` tool calls remain unobserved until an
explicit receipt is submitted.

## Cooperative SDK

Use `Guard.remote` with the existing owner token provider. `Agent.call` and
`@agent.tool(...)` now submit one completion receipt after an allowed body returns
or raises an ordinary exception. The function's return value remains local; the
SDK re-raises the original tool exception. A refused call never enters the body
and never submits a receipt.

For code that executes outside the wrapper:

```python
decision = agent.check("tool", "mock:fs.read", intent="read approved data")
if not decision.allowed:
    raise PolicyViolation(decision)
try:
    result = external_tool()
except Exception:
    agent.complete(decision, "failed")
    raise
else:
    agent.complete(decision, "succeeded")
```

`Agent.complete` sends only the event ID, actor ID, span ID, opaque receipt token,
and `succeeded` or `failed`. Do not put the token in logs or telemetry exports.
No results, exception messages, credentials, or paths belong in a receipt.
Failed receipts use the fixed `tool_execution_failed` code.

Receipt transport does **not** automatically retry. If a successful tool's
receipt is refused or its response is lost, `CompletionReportError` exposes the
original decision and status, and explicitly warns not to run the body again.
Inspect the owner's session snapshot first; the receipt may already have been
accepted. When both the body and receipt fail, the wrapper preserves the original
body exception and adds a fixed note that the receipt was not acknowledged.
Process interruption can leave completion unknown; this is not sandbox retry
recovery, durable execution, or an exactly-once guarantee.

Synthetic local/demo sessions do not issue receipt tokens and retain their old
semantics. This feature does not introduce limited agent credentials or grant
historical store access.

## HTTP contract and evidence

`POST /api/swarm/sessions/{sessionId}/completion` requires the same authenticated
owner as the evaluated session. Supply `CompletionReceipt` from the OpenAPI
contract, including the `completionToken` returned by `/evaluate`. The capability
binds the exact session/event even when two sessions have identical sequential
event IDs and agent spans. Actor and span must also match.

Only an allowed permission-only **tool** event can receive one terminal update.
Refused events, messages, spawns, engine-owned sandbox execution, mismatched
bindings, duplicate receipts (even identical ones), and contradictory receipts
are rejected. Invalid receipts return a fixed validation message without echoing
submitted fields.

The original trace is updated, not appended:

- `executionProvenance: engine-observed` is engine evidence from registered
  sandbox dispatch (or engine-observed refusal before body entry).
- `executionProvenance: caller-reported` is the owner's assertion about an
  external body, **not engine verification**.
- Unknown permission-only outcomes omit the evidence fields.
- Failure can follow partial side effects; it does not prove rollback. Neither
  success provenance alone proves which side effects occurred.

Private SSE messages include `updates`, full existing rows keyed by event ID.
Consumers replace those rows without advancing action counts; `events` remains
new admissions only. Existing evidence is resent on subscription to close the
snapshot/subscribe race, and the dashboard fetches a fresh snapshot on reconnect.
Snapshots, JSON exports, row details, outcome counts, and Markdown reports retain
the provenance distinction. Admission-based safety accounting, graph weights,
detector inputs and policy history do not change.

## Missing-completion visibility

Create an owner-private session with `completionGraceSeconds` in the
`POST /api/swarm/sessions` body (default **60**, integer range **1–86400**).
The dashboard's **Receipt grace (s)** input applies to the next private session
you create; attached sessions retain their own configured grace.

Allowed external tool admissions without receipts become **Missing completion**
warnings after that interval. The timer uses server admission time, not the
caller's supplied trace timestamp. This is not a tool timeout: the tool could
still be running, or the caller could have stopped before or after body entry.
No body entry, failure, rollback, or retry safety is inferred.

Private snapshots and JSON exports include `completionGraceSeconds` and
`completionWarnings`; reports have a separate visibility-only section. Private
SSE messages supply a full replacement `completionWarnings` array on subscription
and whenever it changes, including `[]` when late receipts clear warnings.
These changes do not add recorder actions or change detections, revocations,
graph weights, or admission-based safety accounting.

The receipt capability remains usable after the grace period. An accepted late
receipt clears its warning and records the existing caller-reported outcome.
Warnings apply neither to denied tools, non-tool actions, engine-dispatched
sandbox tools, public synthetic demos, nor historical replay records.

## Regression checks

```sh
cd artifacts/swarm-sentinel/python
python -m unittest discover -s tests
# From the workspace root:
node --experimental-strip-types --test artifacts/swarm-sentinel/tests/live-events.test.ts
pnpm --filter @workspace/api-spec run codegen
pnpm --filter @workspace/swarm-sentinel run typecheck
```