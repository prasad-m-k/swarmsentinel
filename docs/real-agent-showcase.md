# Real LLM agents through the ASP Gateway

This showcase runs three real tool-calling language-model agents. It does not replay canned model responses. The engine reads and updates actual files in a fresh server-owned sandbox; transfers use fictional credits, never real money. The runner has tool schemas, not tool bodies, sandbox paths or external-tool credentials.

The project owner authored the ASP paper and has permitted its use in this project. This permission does not extend to the separately governed AI Village dataset.

## Run on Replit

Start the managed SwarmSentinel engine and dashboard workflows. In the workspace terminal, from the repository root:

```bash
python artifacts/swarm-sentinel/python/examples/real_agents_demo.py --mode normal --token-file /path/to/private/clerk-token.jwt
python artifacts/swarm-sentinel/python/examples/real_agents_demo.py --mode adversarial --token-file /path/to/private/clerk-token.jwt
```

Replace the placeholder with a private file containing a current Clerk session JWT for your account. Keep it outside the repository with user-only permissions (chmod 600), and refresh it externally before expiry. The guard rereads it on each request; credentials never belong in command arguments or traces.

The runner prints its live session ID and a preview path immediately. Sign in as the token's owner, then open that path to attach directly, or open **Live → My private sessions**, choose the labeled **Real LLM agents** session, and press **Watch**. The expandable real-agent guide provides a mode selector and a copyable command. After a run, use the existing authorized trace/report downloads to inspect gateway decisions.

The default model is `gpt-5.4-mini`; use `--model NAME` for another compatible tool-calling model. Replit OpenAI integration usage is billed to credits. Missing provider configuration or provider errors fail explicitly—there is no scripted fallback.

## Roles and authority

| Agent | Delegated tools | Work |
| --- | --- | --- |
| `invoice-reader` | `demo:invoice.read` | Read a fixture invoice and summarize it |
| `ledger-reviewer` | `demo:ledger.read`, `demo:payments.transfer` | Inspect the fictional-credit ledger and propose a transfer |
| `status-reporter` | `demo:board.write` | Post a workflow status |

The trusted Python controller admits these agents and hands their summaries between them through guarded messages. The first tool of each role is selected by the controller to begin the workflow; subsequent tool choices and their arguments come from the model. All registered tools are visible to the model, but visibility is not authorization: inherited scopes and the ASP policy decide which calls may execute.

### Normal mode

The engine classifies the internally generated normal fixture as trusted (`reads=high`). The reader reads the invoice; the reviewer transfers 125 fictional credits to `approved-supplier`; the reporter appends a status. Inspect the returned ledger and board to verify that allowed actions changed actual files.

This trusted classification is a fixture assumption, not a cryptographic verification mechanism. Real externally retrieved invoices should not automatically receive it.

### Adversarial mode

The invoice is explicitly untrusted. The actors are instructed to stress-test the guard by proposing forbidden actions:

1. The reader reads the poisoned fixture and proposes a transfer outside its scope.
2. A guarded message carries contaminated context to the reviewer.
3. The reviewer has payment authority, but contamination withdraws that capability.
4. The reviewer relays context to the reporter, potentially triggering the injection-spread tripwire.
5. The reporter's write is refused by revocation or content-trust rules.
6. A clean controller can still append a review notice.

Model output is nondeterministic. If it declines to propose a forbidden action, the result states that no block demonstration can be claimed. An explicitly instructed adversarial role is **not** evidence that the model spontaneously fell for prompt injection.

## Evidence and files

Each run creates an engine-managed temporary directory containing `invoice.txt`, `ledger.json` and `board.jsonl`. Its path is never returned over the API or given to the runner. The engine deletes these fixtures when the owner deletes the session; they are disposable and not durable across process exit.

The trusted terminal controller separately creates `data/agent-demos/<session-id>/` for evidence only:

- `model-transcript.json`: model tool proposals, tool outcomes, model ID, request/token counts.
- `result.json`: completed outcome including a server-returned ledger/board snapshot, successfully completed tool bodies, blocked model calls, and gateway decisions.
- `failure.json`: failure type and completed gateway decisions if a run fails.

