#!/usr/bin/env python3
"""
Daily pipeline: 数据更新 → Pool → Quant → Bloom → Signal Plan → 信号财务提示
→ 完整资金观测 → 页面发布（含信号股资金补查）→ 打开面板 → 东方财富自选同步。

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

def run_command_with_retries(command, *, attempts=2, label="步骤"):
    """仅用于可重入步骤的有限重试。

    Pool / Quant / Bloom / Signal Plan 会维护跨日状态，不得调用此函数盲目重试。
    """
    result = None
    for attempt in range(1, attempts + 1):
        result = subprocess.run(command, cwd=PROJECT_ROOT)
        if result.returncode == 0:
            return result
        if attempt < attempts:
            print(f"[daily] ⚠ {label}失败 (exit {result.returncode})，将重试 {attempts - attempt} 次")
    return result


def run_market_update(date_yy):
    iso = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    return run_command_with_retries(
        ["python3", "scripts/market_regime.py", "update", "--date", iso],
        label="市场数据更新",
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


def run_signal_fundamentals(date_yy):
    return subprocess.run(
        ["python3", "scripts/signal_fundamentals.py", "--date", date_yy],
        cwd=PROJECT_ROOT,
    )


def run_capital_observer(date_yy):
    iso = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    return run_command_with_retries(
        ["python3", "scripts/capital_observer.py", "run", "--date", iso, "--fetch"],
        label="资金观测",
    )


def load_capital_observer_meta(date_yy):
    path = Path(PROJECT_ROOT) / "capital" / f"capital_observer_{date_yy}.json"
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    meta = payload.get("meta") or {}
    expected_date = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    if meta.get("trade_date") != expected_date:
        raise ValueError(f"资金观测日期不一致: {meta.get('trade_date')} != {expected_date}")
    if meta.get("fetch_enabled") is not True:
        raise ValueError("当日完整资金观测未启用资金补取")
    return meta


def load_market_llm_meta(date_yy):
    """读取市场报告并确认当日 LLM 解读真正成功。"""
    path = Path(PROJECT_ROOT) / "market" / f"market_regime_{date_yy}.json"
    if not path.exists():
        raise FileNotFoundError(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    expected_date = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    meta = payload.get("meta") or {}
    if meta.get("run_date") != expected_date:
        raise ValueError(f"市场报告日期不一致: {meta.get('run_date')} != {expected_date}")
    llm = payload.get("llm") or {}
    if llm.get("status") != "success":
        reason = llm.get("reason") or "unknown"
        raise ValueError(f"市场 LLM 解读未成功: {llm.get('status')} ({reason})")
    if not str(llm.get("analysis") or "").strip():
        raise ValueError("市场 LLM 解读为空")
    return llm


def run_market_publish(date_yy):
    """发布市场面板；LLM 失败时保留主线结论并定向重试一次。"""
    iso = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    command = ["python3", "scripts/market_regime.py", "run", "--date", iso]
    result = subprocess.run(command, cwd=PROJECT_ROOT)
    if result.returncode == 0:
        try:
            load_market_llm_meta(date_yy)
            return result
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"[daily] ⚠ {exc}，将保留已生成主线并重试 LLM")
    else:
        print(f"[daily] ⚠ 市场面板失败 (exit {result.returncode})，将保留已有主线并重试")

    retry_command = command + ["--reuse-existing-mainline"]
    result = subprocess.run(retry_command, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        return result
    try:
        load_market_llm_meta(date_yy)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[daily] ✗ {exc}")
        return subprocess.CompletedProcess(retry_command, 4)
    return result


def run_dashboard_publish(date_yy):
    result = run_market_publish(date_yy)
    if result.returncode != 0:
        return result
    commands = [
        ["python3", "scripts/backtest.py", "--date", date_yy],
        ["python3", "scripts/dashboard_vcp.py", "--date", date_yy],
        ["python3", "scripts/dashboard_signals.py", "--date", date_yy, "--fetch-capital", "--max-mx-requests", "5"],
    ]
    for command in commands:
        result = run_command_with_retries(command, label=f"页面发布 {command[1]}")
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
        root / "capital" / f"capital_observer_{date_yy}.json",
        root / "market" / f"market_regime_{date_yy}.json",
        root / "market" / "data" / f"market_context_{date_yy}.json",
        month_dir / f"capital_context_{date_yy}.js",
        month_dir / f"market_context_{date_yy}.js",
        month_dir / f"vcp_context_{date_yy}.js",
        month_dir / f"signals_context_{date_yy}.js",
        month_dir / f"backtest_context_{date_yy}.js",
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
            for module in ("capital", "market", "vcp", "signals", "backtest"):
                if date_yy not in index.get(module, {}).get("available", []):
                    missing.append(f"dashboard index {module}:{date_yy}")

    capital_path = root / "capital" / f"capital_observer_{date_yy}.json"
    if capital_path.exists():
        try:
            meta = (json.loads(capital_path.read_text(encoding="utf-8")).get("meta") or {})
            expected_date = datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
            if meta.get("trade_date") != expected_date:
                missing.append(f"capital observer 日期:{meta.get('trade_date')}")
            if meta.get("fetch_enabled") is not True:
                missing.append("capital observer 未启用资金补取")
        except (OSError, json.JSONDecodeError):
            missing.append(f"capital/capital_observer_{date_yy}.json（格式无效）")

    market_report_path = root / "market" / f"market_regime_{date_yy}.json"
    if market_report_path.exists():
        try:
            load_market_llm_meta(date_yy)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            missing.append(str(exc))

    market_context_path = root / "market" / "data" / f"market_context_{date_yy}.json"
    if market_context_path.exists():
        try:
            context = json.loads(market_context_path.read_text(encoding="utf-8"))
            market_state = context.get("market_state") or {}
            if market_state.get("analysis_source") != "llm":
                missing.append(f"market context 解读来源:{market_state.get('analysis_source')}")
            if not str(market_state.get("analysis") or "").strip():
                missing.append("market context 市场解读为空")
        except (OSError, json.JSONDecodeError):
            missing.append(f"market/data/market_context_{date_yy}.json（格式无效）")

    signals_path = month_dir / f"signals_context_{date_yy}.js"
    if signals_path.exists():
        try:
            assignment = re.search(
                rf"\[{re.escape(json.dumps(date_yy))}\]\s*=\s*(\{{.*\}});\s*$",
                signals_path.read_text(encoding="utf-8"),
                re.S,
            )
            if not assignment:
                raise ValueError("assignment missing")
            signals = json.loads(assignment.group(1))
            if (signals.get("meta") or {}).get("capital_fetch_enabled") is not True:
                missing.append("signals context 未启用信号股资金补查")
            if any("capital_support" not in row for row in signals.get("signals", [])):
                missing.append("signals context 存在缺少 capital_support 的信号")
        except (OSError, ValueError, json.JSONDecodeError):
            missing.append(f"dashboard signals:{date_yy}（格式无效）")
    if missing:
        print("[daily] 页面/产物完整性核验失败：" + "；".join(missing))
        return False
    print(f"[daily] ✓ 完整性核验通过：{date_yy} 资金/市场/VCP/信号/回测日期数据均已发布")
    return True


def open_dashboard():
    """Open the local dashboard in the system default browser after publishing."""
    dashboard = Path(PROJECT_ROOT) / "dashboard" / "index.html"
    return subprocess.run(["open", str(dashboard)], cwd=PROJECT_ROOT)


def run_zixuan(date_yy):
    """Rebuild Eastmoney's all-watchlist after Bloom has completed."""
    return run_command_with_retries(
        ["python3", "scripts/sync_zixuan.py", "--date", date_yy, "--yes"],
        label="东方财富自选同步",
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
    tracker.init(["data_update", "pool", "quant", "bloom", "signal_plan", "signal_fundamentals", "capital_observer", "dashboard", "verify", "open_dashboard", "zixuan"])
    tracker.set_date(date_yy)

    print(f"[daily] 流水线启动 {date_yy}")
    print(f"[daily] 进度文件: {progress_path}")
    pool_path = Path(PROJECT_ROOT) / "pool" / f"pool_{date_yy}.csv"

    errors = []

    def stop_after(stage):
        tracker.mark_failed(errors[-1] if errors else f"{stage} 失败")
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

    # ── Step 6: Signal financial hints (non-blocking sidecar) ──
    tracker.step_start("signal_fundamentals")
    print("[daily] → 信号财务提示")
    result = run_signal_fundamentals(date_yy)
    if result.returncode != 0:
        tracker.step_done("signal_fundamentals", error=f"exit {result.returncode}")
        print("[daily] ⚠ 信号财务补查失败，页面将使用已有缓存或降级提示")
    else:
        tracker.step_done("signal_fundamentals")
        print("[daily] ✓ signal fundamentals done")

    # ── Step 7: Full daily capital observation ──
    tracker.step_start("capital_observer")
    print("[daily] → 当天完整资金观测")
    result = run_capital_observer(date_yy)
    if result.returncode != 0:
        errors.append(f"capital_observer: exit {result.returncode}")
        tracker.step_done("capital_observer", error=f"exit {result.returncode}")
        stop_after("资金观测")
    try:
        capital_meta = load_capital_observer_meta(date_yy)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        errors.append(f"capital_observer: {exc}")
        tracker.step_done("capital_observer", error=str(exc))
        stop_after("资金观测")
    tracker.step_update(
        "capital_observer",
        observer_status=capital_meta.get("status"),
        requests_used=capital_meta.get("requests_used"),
        request_budget=capital_meta.get("request_budget"),
        error_count=len(capital_meta.get("errors") or []),
    )
    tracker.step_done("capital_observer")
    if capital_meta.get("status") == "partial":
        print("[daily] ⚠ 资金观测部分完成，已保留当日产物与错误明细")
    else:
        print("[daily] ✓ capital observer done")

    # ── Step 8: Dashboard packages, including signal-stock capital fetch ──
    tracker.step_start("dashboard")
    print("[daily] → 生成数据分析面板（含信号股资金补查）")
    result = run_dashboard_publish(date_yy)
    if result.returncode != 0:
        errors.append(f"dashboard: exit {result.returncode}")
        tracker.step_done("dashboard", error=f"exit {result.returncode}")
        stop_after("页面发布")
    tracker.step_done("dashboard")
    print("[daily] ✓ dashboard done")

    # ── Step 9: Verify all date-scoped outputs ──
    tracker.step_start("verify")
    if not verify_pipeline_outputs(date_yy):
        errors.append("verify: missing date-scoped output")
        tracker.step_done("verify", error="missing date-scoped output")
        stop_after("完整性核验")
    tracker.step_done("verify")

    # ── Step 10: Open dashboard ──
    tracker.step_start("open_dashboard")
    print("[daily] → 打开数据分析面板")
    result = open_dashboard()
    if result.returncode != 0:
        tracker.step_done("open_dashboard", error=f"exit {result.returncode}")
        print("[daily] ⚠ 无法自动打开浏览器，页面数据已生成")
    else:
        tracker.step_done("open_dashboard")
        print("[daily] ✓ dashboard opened")

    # ── Step 11: Eastmoney all-watchlist rebuild ──
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

    if errors:
        tracker.mark_failed(errors[-1])
        print(f"[daily] 流水线中止，共 {len(errors)} 个错误")
        for e in errors:
            print(f"  ⚠️ {e}")
        sys.exit(1)
    tracker.mark_done()
    print("[daily] 流水线完成，共 0 个错误")


if __name__ == "__main__":
    main()
