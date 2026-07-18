#!/usr/bin/env python3
"""
Daily pipeline: 数据更新 → Pool → Quant → Bloom → Signal Plan → 页面发布 → 打开面板 → 东方财富自选同步。

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


def _env_flag(name):
    """Return True only for explicit, conventional true values."""
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _progress_path(date_yy):
    return PROGRESS_DIR / f"daily_progress_{date_yy}.json"


# ── stage runners ──

def run_market_update(date_yy):
    iso = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    return subprocess.run(
        ["python3", "scripts/market_regime.py", "update", "--date", iso],
        cwd=PROJECT_ROOT,
    )


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


def run_bloom(date_yy, progress_path):
    return subprocess.run(
        ["python3", "scripts/bloom.py", "--date", date_yy,
         "--progress-file", str(progress_path), "--skip-dashboard-publish"],
        cwd=PROJECT_ROOT,
    )


def run_signal_plan(date_yy, progress_path):
    return subprocess.run(
        ["python3", "scripts/signal_plan.py", "--date", date_yy,
         "--progress-file", str(progress_path)],
        cwd=PROJECT_ROOT,
    )


def run_dashboard_publish(date_yy):
    iso = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    commands = [
        ["python3", "scripts/market_regime.py", "run", "--date", iso],
        ["python3", "scripts/dashboard_vcp.py", "--date", date_yy],
        ["python3", "scripts/dashboard_signals.py", "--date", date_yy],
    ]
    for command in commands:
        result = subprocess.run(command, cwd=PROJECT_ROOT)
        if result.returncode != 0:
            return result
    return result


def verify_pipeline_outputs(date_yy):
    """Confirm every downstream module published the requested date."""
    root = Path(PROJECT_ROOT)
    month_dir = root / "dashboard" / "data" / f"20{date_yy[:4]}"
    required = [
        root / "pool" / f"pool_{date_yy}.csv",
        root / "cache" / "quant_runs" / f"quant_{date_yy}.json",
        root / "bloom" / "state" / f"bloom_input_{date_yy}.json",
        root / "signal_plan" / f"signal_plan_{date_yy}.json",
        root / "market" / "data" / f"market_context_{date_yy}.json",
        month_dir / f"market_context_{date_yy}.js",
        month_dir / f"vcp_context_{date_yy}.js",
        month_dir / f"signals_context_{date_yy}.js",
    ]
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    index_path = root / "dashboard" / "data" / "index.js"
    if not index_path.exists():
        missing.append("dashboard/data/index.js")
    else:
        match = re.search(r"=\s*(\{.*\});\s*$", index_path.read_text(encoding="utf-8"), re.S)
        if not match:
            missing.append("dashboard/data/index.js（格式无效）")
        else:
            index = json.loads(match.group(1))
            for module in ("market", "vcp", "signals"):
                if date_yy not in index.get(module, {}).get("available", []):
                    missing.append(f"dashboard index {module}:{date_yy}")
    if missing:
        print("[daily] 页面/产物完整性核验失败：" + "；".join(missing))
        return False
    print(f"[daily] ✓ 完整性核验通过：{date_yy} 三类页面数据均已发布")
    return True


def open_dashboard():
    """Open the local dashboard in the system default browser after publishing."""
    dashboard = Path(PROJECT_ROOT) / "dashboard" / "index.html"
    return subprocess.run(["open", str(dashboard)], cwd=PROJECT_ROOT)


def run_zixuan(date_yy):
    """Rebuild Eastmoney's all-watchlist after Bloom has completed."""
    return subprocess.run(
        ["python3", "scripts/sync_zixuan.py", "--date", date_yy, "--yes"],
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

    # Resolve date: full runs generate the default-date pool; skip-pool reuses one.
    if not date_yy:
        date_yy = default_pipeline_date()
        if args.skip_pool:
            pool_dir = Path(PROJECT_ROOT) / "pool"
            pool_path_check = pool_dir / f"pool_{date_yy}.csv"
            if not pool_path_check.exists():
                # 复用已有池子时，如果默认日期未生成，则回退到最近 pool。
                pool_files = sorted(pool_dir.glob("pool_*.csv"))
                if pool_files:
                    match = re.fullmatch(r"pool_(\d{6})\.csv", pool_files[-1].name)
                    if match:
                        date_yy = match.group(1)
                        print(f"[daily] 默认日期 {default_pipeline_date()} 无 pool，回退到最新: {date_yy}")
                else:
                    print(f"错误: 未找到模型一 pool 文件，请先运行模型一或指定 --date")
                    sys.exit(1)
    elif args.skip_pool:
        pool_dir = Path(PROJECT_ROOT) / "pool"
        pool_path_check = pool_dir / f"pool_{date_yy}.csv"
        if not pool_path_check.exists():
            print(f"错误: --skip-pool 需要已有 pool 文件: {pool_path_check}")
            sys.exit(1)

    progress_path = _progress_path(date_yy)
    tracker = ProgressTracker(progress_path)
    tracker.init(["data_update", "pool", "quant", "bloom", "signal_plan", "dashboard", "verify", "open_dashboard", "zixuan"])
    tracker.set_date(date_yy)

    print(f"[daily] 流水线启动 {date_yy}")
    print(f"[daily] 进度文件: {progress_path}")
    pool_path = Path(PROJECT_ROOT) / "pool" / f"pool_{date_yy}.csv"

    errors = []

    def stop_after(stage):
        tracker.mark_done()
        print(f"[daily] {stage} 失败，流水线中止。共 {len(errors)} 个错误")
        sys.exit(1)

    # ── Step 1: Shared market data update ──
    tracker.step_start("data_update")
    print("[daily] → 市场数据增量更新")
    result = run_market_update(date_yy)
    if result.returncode != 0:
        errors.append(f"data_update: exit {result.returncode}")
        tracker.step_done("data_update", error=f"exit {result.returncode}")
        stop_after("数据更新")
    tracker.step_done("data_update")
    print("[daily] ✓ data update done")

    # ── Step 2: Pool ──
    if not args.skip_pool:
        tracker.step_start("pool")
        print(f"[daily] → 模型一 Pool")
        result = run_pool(date_yy, force_refresh=args.force_refresh)
        if result.returncode != 0:
            errors.append(f"pool: exit {result.returncode}")
            tracker.step_done("pool", error=f"exit {result.returncode}")
            stop_after("Pool")
        else:
            tracker.step_done("pool")
            print(f"[daily] ✓ pool done")
    else:
        tracker.step_done("pool")  # mark as done since we're skipping

    # ── Step 3: Quant ──
    tracker.step_start("quant")
    print(f"[daily] → 模型二 Quant ({pool_path})")
    result = run_quant(date_yy, pool_path, progress_path)
    if result.returncode != 0:
        errors.append(f"quant: exit {result.returncode}")
        tracker.step_done("quant", error=f"exit {result.returncode}")
    else:
        tracker.step_done("quant")
        print(f"[daily] ✓ quant done")

    if any("quant" in e for e in errors):
        stop_after("Quant")

    # ── Step 4: Bloom ──
    tracker.step_start("bloom")
    print("[daily] → Bloom 信号")
    result = run_bloom(date_yy, progress_path)
    if result.returncode not in (0, 3):  # 3 = LLM failed after deterministic outputs were written
        errors.append(f"bloom: exit {result.returncode}")
        tracker.step_done("bloom", error=f"exit {result.returncode}")
        stop_after("Bloom")
    tracker.step_done("bloom")
    if result.returncode == 3:
        print("[daily] ⚠ Bloom LLM 解读失败，已使用规则产物继续")
    else:
        print("[daily] ✓ bloom done")

    # ── Step 5: Signal Plan ──
    tracker.step_start("signal_plan")
    print("[daily] → Signal Plan")
    result = run_signal_plan(date_yy, progress_path)
    if result.returncode != 0:
        errors.append(f"signal_plan: exit {result.returncode}")
        tracker.step_done("signal_plan", error=f"exit {result.returncode}")
        stop_after("Signal Plan")
    tracker.step_done("signal_plan")
    print("[daily] ✓ signal plan done")

    # ── Step 6: Dashboard packages ──
    tracker.step_start("dashboard")
    print("[daily] → 生成数据分析面板")
    result = run_dashboard_publish(date_yy)
    if result.returncode != 0:
        errors.append(f"dashboard: exit {result.returncode}")
        tracker.step_done("dashboard", error=f"exit {result.returncode}")
        stop_after("页面发布")
    tracker.step_done("dashboard")
    print("[daily] ✓ dashboard done")

    # ── Step 7: Verify all date-scoped outputs ──
    tracker.step_start("verify")
    if not verify_pipeline_outputs(date_yy):
        errors.append("verify: missing date-scoped output")
        tracker.step_done("verify", error="missing date-scoped output")
        stop_after("完整性核验")
    tracker.step_done("verify")

    # ── Step 8: Open dashboard ──
    tracker.step_start("open_dashboard")
    print("[daily] → 打开数据分析面板")
    result = open_dashboard()
    if result.returncode != 0:
        tracker.step_done("open_dashboard", error=f"exit {result.returncode}")
        print("[daily] ⚠ 无法自动打开浏览器，页面数据已生成")
    else:
        tracker.step_done("open_dashboard")
        print("[daily] ✓ dashboard opened")

    # ── Step 9: Eastmoney all-watchlist rebuild ──
    if not _env_flag("ENABLE_ZIXUAN_SYNC"):
        tracker.step_done("zixuan")
        print("[daily] - zixuan disabled (set ENABLE_ZIXUAN_SYNC=true in .env to enable)")
    else:
        tracker.step_start("zixuan")
        print(f"[daily] → 东方财富自选重建")
        result = run_zixuan(date_yy)
        if result.returncode != 0:
            errors.append(f"zixuan: exit {result.returncode}")
            tracker.step_done("zixuan", error=f"exit {result.returncode}")
        else:
            tracker.step_done("zixuan")
            print(f"[daily] ✓ zixuan done")

    tracker.mark_done()
    print(f"[daily] 流水线完成，共 {len(errors)} 个错误")
    if errors:
        for e in errors:
            print(f"  ⚠️ {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
