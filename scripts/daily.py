#!/usr/bin/env python3
"""
Daily pipeline: 模型一 → 模型二 → 模型四（Bloom → Signal Plan → 花期策览）。

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
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config


TRACKER_CONFIG, _ = load_strategy_config("04-tracker.json")
TRACKER_DIR = Path(PROJECT_ROOT) / TRACKER_CONFIG["outputs"]["report_dir"]

# Where progress and final report live
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


# ── progress helpers ──

def _progress_file(date_yy):
    PROGRESS_DIR.mkdir(parents=True, exist_ok=True)
    return PROGRESS_DIR / f"daily_progress_{date_yy}.json"


def _atomic_write(path, data):
    """Write JSON with a temp file + rename to avoid reader/writer races."""
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def _read_progress(path):
    for _ in range(3):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, FileNotFoundError):
            time.sleep(0.05)
    return None


def _init_progress(date_yy):
    now = datetime.now().isoformat()
    steps = {}
    for key in ("pool", "quant", "bloom", "plan", "assemble"):
        steps[key] = {
            "status": "waiting",
            "started_at": None,
            "finished_at": None,
            "elapsed_s": None,
            "error": None,
        }
    data = {
        "date": date_yy,
        "status": "running",
        "started_at": now,
        "updated_at": now,
        "steps": steps,
    }
    path = _progress_file(date_yy)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return path


def _step_start(date_yy, step):
    path = _progress_file(date_yy)
    if not path.exists():
        return
    data = _read_progress(path)
    if data is None:
        return
    data["steps"][step]["status"] = "running"
    data["steps"][step]["started_at"] = datetime.now().isoformat()
    data["updated_at"] = datetime.now().isoformat()
    _atomic_write(path, data)


def _step_done(date_yy, step, error=None):
    path = _progress_file(date_yy)
    if not path.exists():
        return
    data = _read_progress(path)
    if data is None:
        return
    s = data["steps"][step]
    s["status"] = "error" if error else "done"
    s["finished_at"] = datetime.now().isoformat()
    s["error"] = error
    if s["started_at"]:
        started = datetime.fromisoformat(s["started_at"])
        s["elapsed_s"] = round((datetime.now() - started).total_seconds())
    data["updated_at"] = datetime.now().isoformat()
    _atomic_write(path, data)


def _mark_done(date_yy):
    path = _progress_file(date_yy)
    if not path.exists():
        return
    data = _read_progress(path)
    if data is None:
        return
    started = datetime.fromisoformat(data["started_at"])
    data["total_elapsed_s"] = round((datetime.now() - started).total_seconds())
    data["status"] = "done"
    data["updated_at"] = datetime.now().isoformat()
    _atomic_write(path, data)


# ── stage runners ──

def run_pool(date_yy, force_refresh=False):
    cmd = ["python3", "scripts/run_pool.py", "--date", date_yy]
    if force_refresh:
        cmd.append("--force-refresh")
    return subprocess.run(cmd, cwd=PROJECT_ROOT)


def run_quant(date_yy, pool_path, progress_file):
    cmd = [
        "python3", "scripts/quant_filter.py",
        "--date", date_yy,
        "--pool", str(pool_path),
        "--progress-file", str(progress_file),
    ]
    return subprocess.run(cmd, cwd=PROJECT_ROOT)


def run_bloom(date_yy):
    return subprocess.run(
        ["python3", "scripts/bloom.py", "--date", date_yy],
        cwd=PROJECT_ROOT,
    )


def run_plan(date_yy):
    return subprocess.run(
        ["python3", "scripts/signal_plan.py", "--date", date_yy],
        cwd=PROJECT_ROOT,
    )


def run_assemble(date_yy):
    """Read bloom + plan outputs from disk, assemble consolidated report.
    Steps 3 and 4 already ran bloom.py and signal_plan.py (with LLM calls).
    This step only reads their outputs — no duplicate LLM calls."""
    from pathlib import Path as _Path
    from scripts.tracker import build_consolidated_markdown

    bloom_input_path = _Path(PROJECT_ROOT) / "bloom" / "state" / f"bloom_input_{date_yy}.json"
    plan_json_path = _Path(PROJECT_ROOT) / "signal_plan" / f"signal_plan_{date_yy}.json"
    bloom_md_path = _Path(PROJECT_ROOT) / "bloom" / f"bloom_{date_yy}.md"
    plan_md_path = _Path(PROJECT_ROOT) / "signal_plan" / f"signal_plan_{date_yy}.md"

    if not bloom_input_path.exists():
        raise FileNotFoundError(f"missing bloom input: {bloom_input_path}")
    if not plan_json_path.exists():
        raise FileNotFoundError(f"missing plan json: {plan_json_path}")

    with open(bloom_input_path, "r", encoding="utf-8") as f:
        bloom_data = json.load(f)
    with open(plan_json_path, "r", encoding="utf-8") as f:
        plan_data = json.load(f)

    bloom_md = bloom_md_path.read_text(encoding="utf-8") if bloom_md_path.exists() else ""
    plan_md = plan_md_path.read_text(encoding="utf-8") if plan_md_path.exists() else ""

    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    consolidated = build_consolidated_markdown(bloom_data, plan_data, date_yy, bloom_md, plan_md)
    tracker_path = TRACKER_DIR / f"花期策览_{date_yy}.md"
    with open(tracker_path, "w", encoding="utf-8") as f:
        f.write(consolidated)
    return tracker_path


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

    # Resolve date from latest pool if not specified
    if not date_yy:
        from scripts.run_pool import POOL_DIR
        pool_files = sorted(POOL_DIR.glob("pool_*.csv"))
        if not pool_files:
            print("错误: 未找到模型一 pool 文件，请先运行模型一或指定 --date")
            sys.exit(1)
        match = re.fullmatch(r"pool_(\d{6})\.csv", pool_files[-1].name)
        if not match:
            print(f"错误: 无法从池文件名解析日期: {pool_files[-1]}")
            sys.exit(1)
        date_yy = match.group(1)

    progress_path = _init_progress(date_yy)
    print(f"[daily] 流水线启动 {date_yy}")
    print(f"[daily] 进度文件: {progress_path}")
    pool_path = Path(PROJECT_ROOT) / "pool" / f"pool_{date_yy}.csv"

    errors = []

    # ── Step 1: Pool ──
    if not args.skip_pool:
        _step_start(date_yy, "pool")
        print(f"[daily] → 模型一 Pool")
        result = run_pool(date_yy, force_refresh=args.force_refresh)
        if result.returncode != 0:
            errors.append(f"pool: exit {result.returncode}")
            _step_done(date_yy, "pool", error=f"exit {result.returncode}")
        else:
            _step_done(date_yy, "pool")
            print(f"[daily] ✓ pool done")
    else:
        _step_done(date_yy, "pool")  # mark as done since we're skipping

    # ── Step 2: Quant ──
    _step_start(date_yy, "quant")
    print(f"[daily] → 模型二 Quant ({pool_path})")
    result = run_quant(date_yy, pool_path, progress_path)
    if result.returncode != 0:
        errors.append(f"quant: exit {result.returncode}")
        _step_done(date_yy, "quant", error=f"exit {result.returncode}")
    else:
        _step_done(date_yy, "quant")
        print(f"[daily] ✓ quant done")

    # Stop early if quant failed (no data for downstream)
    if any("quant" in e for e in errors):
        _mark_done(date_yy)
        print(f"[daily] quant 失败，流水线中止。共 {len(errors)} 个错误")
        sys.exit(1)

    # ── Step 3: Bloom ──
    _step_start(date_yy, "bloom")
    print(f"[daily] → Bloom")
    result = run_bloom(date_yy)
    if result.returncode not in (0, 3):  # 3 = LLM failed (non-fatal)
        errors.append(f"bloom: exit {result.returncode}")
        _step_done(date_yy, "bloom", error=f"exit {result.returncode}")
    else:
        _step_done(date_yy, "bloom")
        print(f"[daily] ✓ bloom done (exit {result.returncode})")

    # ── Step 4: Signal Plan ──
    _step_start(date_yy, "plan")
    print(f"[daily] → Signal Plan")
    result = run_plan(date_yy)
    if result.returncode != 0:
        errors.append(f"plan: exit {result.returncode}")
        _step_done(date_yy, "plan", error=f"exit {result.returncode}")
    else:
        _step_done(date_yy, "plan")
        print(f"[daily] ✓ plan done")

    # ── Step 5: Assemble ──
    _step_start(date_yy, "assemble")
    print(f"[daily] → 合并报告")
    try:
        tracker_path = run_assemble(date_yy)
        _step_done(date_yy, "assemble")
        print(f"[daily] ✓ 花期策览: {tracker_path}")
    except Exception as exc:
        errors.append(f"assemble: {exc}")
        _step_done(date_yy, "assemble", error=str(exc))

    _mark_done(date_yy)
    print(f"[daily] 流水线完成，共 {len(errors)} 个错误")
    if errors:
        for e in errors:
            print(f"  ⚠️ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
