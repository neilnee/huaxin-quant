#!/usr/bin/env python3
"""Run deterministic repository quality checks without network access."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(os.path.abspath(__file__)).parents[1]
SOURCE_REPO = ROOT / "scripts"
if SOURCE_REPO.is_symlink():
    SOURCE_REPO = SOURCE_REPO.resolve().parent
else:
    SOURCE_REPO = ROOT


def run(label: str, command: list[str], *, cwd: Path = ROOT, env: dict | None = None) -> None:
    print(f"[check] {label}")
    completed = subprocess.run(command, cwd=cwd, env=env)
    if completed.returncode:
        raise SystemExit(completed.returncode)


def python_files() -> list[str]:
    return [str(path.relative_to(ROOT)) for path in sorted((ROOT / "scripts").glob("*.py"))] + [
        str(path.relative_to(ROOT)) for path in sorted((ROOT / "scripts" / "data").glob("*.py"))
    ]


def main() -> int:
    env = os.environ.copy()
    env.setdefault("PYTHONPYCACHEPREFIX", "/tmp/huaxin_check_pycache")
    run("Python syntax", [sys.executable, "-m", "py_compile", *python_files()], env=env)
    run("unit tests", [sys.executable, "-m", "unittest", "discover", "-s", "scripts", "-p", "test_*.py"], env=env)

    print("[check] strategy JSON")
    for path in sorted((ROOT / "strategies").glob("*.json")):
        with path.open(encoding="utf-8") as handle:
            payload = json.load(handle)
        if not payload.get("strategy_version"):
            raise SystemExit(f"strategy_version missing: {path}")

    node = shutil.which("node")
    if node:
        for path in sorted((ROOT / "dashboard").glob("*.js")):
            run(f"JavaScript syntax: {path.name}", [node, "--check", str(path)])
    else:
        print("[check] JavaScript syntax skipped: node not installed")

    run("Git whitespace", ["git", "-C", str(SOURCE_REPO), "diff", "--check"])
    print("[check] all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
