# Private live-session access

## Trust boundary

The Python engine verifies Clerk session JWTs directly. There is no trusted-user
header and no reliance on a frontend-only sign-in gate. Private HTTP sessions are
created with the verified Clerk subject as their owner. Only that owner may list,
inspect/export, evaluate, stream or delete them. Foreign and missing IDs return the
same 404; anonymous/invalid sessions return 401; missing auth configuration or an
unreachable identity service returns 503. The HTTP response cache is disabled.

Browser calls use Clerk's `__session` cookie, including native EventSource. POST
and DELETE with cookies also require an approved Origin. Remote SDK calls use a
Clerk session JWT via `Guard.remote(url, token_provider=...)`; the provider must
return a refreshed token before expiry. Use HTTPS except on loopback. Credentials
must never be put in URLs, committed, or copied into trace fields. There is no
custom password, arbitrary bearer key, or client-supplied owner scheme.

Clerk is provisioned through Replit. The frontend includes path-based sign-in and
sign-up routes; the shared API service provides the Clerk production proxy. No
deployment was performed as part of this change.

## Configuration

- `CLERK_PUBLISHABLE_KEY`: provisioned by the authentication setup. The engine
  derives its trusted HTTPS issuer and JWKS URL from this configuration, not from
  untrusted token claims. The frontend uses `VITE_CLERK_PUBLISHABLE_KEY`.
- `SWARM_AUTHORIZED_PARTIES`: comma-separated exact trusted browser origins,
  such as `https://your-approved-host`. In development the runtime-provided
  `https://REPLIT_DEV_DOMAIN` is also accepted. Local development needs an explicit
  origin (for example `http://localhost:5173`). Do not use wildcards.
- `VITE_CLERK_PROXY_URL`: managed production frontend proxy configuration.
  Empty in development; keep the shared API service available for `/api/__clerk`.
- `SWARM_ENABLE_VILLAGE=true` **and** `SWARM_VILLAGE_READERS`: an explicit
  comma-separated allowlist of approved Clerk subjects is required for private
  AI Village metadata/replay. Both are disabled/unset by default. Enabling these
  is a separate approval decision; this task did not download or build a store.
  Only allowlisted users share access to the approved historical store; live
  sessions remain individually owner-only.

## Intentional public demonstrations

`/api/swarm/demo/injection` only runs the fixed, hand-authored synthetic attack.
It accepts pacing and feedback options, never caller-supplied traces. Resulting
sessions live in separate bounded storage. Public listing, snapshots/reports and
SSE use `/api/swarm/demo/sessions...`; there are no public evaluation or deletion
routes. Private IDs never resolve through these routes. The dashboard calls these
records **PUBLIC SYNTHETIC**; regular synthetic `/simulate` scenarios remain public.
The private API does not expose the demo store.

## Dashboard and limits

Select Live → My private sessions after signing in. Create a session, attach a
remote guard using the returned session ID and the same owner's verified token,
then Watch to see updates. JSON and Markdown exports recheck access by fetching
an authorized snapshot. Delete removes the in-memory session after confirmation.
The terminal real-agent showcase also requires an owner JWT via `--token-file`.
Keep that file outside the repository with user-only permissions (chmod 600) and
refresh it externally before expiry; the guard rereads it for each request.
Session labels and terminal dashboard deep links remain supported, but never
grant access: sign in as the token's owner to open the linked private session.
Switching account or live scope clears local traces and closes existing streams.
Reconnects fetch a fresh snapshot/cursor, preventing duplicate trace accumulation.

JWT verification is offline: stolen/revoked tokens may work until their short
expiry. Streams also stop at expiry. Immediate revocation, machine identities,
per-agent credentials, persistence, rate limiting and byte-bounded trace retention
remain gaps; see `THREAT_MODEL.md`. New private sessions never evict other owners.

## Regression checks

From `artifacts/swarm-sentinel/python`:

```sh
python -m unittest discover -s tests
```

Access tests generate ephemeral signing keys and synthetic markers locally. They
do not use production secrets, live agent data or datasets. OpenAPI is maintained
in `lib/api-spec/openapi.yaml`; regenerate both clients with:

```sh
pnpm --filter @workspace/api-spec run codegen
pnpm run typecheck
```