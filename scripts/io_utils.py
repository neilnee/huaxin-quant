"""Crash-safe local file writes and advisory process locks.

These helpers protect engineering persistence only. They do not transform
payloads or apply business rules.
"""

from __future__ import annotations

import csv
import fcntl
import json
import os
import socket
import tempfile
from contextlib import AbstractContextManager
from datetime import datetime
from pathlib import Path
from typing import Iterable, Mapping, Sequence


class LockBusyError(RuntimeError):
    """Raised when a non-blocking lock is already held by another process."""


class FileLock(AbstractContextManager):
    """Advisory lock backed by a stable sidecar file.

    The kernel releases the lock if the owner exits, so stale metadata never
    blocks a future run. The metadata is diagnostic only.
    """

    def __init__(self, path: Path | str, *, blocking: bool = True, purpose: str = ""):
        self.path = Path(path)
        self.blocking = blocking
        self.purpose = purpose
        self._handle = None

    def acquire(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        operation = fcntl.LOCK_EX | (0 if self.blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(handle.fileno(), operation)
        except BlockingIOError as exc:
            handle.seek(0)
            owner = handle.read().strip() or "owner metadata unavailable"
            handle.close()
            raise LockBusyError(f"lock already held: {self.path} ({owner})") from exc
        metadata = {
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "purpose": self.purpose,
        }
        handle.seek(0)
        handle.truncate()
        json.dump(metadata, handle, ensure_ascii=False)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        self._handle = handle
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None

    def __enter__(self) -> "FileLock":
        return self.acquire()

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


def _atomic_replace(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def atomic_write_text(path: Path | str, value: str) -> None:
    _atomic_replace(Path(path), lambda handle: handle.write(value))


def atomic_write_json(path: Path | str, value, *, indent: int | None = 2) -> None:
    def write(handle) -> None:
        json.dump(value, handle, ensure_ascii=False, indent=indent)
        handle.write("\n")

    _atomic_replace(Path(path), write)


def atomic_write_csv(
    path: Path | str,
    fieldnames: Sequence[str],
    rows: Iterable[Mapping],
    *,
    encoding: str = "utf-8-sig",
) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    mode = target.stat().st_mode & 0o777 if target.exists() else 0o644
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            writer.writeheader()
            for row in rows:
                writer.writerow({field: row.get(field, "") for field in fieldnames})
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary.exists():
            temporary.unlink()