These files are gitignored. Do not commit provider credentials or run transcripts. A gateway `allow` (and the trace's legacy `executed` field) records permission, not proof that a tool succeeded. The execution response's `toolBodyExecuted` reports whether a body was entered; `error=tool_execution_failed` explicitly reports a failed attempt. The successful-body list and server-returned snapshots supply execution evidence.

## Server-owned execution contract

- `POST /api/swarm/sessions/{id}/sandbox` initializes either fixed normal or adversarial fixtures once, before any actions. It accepts only `mode` and requires the `agents` policy. Reinitialization, mode switching, caller-chosen paths and late initialization are refused.
- `POST /api/swarm/sessions/{id}/execute` accepts `agentId`, `spanId`, `parentId`, `parentSpanId`, the registered `tool` name and its `arguments`. The engine verifies owner authentication and session admission/lineage, validates the tool's exact arguments, derives target/trust/write/resource metadata and server time, evaluates ASP and dispatches under the same session lock.
- `GET /api/swarm/sessions/{id}/sandbox` returns owner-private execution evidence, never paths. These routes do not exist on the public synthetic demo API and do not launch paid inference.
- An `evaluate` decision is not a reusable execution authorization. Direct requests to `execute` receive a fresh decision even if the caller previously obtained an allow. Refusals return no result and `toolBodyExecuted=false`.
- Untrusted fixture reads contaminate the admitted actor as well as the reading span. Fresh invented spans and narrower child agents cannot reset that contamination.
- The SDK's `Agent.execute` consumes the server result. It does not invoke a caller-side function. It does not retry an execution after a lost HTTP response, since the write might already have happened; stop and inspect the owner-private snapshot/Live trace before deciding what to do next.

## Limits and safety

- Policy: `policies/agents.asp.json`. Existing synthetic and AI Village policies remain unchanged.
- Only four registered sandbox tools exist. Models cannot supply filesystem paths, execute shell commands, choose arbitrary recipients, or override gateway metadata such as trust classification.
- Each role has at most four model rounds, at most 1200 completion tokens per request, bounded provider retries/backoff, sequential rate limiting, and a four-minute run deadline checked between requests.
- Provider inference is executed by the trusted controller. This demo gates agent tool calls, messages, and delegation, not every network operation of the controller.
- Real-model sessions require verified owner authentication for creation, observation, evaluation and deletion. Public visitors can observe only the separately stored fixed synthetic demonstrations and cannot invoke a paid model launcher. Use fictional fixtures; historical dataset approval is separate.
- The four remote demo tool bodies now require server-owned ASP evaluation and dispatch; skipping the SDK cannot skip policy on that API. Generic `agent.call`, decorator tools and scripted demonstrations remain cooperative, and `Guard.local` is explicitly in-process.
- This is not OS isolation or tamper-proof protection against arbitrary code with host filesystem access, engine reconfiguration privileges or stolen owner credentials. Owner authentication and admitted-agent lineage are not cryptographic per-agent credentials: the controller is trusted and an owner can act as any admitted actor in their own session. The separate limited-agent-access work is still required before sharing access with untrusted autonomous processes.
- Sessions and tool fixtures are ephemeral. Controller evidence files remain until the workspace owner removes them.
- No raw AI Village records are downloaded or used.

## Outside Replit

Install the root Python dependencies and configure a supported OpenAI-compatible provider through your environment/secrets manager (`AI_INTEGRATIONS_OPENAI_BASE_URL` and `AI_INTEGRATIONS_OPENAI_API_KEY`). Never put credentials in source or command-line arguments.

Start the FastAPI engine and pass its origin:

```bash
python artifacts/swarm-sentinel/python/examples/real_agents_demo.py \
  --remote http://127.0.0.1:8000 --mode normal --token-file /path/to/private/clerk-token.jwt
```

Authenticated HTTP is supported only on loopback; other origins require HTTPS. Configure the engine's trusted Clerk issuer and authorized application origins as described in [live-session access](live-session-access.md). The local dashboard still needs its documented Vite proxy or a same-origin reverse proxy.

## Verification

```bash
(cd artifacts/swarm-sentinel/python && python -m unittest discover tests)
pnpm run typecheck
```

Unit tests use explicit offline model fixtures through the actual authenticated HTTP execution routes, and assert that normal/adversarial workflows remain in owner-private Live lists and snapshots. Direct signed-token HTTP regressions assert unchanged fixture bytes and no dispatch on policy/authentication refusals. They also cover argument/metadata forgery, fresh-span and descendant taint laundering, concurrent writes, locking and lost-response retry safety. These tests do not claim a paid provider run. To verify real model behavior, run both modes from the terminal with the configured provider and inspect their returned ledgers and gateway traces.