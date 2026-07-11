"""Cross-process shared/exclusive file lock for SQLite authority cutover."""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import BinaryIO


class CoordinationLockTimeout(TimeoutError):
    pass


class CoordinationFileLock:
    """Shared lock for normal DB access; exclusive lock for maintenance cutover."""

    def __init__(
        self,
        path: Path | str,
        *,
        shared: bool,
        timeout_seconds: float = 60.0,
        poll_seconds: float = 0.05,
    ) -> None:
        self.path = Path(path)
        self.shared = bool(shared)
        self.timeout_seconds = max(0.1, float(timeout_seconds))
        self.poll_seconds = max(0.01, float(poll_seconds))
        self._handle: BinaryIO | None = None

    def __enter__(self) -> "CoordinationFileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
            os.fsync(handle.fileno())
        deadline = time.monotonic() + self.timeout_seconds
        while True:
            try:
                self._try_lock(handle)
                self._handle = handle
                return self
            except (BlockingIOError, OSError) as exc:
                if time.monotonic() >= deadline:
                    handle.close()
                    raise CoordinationLockTimeout(
                        f"timed out acquiring {'shared' if self.shared else 'exclusive'} "
                        f"coordination lock: {self.path}"
                    ) from exc
                time.sleep(self.poll_seconds)

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            self._unlock(handle)
        finally:
            handle.close()

    def _try_lock(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_NBRLCK if self.shared else msvcrt.LK_NBLCK
            msvcrt.locking(handle.fileno(), mode, 1)
            return
        import fcntl

        mode = fcntl.LOCK_SH if self.shared else fcntl.LOCK_EX
        fcntl.flock(handle.fileno(), mode | fcntl.LOCK_NB)

    def _unlock(self, handle: BinaryIO) -> None:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
