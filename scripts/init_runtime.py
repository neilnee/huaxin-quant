#!/usr/bin/env python3
"""Create local runtime directories for Huaxin Quant.

The public repository intentionally excludes generated data, local secrets,
and user-maintained notes. Run this once after cloning if you want the local
workspace layout prepared before executing the pipeline.
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT


DEFAULT_ROOT = Path(PROJECT_ROOT)
SOURCE_ROOT = Path(__file__).resolve().parents[1]

RUNTIME_DIRS = [
    "cache/xuangu",
    "cache/quant_runs",
    "cache/financial",
    "cache/briefing",
    "cache/calc_params",
    "cache/calc_results",
    "cache/research",
    "cache/zixuan",
    "cache/valuation_runs",
    "cache/market_data",
    "cache/capital_flow",
    "cache/strategy",
    "pool",
    "quant",
    "bloom",
    "bloom/state",
    "signal_plan",
    "tracker",
    "market/data",
    "capital",
    "backtest",
    "reports/valuation",
    "reports/indexes",
    "reports/archive/valuation",
    "position/trades",
    "position/states",
    "position/imports",
    "position/performance",
    "refer",
    "dashboard/data",
    ".tmp/scripts",
    ".tmp/locks",
]

POSITION_CONFIG = json.loads((SOURCE_ROOT / "strategies" / "04-position.json").read_text(encoding="utf-8"))
CSV_TEMPLATES = {
    "position/position_plan.csv": POSITION_CONFIG["schemas"]["position_plan"],
    "position/lots_current.csv": POSITION_CONFIG["schemas"]["lots_current"],
}


def ensure_csv(path: Path, fieldnames: list[str]) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Initialize or validate a Huaxin Quant runtime workspace")
    parser.add_argument("--root", default=str(DEFAULT_ROOT), help="runtime workspace root; defaults to the local symlink workspace")
    parser.add_argument("--check", action="store_true", help="validate only; do not create files or directories")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = Path(args.root).expanduser().absolute()
    if args.check:
        missing = [rel for rel in RUNTIME_DIRS if not (root / rel).is_dir()]
        missing.extend(rel for rel in CSV_TEMPLATES if not (root / rel).is_file())
        print(f"Runtime root: {root}")
        if missing:
            print("Missing runtime paths:")
            for rel in missing:
                print(f"  - {rel}")
            return 1
        print("Runtime workspace is ready")
        return 0

    created_dirs = []
    for rel in RUNTIME_DIRS:
        path = root / rel
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(rel)

    created_files = []
    for rel, fields in CSV_TEMPLATES.items():
        if ensure_csv(root / rel, fields):
            created_files.append(rel)

    print(f"Runtime root: {root}")
    print(f"Directories created: {len(created_dirs)}")
    for rel in created_dirs:
        print(f"  + {rel}")
    print(f"Template files created: {len(created_files)}")
    for rel in created_files:
        print(f"  + {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
