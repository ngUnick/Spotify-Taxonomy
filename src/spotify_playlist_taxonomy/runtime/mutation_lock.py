"""Cross-process exclusive lock for guarded catalogue mutations.

The durable database journal remains the source of truth after a crash.  This
lock only prevents two live processes from interleaving preflight, provider
dispatch, read-back, and finalization against the same runtime directory.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO


class MutationLockError(ValueError):
    """Raised when another live process owns the runtime mutation lock."""


@dataclass(frozen=True, slots=True)
class MutationLockStatus:
    state: str
    holder_pid: int | None = None
    holder_purpose: str | None = None
    acquired_at: str | None = None


class RuntimeMutationLock:
    """A non-blocking, process-owned byte-range lock scoped to one runtime.

    Windows uses ``msvcrt.locking`` and POSIX test environments use ``flock``.
    In either case the operating system releases the lock if the owning process
    exits.  The adjacent JSON is diagnostic metadata only and never establishes
    ownership or permits a retry by itself.
    """

    filename = "catalogue-mutation.lock"
    metadata_suffix = ".json"

    def __init__(self, runtime_root: Path, purpose: str) -> None:
        if not purpose or len(purpose) > 128:
            raise ValueError("mutation lock purpose is invalid")
        self.path = runtime_root / "state" / self.filename
        self.metadata_path = self.path.with_suffix(self.path.suffix + self.metadata_suffix)
        self.purpose = purpose
        self._file: BinaryIO | None = None
        self._acquired = False

    def __enter__(self) -> "RuntimeMutationLock":
        self.acquire()
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()

    def acquire(self) -> None:
        if self._acquired:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        try:
            self._ensure_lock_byte(handle)
            if not self._try_lock(handle):
                status = self._metadata_status()
                detail = "mutation lock already held"
                if status.holder_pid is not None:
                    detail += f" (pid={status.holder_pid}"
                    if status.holder_purpose:
                        detail += f", purpose={status.holder_purpose}"
                    detail += ")"
                raise MutationLockError(detail)
            self._file = handle
            self._acquired = True
            self._write_metadata()
        except Exception:
            if self._acquired:
                self.release()
            else:
                handle.close()
            raise

    def release(self) -> None:
        handle, self._file = self._file, None
        acquired, self._acquired = self._acquired, False
        if handle is None:
            return
        try:
            if acquired:
                self._unlock(handle)
        finally:
            handle.close()

    @classmethod
    def inspect(cls, runtime_root: Path) -> MutationLockStatus:
        """Probe a lock without acquiring or changing its diagnostic metadata."""

        path = runtime_root / "state" / cls.filename
        try:
            handle = path.open("r+b")
        except FileNotFoundError:
            return MutationLockStatus("free")
        try:
            if cls._try_lock(handle):
                cls._unlock(handle)
                return MutationLockStatus("free")
            return cls._read_metadata_path(path.with_suffix(path.suffix + cls.metadata_suffix))
        finally:
            handle.close()

    def _metadata_status(self) -> MutationLockStatus:
        return self._read_metadata_path(self.metadata_path)

    @staticmethod
    def _ensure_lock_byte(handle: BinaryIO) -> None:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)

    @staticmethod
    def _try_lock(handle: BinaryIO) -> bool:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    @staticmethod
    def _unlock(handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _write_metadata(self) -> None:
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "purpose": self.purpose,
                "acquired_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        with self.metadata_path.open("wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _read_metadata_path(path: Path) -> MutationLockStatus:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            pid = raw.get("pid")
            purpose = raw.get("purpose")
            acquired_at = raw.get("acquired_at")
            if not isinstance(pid, int) or pid <= 0:
                pid = None
            if not isinstance(purpose, str) or len(purpose) > 128:
                purpose = None
            if not isinstance(acquired_at, str) or len(acquired_at) > 64:
                acquired_at = None
            return MutationLockStatus("held", pid, purpose, acquired_at)
        except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
            return MutationLockStatus("held")
