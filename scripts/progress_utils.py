#!/usr/bin/env python3
"""
Shared progress tracking for the daily pipeline.

Provides a ProgressTracker class that writes step-level progress to a JSON file
with fcntl.flock locking for safe multi-process reads and writes.

Usage:
  tracker = ProgressTracker("/path/to/.tmp/daily_progress_260710.json")
  tracker.init(["pool", "quant", "tracker"])
  tracker.step_start("pool")
  tracker.step_update("quant", total=200, completed=50, current_code="000001")
  tracker.step_done("pool")
  tracker.mark_done()

  # Reading (class method, uses shared lock):
  data = ProgressTracker.read("/path/to/progress.json")
"""

import fcntl
import json
import os
import time
from datetime import datetime
from pathlib import Path


class ProgressTracker:
    """Thread/process-safe progress file writer with fcntl flock.

    All writes use LOCK_EX + atomic tempfile+rename.
    All reads use LOCK_SH + retry on partial writes.
    """

    def __init__(self, file_path):
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ── public API ──

    def init(self, steps):
        """Create a fresh progress file with the given step keys."""
        now = datetime.now().isoformat()
        step_map = {}
        for key in steps:
            step_map[key] = {
                "status": "waiting",
                "started_at": None,
                "finished_at": None,
                "elapsed_s": None,
                "error": None,
            }
        data = {
            "date": "",
            "status": "running",
            "started_at": now,
            "updated_at": now,
            "steps": step_map,
        }
        self._write_full(data)

    def set_date(self, date_yy):
        """Set the date field after init."""
        self._update_root("date", date_yy)

    def step_start(self, step):
        """Mark a step as running. Creates the step entry if it doesn't exist."""
        now = datetime.now().isoformat()
        data = self._read_modify_write()
        if data is None:
            return
        steps = data.setdefault("steps", {})
        if step not in steps:
            steps[step] = {
                "status": "waiting",
                "started_at": None,
                "finished_at": None,
                "elapsed_s": None,
                "error": None,
            }
        steps[step]["status"] = "running"
        steps[step]["started_at"] = now
        steps[step]["finished_at"] = None
        steps[step]["error"] = None
        data["updated_at"] = now
        self._write_full(data)

    def step_update(self, step, **fields):
        """Update dynamic fields within a step (e.g. total, completed, current_code).

        Only valid keys are written to the step dict; status/started_at/finished_at
        are preserved unless explicitly passed.
        """
        if not fields:
            return
        data = self._read_modify_write()
        if data is None:
            return
        steps = data.setdefault("steps", {})
        if step not in steps:
            steps[step] = {
                "status": "running",
                "started_at": datetime.now().isoformat(),
                "finished_at": None,
                "elapsed_s": None,
                "error": None,
            }
        for k, v in fields.items():
            steps[step][k] = v
        data["updated_at"] = datetime.now().isoformat()
        self._write_full(data)

    def step_done(self, step, error=None):
        """Mark a step as done (or error if error string provided)."""
        now = datetime.now().isoformat()
        data = self._read_modify_write()
        if data is None:
            return
        steps = data.setdefault("steps", {})
        if step not in steps:
            steps[step] = {
                "status": "waiting",
                "started_at": now,
                "finished_at": None,
                "elapsed_s": None,
                "error": None,
            }
        s = steps[step]
        s["status"] = "error" if error else "done"
        s["finished_at"] = now
        s["error"] = error
        if s.get("started_at"):
            try:
                started = datetime.fromisoformat(s["started_at"])
                s["elapsed_s"] = round((datetime.now() - started).total_seconds())
            except (ValueError, TypeError):
                pass
        data["updated_at"] = now
        self._write_full(data)

    def mark_done(self):
        """Mark the entire pipeline as done, computing total_elapsed_s."""
        data = self._read_modify_write()
        if data is None:
            return
        started_str = data.get("started_at")
        if started_str:
            try:
                started = datetime.fromisoformat(started_str)
                data["total_elapsed_s"] = round((datetime.now() - started).total_seconds())
            except (ValueError, TypeError):
                pass
        data["status"] = "done"
        data["updated_at"] = datetime.now().isoformat()
        self._write_full(data)

    # ── static reader ──

    @staticmethod
    def read(file_path):
        """Read progress file with shared lock and retry.

        Returns parsed dict, or None if file doesn't exist or is corrupt after retries.
        """
        path = Path(file_path)
        if not path.exists():
            return None
        for attempt in range(3):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                    try:
                        return json.load(f)
                    finally:
                        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            except (json.JSONDecodeError, FileNotFoundError, OSError):
                time.sleep(0.05)
        return None

    # ── internal helpers ──

    def _read_modify_write(self):
        """Read current state (with shared lock), return dict for modification.
        Caller must call _write_full() after modifying.
        Returns None if read failed after retries."""
        return ProgressTracker.read(self._path)

    def _write_full(self, data):
        """Atomically write full progress data with exclusive lock."""
        tmp = self._path.with_suffix(".tmp")
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                try:
                    json.dump(data, f, ensure_ascii=False, indent=2)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            tmp.replace(self._path)
        except OSError:
            pass  # best-effort, never crash the pipeline

    def _update_root(self, key, value):
        """Update a single root-level field."""
        data = self._read_modify_write()
        if data is None:
            return
        data[key] = value
        data["updated_at"] = datetime.now().isoformat()
        self._write_full(data)
