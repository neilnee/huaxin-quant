"""Concurrency-safe publisher for the shared dashboard module index."""

from __future__ import annotations

import json
import re
from pathlib import Path

from scripts.io_utils import FileLock, atomic_write_text


def load_dashboard_index(data_dir: Path) -> dict:
    path = data_dir / "index.js"
    if not path.exists():
        return {}
    match = re.search(r"=\s*(\{.*\});\s*$", path.read_text(encoding="utf-8"), re.S)
    return json.loads(match.group(1)) if match else {}


def update_dashboard_module(
    data_dir: Path,
    kind: str,
    dates: list[str],
    *,
    extra: dict | None = None,
    defaults: tuple[str, ...] = (),
) -> dict:
    data_dir.mkdir(parents=True, exist_ok=True)
    with FileLock(data_dir / ".index.lock", purpose=f"dashboard-index:{kind}"):
        index = load_dashboard_index(data_dir)
        entry = {"latest": dates[-1] if dates else None, "available": dates}
        if extra:
            entry.update(extra)
        index[kind] = entry
        for name in defaults:
            index.setdefault(name, {"latest": None, "available": []})
        atomic_write_text(
            data_dir / "index.js",
            "window.QUANT_DASHBOARD_INDEX = " + json.dumps(index, ensure_ascii=False) + ";\n",
        )
        return index
