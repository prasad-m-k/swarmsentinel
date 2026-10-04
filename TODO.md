# TODO — deferred beyond the PoC

SwarmSentinel is a proof of concept. These suggested follow-ups are recorded
for the real implementation, not scheduled for implementation as part of the
PoC. Existing work already in progress or queued is unchanged.

## Features and operational hardening

- [ ] **Notify owners about missing completion reports when Live is closed.**
  Provide owner-private notifications without requiring the Live dashboard
  to remain open.
- [ ] **Keep an audit history when late reports clear completion warnings.**
  Retain the original warning and its resolution when a caller reports a
  tool outcome after the warning was raised.
- [ ] **Give automated agents limited access without sharing a user's full session.**
  Provide narrowly scoped machine access rather than sharing a user's
  full authenticated session.
- [ ] **Prevent heavy demo or agent traffic from exhausting the live recorder.**
  Add load controls while preserving execution evidence and retry receipts
  needed for safe recovery.

## Validation before a production release

- [ ] **Keep caller recovery responsive when many results await acknowledgement.**
  Test recovery performance with a large backlog of unacknowledged results,
  while preserving durable records and safe retries.
- [ ] **Catch private Live filter and account-switch regressions before release.**
  Cover result filters and account changes with regression tests, including
  checks that one user's private session data never appears for another user.