#!/usr/bin/env python3
"""
Daily pipeline: 模型一 → 模型二 → 模型四 Tracker。

Launches each stage via subprocess, writes step-level progress to a shared
JSON file consumed by monitor.py. This script is non-interactive and designed
to run in the background.

Usage:
  python3 scripts/daily.py                        # full pipeline
  python3 scripts/daily.py --date 260709          # specific date
  python3 scripts/daily.py --skip-pool            # reuse existing pool
  python3 scripts/daily.py --force-refresh        # force refresh cached data

Background + monitor:
  python3 scripts/daily.py &                      # start in background
  python3 scripts/monitor.py                      # watch progress (foreground)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT, default_pipeline_date
from scripts.progress_utils import ProgressTracker

PROGRESS_DIR = Path(PROJECT_ROOT) / ".tmp"


def normalize_date_arg(value):
    if not value:
        return None
    raw = value.strip()
    if re.fullmatch(r"\d{6}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%y%m%d")
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def _load_dotenv():
    env_path = Path(PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def _progress_path(date_yy):
    return PROGRESS_DIR / f"daily_progress_{date_yy}.json"


# ── stage runners ──

def run_pool(date_yy, force_refresh=False):
    from datetime import datetime as _dt
    iso = _dt.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    cmd = ["python3", "scripts/run_pool.py", "--date", iso]
    if force_refresh:
        cmd.append("--force-refresh")
    return subprocess.run(cmd, cwd=PROJECT_ROOT)


def run_quant(date_yy, pool_path, progress_path):
    cmd = [
        "python3", "scripts/quant_filter.py",
        "--date", date_yy,
        "--pool", str(pool_path),
        "--progress-file", str(progress_path),
    ]
    return subprocess.run(cmd, cwd=PROJECT_ROOT)


def run_tracker(date_yy, progress_path):
    """Run model 4 tracker (bloom → plan → assemble) as a subprocess."""
    return subprocess.run(
        ["python3", "scripts/tracker.py", "--date", date_yy,
         "--progress-file", str(progress_path)],
        cwd=PROJECT_ROOT,
    )


# ── main ──

def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Huaxin Quant daily pipeline")
    parser.add_argument("--date", help="运行日期 YYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--skip-pool", action="store_true", help="跳过模型一，复用已有池子")
    parser.add_argument("--force-refresh", action="store_true", help="强制刷新数据缓存")
    args = parser.parse_args()

    try:
        date_yy = normalize_date_arg(args.date)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    # Resolve date: use 15:00-aware default, fall back to latest pool if needed
    if not date_yy:
        date_yy = default_pipeline_date()
        pool_dir = Path(PROJECT_ROOT) / "pool"
        pool_path_check = pool_dir / f"pool_{date_yy}.csv"
        if not pool_path_check.exists():
            # 当日 pool 尚未生成（例如凌晨跑前一天数据），回退到最新 pool
            pool_files = sorted(pool_dir.glob("pool_*.csv"))
            if pool_files:
                match = re.fullmatch(r"pool_(\d{6})\.csv", pool_files[-1].name)
                if match:
                    date_yy = match.group(1)
                    print(f"[daily] 默认日期 {default_pipeline_date()} 无 pool，回退到最新: {date_yy}")
            else:
                print(f"错误: 未找到模型一 pool 文件，请先运行模型一或指定 --date")
                sys.exit(1)

    progress_path = _progress_path(date_yy)
    tracker = ProgressTracker(progress_path)
    tracker.init(["pool", "quant", "tracker"])
    tracker.set_date(date_yy)

    print(f"[daily] 流水线启动 {date_yy}")
    print(f"[daily] 进度文件: {progress_path}")
    pool_path = Path(PROJECT_ROOT) / "pool" / f"pool_{date_yy}.csv"

    errors = []

    # ── Step 1: Pool ──
    if not args.skip_pool:
        tracker.step_start("pool")
        print(f"[daily] → 模型一 Pool")
        result = run_pool(date_yy, force_refresh=args.force_refresh)
        if result.returncode != 0:
            errors.append(f"pool: exit {result.returncode}")
            tracker.step_done("pool", error=f"exit {result.returncode}")
        else:
            tracker.step_done("pool")
            print(f"[daily] ✓ pool done")
    else:
        tracker.step_done("pool")  # mark as done since we're skipping

    # ── Step 2: Quant ──
    tracker.step_start("quant")
    print(f"[daily] → 模型二 Quant ({pool_path})")
    result = run_quant(date_yy, pool_path, progress_path)
    if result.returncode != 0:
        errors.append(f"quant: exit {result.returncode}")
        tracker.step_done("quant", error=f"exit {result.returncode}")
    else:
        tracker.step_done("quant")
        print(f"[daily] ✓ quant done")

    # Stop early if quant failed (no data for downstream)
    if any("quant" in e for e in errors):
        tracker.mark_done()
        print(f"[daily] quant 失败，流水线中止。共 {len(errors)} 个错误")
        sys.exit(1)

    # ── Step 3: Tracker (Bloom → Plan → Assemble) ──
    tracker.step_start("tracker")
    print(f"[daily] → 模型四 Tracker")
    result = run_tracker(date_yy, progress_path)
    if result.returncode not in (0, 3):  # 3 = Bloom LLM failed (non-fatal)
        errors.append(f"tracker: exit {result.returncode}")
        tracker.step_done("tracker", error=f"exit {result.returncode}")
    else:
        tracker.step_done("tracker")
        print(f"[daily] ✓ tracker done")

    tracker.mark_done()
    print(f"[daily] 流水线完成，共 {len(errors)} 个错误")
    if errors:
        for e in errors:
            print(f"  ⚠️ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
