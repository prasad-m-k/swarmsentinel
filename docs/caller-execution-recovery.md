# Recovering sandbox execution after a caller restart

The remote Python SDK can opt into a **keyed, multi-execution journal**.
It protects retries when the caller process exits, including after the engine
dispatches a tool but before the caller saves the response. It does not make
the engine's in-memory sessions persistent.

```python
from sdk.guard import Guard

guard = Guard.remote(
    engine_url,
    token_provider=get_current_owner_token,
    journal_path="data/caller-recovery/payment.json",
    policy="agents",
)
guard.configure_sandbox("normal")  # first initialization only
reviewer = guard.root().spawn(
    "reviewer", scope=["demo:ledger.read", "demo:payments.transfer"]
)
outcome = reviewer.execute(
    "transfer_funds", {"amount": 125, "recipient": "approved-supplier"},
    idempotency_key="invoice-4471-payment",
)
# Only after your application has consumed the result:
guard.acknowledge_execution("invoice-4471-payment")
```

Create the private parent directory first. The SDK writes the session binding,
exact execution payload (including key and spans), and full caller ancestor
lineage before sending execution. After a response, it atomically records the
outcome before invoking `on_decision`. Files are private (POSIX mode `0600`, or a
protected current-user-only Windows DACL), writes use durable replacement, and a sibling lock
protects short read/modify/write commits. The journal lock is released before
network requests, and the transport uses one HTTP connection per calling thread.
Parallel agents sharing a guard can therefore save and dispatch independently
keyed requests without overwriting other pending calls or completed receipts.

After restarting the caller, reuse the same endpoint and journal:

```python
guard = Guard.remote(
    engine_url,
    token_provider=get_current_owner_token,  # fresh credentials, never journaled
    journal_path="data/caller-recovery/payment.json",
)
# Includes completed receipts that the application has not yet acknowledged.
for key, entry in guard.saved_executions().items():
    outcome = guard.recover_execution(key)  # original payload, session and spans
    consume_result(key, outcome)             # your application's result handling
    guard.acknowledge_execution(key)

# Inspect only unresolved requests without making network calls:
pending = guard.pending_executions()  # {key: detached payload}
# Before acknowledging a saved execution, restore its actor if needed:
# reviewer = guard.resume_agent(key)  # ancestor spans restored; no spawn calls
```

Do not configure the sandbox again, respawn the agent, or construct fresh spans
for a pending execution. An exact retry from `resume_agent(key).execute(...)` is
also supported when you explicitly supply `idempotency_key=key`. Reusing a saved
key with changed arguments, tool, actor, or lineage is refused locally. JSON
member order is irrelevant; numeric type changes are not. New explicit keys are
independent invocations, including while older requests are unresolved.

**An omitted execution key always creates a new invocation.** It never guesses
which pending call to retry. If a response is lost, `ExecutionUncertain` exposes
the generated key; use `recover_execution(error.idempotency_key)`, or discover it
with `pending_executions()` after restart. Do not rerun an uncertain call with a
fresh key, since that can cause another side effect.

For compatibility, `recover_execution()` and `resume_agent()` accept an omitted
key only when exactly one saved entry exists; otherwise they raise `JournalError`
before sending anything. `pending_execution(key)` returns that payload or `None`
if completed. Without a key it returns the sole pending payload, `None` if none,
and raises if several are pending.

Recovery authenticates against the original owner-private route even for a
locally completed receipt. The engine returns the original success, policy
refusal, or tool failure without dispatching again. A subsequent ordinary
`execute` after completion is a **new invocation**, unless you explicitly supply
the old key. Every request remains saved until explicit
`acknowledge_execution(key)` removes its **completed** receipt. Acknowledgement
refuses unresolved and unknown keys; it does not remove sibling entries, alter
the engine's retry receipts, or authorize rerunning a tool. Do not acknowledge a
key while another thread is still dispatching or recovering that same key.
There is no automatic eviction or size limit: callers must acknowledge consumed
results to control file growth (see the opt-in inspection below). Acknowledgement is not an atomic transaction
with your application's result handling; make that handling idempotent by key.
Existing version-1 journals migrate with their session, key, payload, and lineage
intact, including completed receipts.

`SessionUnavailable` means the saved session is expired, deleted, or inaccessible
to the current owner (HTTP 404/410). The SDK keeps the journal and never creates a
replacement session or changes the key. Engine restarts lose all session state,
so recovery is not possible after an engine restart. Other HTTP errors, including
authentication failures and key conflicts, remain `GatewayError`; transport
failures expose the saved key through `ExecutionUncertain`. Journal read/write,
binding, corruption, and concurrent-use failures raise `JournalError`, never
falling back to an unjournaled execution.

This file is sensitive caller data: it contains tool arguments and results.
Keep it in a private, untracked directory, not in source control or a shared
folder. Credentials and HTTP headers are not stored; do not put credentials
in tool arguments. One file is for one session/caller, which may run multiple
agents in parallel. Independent callers should use separate files; simultaneous
file-lock contention between caller instances fails explicitly.
An empty journal has no execution to recover.
Leaving `journal_path` unset preserves the existing non-persistent SDK behavior.
The local guard and externally executed `Agent.call` wrappers are not journaled.

