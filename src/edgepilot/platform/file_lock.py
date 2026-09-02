"""Small cross-platform advisory file lock used by the lightweight agent layer.

The OS primitives below are *inter-process* locks, and on both platforms they
are owned by the open file description rather than by the process. Two threads
of the same process therefore contend with each other, and each platform fails
in its own way: ``fcntl.flock`` blocks forever, while ``msvcrt.locking`` gives
up after ten one-second retries and raises ``OSError: [Errno 36] Resource
deadlock avoided``. The Dashboard serves requests on threads, so concurrent
auth calls hit exactly that.

The fix is a per-path in-process lock in front of the OS lock. Threads queue on
it in order, the OS lock is taken once by the outermost holder, and only real
cross-process contention ever reaches the kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import threading
import time
from typing import IO

# How long to keep retrying a lock another *process* holds before giving up.
# POSIX blocks indefinitely, matching the previous behaviour; only Windows,
# which has no blocking-with-timeout primitive, uses this.
LOCK_TIMEOUT_SECONDS = 30.0
_RETRY_MIN_SECONDS = 0.01
_RETRY_MAX_SECONDS = 0.25


@dataclass
class _ProcessLock:
    """Per-path in-process gate guarding one OS-level lock."""

    lock: threading.RLock = field(default_factory=threading.RLock)
    depth: int = 0
    handle: IO[bytes] | None = None


_REGISTRY: dict[str, _ProcessLock] = {}
_REGISTRY_LOCK = threading.Lock()


def _process_lock(path: Path) -> _ProcessLock:
    key = os.path.normcase(os.path.abspath(str(path)))
    with _REGISTRY_LOCK:
        state = _REGISTRY.get(key)
        if state is None:
            state = _ProcessLock()
            _REGISTRY[key] = state
        return state


def _acquire_os_lock(handle: IO[bytes], timeout: float, path: Path) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return

    import msvcrt

    # msvcrt locks a byte range, so the file needs at least one byte.
    if handle.seek(0, os.SEEK_END) == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + timeout
    delay = _RETRY_MIN_SECONDS
    while True:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            # LK_NBLCK fails immediately while another process holds the range.
            # LK_LOCK would instead retry for a fixed ten seconds and then raise
            # EDEADLK, which reads as a bug rather than as contention.
            if time.monotonic() >= deadline:
                raise TimeoutError(f"could not acquire {path} within {timeout:g}s") from exc
            time.sleep(delay)
            delay = min(delay * 2, _RETRY_MAX_SECONDS)


def _release_os_lock(handle: IO[bytes]) -> None:
    if os.name != "nt":
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class FileLock:
    def __init__(self, path: str, *, timeout: float = LOCK_TIMEOUT_SECONDS) -> None:
        self.path = Path(path)
        self.handle: IO[bytes] | None = None
        self.timeout = timeout
        self._state = _process_lock(self.path)
        self._held = False

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        state = self._state
        state.lock.acquire()
        try:
            if state.depth == 0:
                handle = self.path.open("a+b")
                try:
                    _acquire_os_lock(handle, self.timeout, self.path)
                except BaseException:
                    handle.close()
                    raise
                state.handle = handle
                self.handle = handle
            state.depth += 1
        except BaseException:
            state.lock.release()
            raise
        self._held = True
        return self

    def __exit__(self, *_args: object) -> None:
        if not self._held:
            return
        state = self._state
        try:
            state.depth -= 1
            if state.depth == 0 and state.handle is not None:
                handle, state.handle = state.handle, None
                try:
                    _release_os_lock(handle)
                finally:
                    handle.close()
        finally:
            self.handle = None
            self._held = False
            state.lock.release()
