"""Host-wide run lock, so a backup never overlaps another backup, prune or restore."""

from __future__ import annotations

import contextlib
import fcntl
import os
from collections.abc import Iterator
from pathlib import Path

from drkit.errors import ConfigError, LockHeldError

DEFAULT_LOCK_PATH = Path("/run/drkit.lock")


@contextlib.contextmanager
def acquire(path: Path = DEFAULT_LOCK_PATH) -> Iterator[None]:
    """Hold an exclusive, non-blocking ``flock`` on ``path`` for the duration of the block.

    The kernel releases the lock when the process dies, so a crashed run never leaves a stale
    lock behind. The holder's PID is written to the file for diagnostics only.
    """
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise ConfigError(
            f"cannot open lock file {path}: {exc.strerror}",
            hint="run drkit as root, or check that the lock directory exists and is writable",
        ) from exc
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            holder = _read_pid(fd)
            raise LockHeldError(
                f"another drkit process{f' (pid {holder})' if holder else ''} holds {path}",
                hint="wait for it to finish; the lock is released automatically when it exits",
            ) from exc
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        try:
            yield
        finally:
            os.ftruncate(fd, 0)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_pid(fd: int) -> int | None:
    os.lseek(fd, 0, os.SEEK_SET)
    content = os.read(fd, 32).decode(errors="replace").strip()
    return int(content) if content.isdigit() else None