## Inspect file growth without exposing requests or results

Opt in to inspection by calling `guard.journal_summary()` at startup or periodically
in your application. It makes no network request and returns only four integers:
`size_bytes`, `total_entries`, `pending_entries`, and `completed_entries`.
Completed entries include successes, refusals, and tool failures awaiting explicit
acknowledgement; pending entries are unresolved requests, not necessarily tools
that have not run.

```python
summary = guard.journal_summary()
# Example advisory thresholds; choose these for your application's workload.
if summary["size_bytes"] >= 10 * 1024 * 1024 or summary["total_entries"] >= 1000:
    logger.warning(
        "Caller recovery journal: bytes=%d entries=%d pending=%d completed=%d. "
        "Recover pending keys in the original session; consume completed results "
        "and explicitly acknowledge only consumed keys.",
        summary["size_bytes"], summary["total_entries"],
        summary["pending_entries"], summary["completed_entries"],
    )
```

The SDK never emits this warning automatically. The summary excludes paths,
session IDs, execution keys, lineage, arguments, outcomes, and credentials.
Do not log `saved_executions()` or `pending_executions()` to diagnose growth:
those APIs expose sensitive recovery payloads. Direct storage users can call
`PendingJournal(path).summary()` without attaching to a remote guard.

Byte size is the actual journal file length from the same safely opened handle
used to validate its contents, including JSON encoding and any whitespace.
It excludes the sibling lock, abandoned temporary files, filesystem allocation
overhead, engine recorder traffic, and persistent engine storage. Counts and size
are one locked snapshot, which may change immediately after inspection. Inspection
validates the file and guard binding, raises `JournalError` for missing/corrupt
files or lock/storage failures, and never rewrites even a legacy journal.
Like other journal reads, it parses the whole file; choose a polling interval
appropriate for large files rather than inspecting on every execution.

These thresholds are advisory, not quotas. Crossing one never evicts unresolved
requests, acknowledges results, or blocks dispatch. Recover pending executions
using their saved keys and original session, then consume results idempotently
and explicitly acknowledge each consumed completed key. Do not delete, rotate,
truncate, or replace the journal to silence an alert. Without `journal_path`,
`journal_summary()` raises `JournalError`; existing non-journaled SDK use is unchanged.

## Platform and storage requirements

- **Linux/macOS (POSIX):** nonblocking `flock` on a stable sibling `.lock` file,
  no-follow file opens, `0600` temporary files, file `fsync`, atomic `os.replace`,
  and parent-directory `fsync`. The filesystem must honor these primitives.
- **Windows:** standard-library native Win32 calls; no extra SDK dependency.
  Use an ordinary path on a **fixed local NTFS volume**, inside a private
  directory controlled by the caller. UNC/network paths, non-NTFS/removable
  volumes, alternate data streams, directory reparse points, and ambiguous
  trailing-dot/space paths are refused. File reparse points and hard links are
  refused too. Newly created journal, temporary, and lock files have an explicit
  protected DACL granting full access only to the process user's SID and are
  owned by that user. Existing files must match that private security descriptor;
  permissions are not silently repaired. A zero-share, non-inheritable handle
  on the stable `.lock` file excludes competing processes, including deletion
  or renaming of that lock. Closing the handle or terminating the process releases
  it; an abandoned lock file is not itself a held lock.

Windows commits flush a private, same-directory temporary file using
`FlushFileBuffers` before calling `MoveFileExW` with
`MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH`. There is no cross-volume
copy fallback, plain `os.replace` fallback, or ignored synchronization error.
This is the Windows durable-move primitive, not an unsupported directory `fsync`.
Both platforms require storage that honors flush requests; neither protects
against faulty hardware, malicious administrators, or a filesystem that lies
about persistence. A process interrupted before replacement leaves the prior
record; one interrupted after replacement leaves the new complete record.
An interruption before replacement may leave a private temporary file: it is
never selected as a recovery source. Retain the original journal and recover
its saved keys after any uncertain commit; never create a new session or key
to work around a write failure.

Do not copy a Windows journal into a broadly accessible file and then opt in.
If an existing file's ACL is refused, have its owner secure it while no caller
is running, preserving its contents and session binding. Do not delete a held
lock file or share the parent directory with untrusted writers on either platform.
Credentials still come from `token_provider` on each request and are never saved.
Unsupported platforms or storage/security failures raise `JournalError` before
an execution can be sent; the SDK without `journal_path` remains usable.

Run the same recovery and storage tests on each supported host:

```sh
cd artifacts/swarm-sentinel/python
python -m unittest discover -s tests -p test_journal.py -v
```

The suite includes real process exits after dispatch, forced termination while
holding a lock, competing processes during data-file replacement, and exits
before/after replacement. Windows-only tests additionally exercise native
security checks. Passing POSIX tests alone does not validate the Win32 APIs.