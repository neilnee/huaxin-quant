#!/usr/bin/env python3
"""Create local runtime directories for Huaxin Quant.

The public repository intentionally excludes generated data, local secrets,
and user-maintained notes. Run this once after cloning if you want the local
workspace layout prepared before executing the pipeline.
"""

import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)

RUNTIME_DIRS = [
    "cache/xuangu",
    "cache/daily",
    "cache/quant_runs",
    "cache/financial",
    "cache/briefing",
    "cache/calc_params",
    "cache/calc_results",
    "cache/research",
    "cache/zixuan",
    "pool",
    "quant",
    "bloom",
    "bloom/state",
    "reports/valuation",
    "reports/indexes",
    "reports/archive/valuation",
    "signals",
    "refer",
    "tmp/scripts",
]

CSV_TEMPLATES = {
    "signals/positions.csv": [
        "code",
        "name",
        "asset_type",
        "target_type",
        "target_value",
        "actual_shares",
        "cost_price",
        "stop_loss_override",
        "manual_tag",
    ],
    "signals/batches.csv": [
        "batch_id",
        "code",
        "name",
        "entry_date",
        "entry_logic",
        "entry_ma",
        "shares",
        "cost_price",
        "stop_loss_override",
        "status",
    ],
}


def ensure_csv(path: Path, fieldnames: list[str]) -> bool:
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
    return True


def main() -> int:
    created_dirs = []
    for rel in RUNTIME_DIRS:
        path = ROOT / rel
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(rel)

    created_files = []
    for rel, fields in CSV_TEMPLATES.items():
        if ensure_csv(ROOT / rel, fields):
            created_files.append(rel)

    print(f"Runtime root: {ROOT}")
    print(f"Directories created: {len(created_dirs)}")
    for rel in created_dirs:
        print(f"  + {rel}")
    print(f"Template files created: {len(created_files)}")
    for rel in created_files:
        print(f"  + {rel}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
