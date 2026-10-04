"""Keyed caller journal. Private local files; no engine durability claims."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import tempfile
import threading


class JournalError(RuntimeError):
    """Journal unavailable or incompatible; no unjournaled execution is safe."""


def canonical(value):
    # Match the server's JSON equality: order is irrelevant, numeric types are not.
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _validate_execution(entry, root, key):
    if not isinstance(entry, dict) or entry.get("state") not in ("pending", "completed"):
        raise ValueError("Invalid saved execution state")
    expected = {"state", "payload", "lineage"}
    if entry["state"] == "completed":
        expected.add("outcome")
    if set(entry) != expected:
        raise ValueError("Invalid saved execution fields")
    payload, lineage = entry["payload"], entry["lineage"]
    fields = ("agentId", "spanId", "parentId", "parentSpanId", "tool", "idempotencyKey")
    if (not isinstance(payload, dict) or set(payload) != set(fields) | {"arguments"}
            or not all(isinstance(payload[k], str) for k in fields)
            or not isinstance(key, str) or not key or payload["idempotencyKey"] != key
            or not isinstance(payload["arguments"], dict)
            or not isinstance(lineage, list) or not lineage):
        raise ValueError("Invalid saved execution")
    for agent in lineage:
        if (not isinstance(agent, dict) or set(agent) != {"name", "span"}
                or not all(isinstance(v, str) and v for v in agent.values())):
            raise ValueError("Invalid saved lineage")
    parent = lineage[-2] if len(lineage) > 1 else {"name": "", "span": ""}
    if (lineage[0]["name"] != root
            or payload["agentId"] != lineage[-1]["name"]
            or payload["spanId"] != lineage[-1]["span"]
            or payload["parentId"] != parent["name"]
            or payload["parentSpanId"] != parent["span"]):
        raise ValueError("Saved lineage does not match execution")
    if entry["state"] == "completed" and not isinstance(entry["outcome"], dict):
        raise ValueError("Invalid saved outcome")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate journal field or execution key")
        result[key] = value
    return result


class PendingJournal:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.windows = None
        if os.name == "nt":
            try:
                from ._windows_journal import WindowsJournalIO
                self.windows = WindowsJournalIO(self.path)
                self.path = self.windows.path
            except (OSError, ImportError, AttributeError) as e:
                raise JournalError("Cannot initialize private Windows execution journal") from e

    @contextmanager
    def locked(self):
        # Lock a stable sibling, not the file replaced by atomic commits.
        with self.lock:
            fd = None
            try:
                if self.windows is not None:
                    fd = self.windows.open_fd(str(self.path) + ".lock", create=True, lock=True)
                else:
                    import fcntl
                    fd = os.open(str(self.path) + ".lock",
                                 os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
                    os.fchmod(fd, 0o600)
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, ImportError, AttributeError) as e:
                if fd is not None:
                    os.close(fd)
                raise JournalError("Cannot lock execution journal; use one caller per journal") from e
            try:
                yield
            finally:
                os.close(fd)

    def read(self):
        return self._read_snapshot()[0]

    def _read_snapshot(self):
        """Read validated contents and byte size from the same private file handle."""
        try:
            fd = (self.windows.open_fd(self.path, read=True) if self.windows is not None
                  else os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW))
        except FileNotFoundError:
            return None, 0
        except OSError as e:
            raise JournalError("Cannot read execution journal") from e
        try:
            with os.fdopen(fd, encoding="utf-8") as stream:
                size_bytes = os.fstat(stream.fileno()).st_size
                record = json.load(stream, object_pairs_hook=_unique_object)
            if (not isinstance(record, dict) or type(record.get("version")) is not int
                    or record["version"] not in (1, 2)
                    or not all(isinstance(record.get(k), str) and record[k]
                               for k in ("sessionId", "root", "endpoint"))):
                raise ValueError("Invalid journal header")
            header = {"version", "endpoint", "sessionId", "root"}
            if record["version"] == 1:
                # Normalize old single-slot journals without replacing their binding.
                state = record["state"]
                if state not in ("idle", "pending", "completed"):
                    raise ValueError("Invalid legacy journal state")
                entry = {k: v for k, v in record.items() if k not in header}
                entries = {}
                if state == "idle":
                    if entry != {"state": "idle"}:
                        raise ValueError("Invalid legacy idle journal")
                else:
                    key = record["payload"]["idempotencyKey"]
                    _validate_execution(entry, record["root"], key)
                    entries[key] = entry
                record = {k: record[k] for k in header}
                record.update(version=2, executions=entries)
            if set(record) != header | {"executions"} or not isinstance(record["executions"], dict):
                raise ValueError("Invalid journal fields")
            for key, entry in record["executions"].items():
                _validate_execution(entry, record["root"], key)
            canonical(record)
            return record, size_bytes
        except (OSError, ValueError, TypeError, KeyError) as e:
            raise JournalError("Invalid execution journal; refusing to start a replacement session") from e

    def summary(self, *, expected_binding=None):
        """Opt-in, payload-free inspection; never log, rewrite, or remove entries.

        Size is the journal file's logical bytes, excluding lock/temporary files.
        expected_binding optionally checks the calling guard's saved session.
        """
        with self.locked():
            record, size_bytes = self._read_snapshot()
            if record is None or (expected_binding is not None and any(
                    record.get(key) != value for key, value in expected_binding.items())):
                raise JournalError("Saved journal binding changed or disappeared")
            entries = record["executions"]
            pending = sum(entry["state"] == "pending" for entry in entries.values())
            return {
                "size_bytes": size_bytes,
                "total_entries": len(entries),
                "pending_entries": pending,
                "completed_entries": len(entries) - pending,
            }

    def _replace(self, temporary):
        if self.windows is not None:
            self.windows.replace(temporary)
            return
        os.replace(temporary, self.path)
        directory = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def write(self, record):
        temporary = None
        try:
            data = canonical(record)
            if self.windows is not None:
                # Refuse unsafe existing destinations, including reparse points.
                try:
                    existing = self.windows.open_fd(self.path, read=True)
                except FileNotFoundError:
                    pass
                else:
                    os.close(existing)
                fd, temporary = self.windows.temporary()
            else:
                fd, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(data)
                stream.flush()
                if self.windows is not None:
                    self.windows.flush(stream.fileno())
                else:
                    os.fsync(stream.fileno())
            self._replace(temporary)
        except (OSError, ValueError, TypeError) as e:
            raise JournalError("Cannot commit execution journal; retain it and recover the saved key") from e
        finally:
            if temporary and os.path.exists(temporary):
                os.unlink(temporary)