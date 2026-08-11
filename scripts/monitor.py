#!/usr/bin/env python3
"""
Progress monitor for the daily pipeline. Polls the progress JSON written by
daily.py and renders a live progress page under .tmp/.

Usage:
  python3 scripts/monitor.py                    # watch current run
  python3 scripts/monitor.py --date 260709      # watch specific date
  python3 scripts/monitor.py --interval 1       # poll every 1s (default 2s)
"""

import argparse
import json
import os
import re
import sys
import time
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


def _resolve_date(date_yy):
    """解析监控日期，统一使用 15:00 收盘分隔线。

    优先精确匹配给定日期；未指定时按 default_pipeline_date() 查找对应
    progress 文件，找不到再回退到最新的 progress 文件。
    """
    if date_yy:
        return date_yy
    # 优先匹配 15:00-aware 默认日期
    default_yy = default_pipeline_date()
    default_path = _progress_file(default_yy)
    if default_path.exists():
        return default_yy
    # 回退：查找最近的 progress 文件（不早于默认日期，避免拿到未来日期）
    files = sorted(PROGRESS_DIR.glob("daily_progress_*.json"))
    for f in reversed(files):
        match = re.fullmatch(r"daily_progress_(\d{6})\.json", f.name)
        if match:
            candidate = match.group(1)
            if candidate <= default_yy:
                return candidate
    # 最后兜底：取最新 progress（可能比默认日期新，但至少有东西可监控）
    if files:
        match = re.fullmatch(r"daily_progress_(\d{6})\.json", files[-1].name)
        if match:
            return match.group(1)
    return None


def _progress_file(date_yy):
    return PROGRESS_DIR / f"daily_progress_{date_yy}.json"


def _progress_markdown_path(date_yy):
    return PROGRESS_DIR / f"daily_progress_{date_yy}.md"


def _read_progress(date_yy):
    """Read progress with shared lock via ProgressTracker."""
    path = _progress_file(date_yy)
    return ProgressTracker.read(path)


def _status_icon(status):
    icons = {
        "waiting": "⏳",
        "running": "🔄",
        "done": "✅",
        "error": "❌",
    }
    return icons.get(status, "❓")


def _step_time(step):
    """Return elapsed time string. For done/error steps, use stored elapsed_s
    (captured at completion time). For running steps, compute live from started_at."""
    if step.get("status") in ("done", "error") and step.get("elapsed_s") is not None:
        s = step["elapsed_s"]
        if s < 60:
            return f"{s}s"
        m, sec = divmod(s, 60)
        return f"{m}m{sec}s"
    started = step.get("started_at")
    if not started:
        return ""
    elapsed = round((datetime.now() - datetime.fromisoformat(started)).total_seconds())
    if elapsed < 60:
        return f"{elapsed}s"
    m, s = divmod(elapsed, 60)
    return f"{m}m{s}s"


def _step_label(step):
    labels = {
        "data_update": "市场数据更新",
        "pool": "模型一 Pool",
        "quant": "模型二 Quant",
        "bloom": "Bloom 信号",
        "signal_plan": "Signal Plan",
        "signal_fundamentals": "信号财务提示",
        "capital_observer": "当天完整资金观测",
        "dashboard": "数据分析面板",
        "verify": "产物完整性核验",
        "open_dashboard": "打开数据分析面板",
        "zixuan": "东方财富自选同步",
    }
    return labels.get(step, step)


def _has_detail(step_key):
    """Steps that can show per-item progress details."""
    return step_key in ("quant", "bloom", "signal_plan")


def _build_progress_markdown(progress):
    date_yy = progress.get("date", "")
    steps = progress.get("steps", {})
    started_at = progress.get("started_at", "")
    root_status = progress.get("status", "running")
    headline = "❌ 已中止" if root_status == "failed" else "✅ 已完成" if root_status == "done" else "⏳ 生成中"
    lines = [
        f"# 每日流程 {date_yy}",
        "",
        f"> {headline} — {started_at[:19] if started_at else ''}",
        "",
        "| 阶段 | 状态 | 进度 |",
        "|------|------|------|",
    ]

    for key in ("data_update", "pool", "quant", "bloom", "signal_plan", "signal_fundamentals", "capital_observer", "dashboard", "verify", "open_dashboard", "zixuan"):
        step = steps.get(key, {})
        status = step.get("status", "waiting")
        icon = _status_icon(status)
        label = _step_label(key)

        if _has_detail(key) and status == "running":
            total = step.get("total", 0)
            done = step.get("completed", 0)
            pct = f"{done / total * 100:.1f}%" if total else "-"
            cur_code = step.get("current_code", "")
            cur_name = step.get("current_name", "")
            cur_stage = step.get("current_stage", "")
            detail = f"{done}/{total} ({pct})"
            if cur_stage:
                detail += f" — {cur_stage}"
            if cur_code:
                detail += f" [{cur_code} {cur_name}]" if cur_name else f" [{cur_code}]"
        elif status == "done":
            elapsed = _step_time(step)
            detail = elapsed
        elif status == "error":
            detail = step.get("error", "未知错误")
        else:
            detail = ""

        lines.append(f"| {icon} {label} | {status} | {detail} |")

    if root_status in {"done", "failed"}:
        total_s = progress.get("total_elapsed_s", 0)
        if not total_s:
            started = datetime.fromisoformat(started_at) if started_at else None
            last_finish = max(
                (s.get("finished_at") for s in steps.values() if s.get("finished_at")),
                default=None,
            )
            if started and last_finish:
                total_s = round((datetime.fromisoformat(last_finish) - started).total_seconds())
        total_str = f"{total_s // 60}m{total_s % 60}s" if total_s else ""
    else:
        total_str = _step_time({"started_at": started_at}) if started_at else ""
    footer = (f"*流水线已中止：{progress.get('failure_reason', '未知原因')}*"
              if root_status == "failed" else "*流水线已完成。*" if root_status == "done"
              else "*流水线运行中。运行 `python3 scripts/monitor.py` 查看实时进度。*")
    lines.extend([
        "",
        f"> 已用时 {total_str}",
        "",
        "---",
        "",
        footer,
        "",
    ])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Daily pipeline progress monitor")
    parser.add_argument("--date", help="监控日期 YYMMDD 或 YYYY-MM-DD；默认自动检测")
    parser.add_argument("--interval", type=float, default=2.0, help="轮询间隔秒数，默认 2s")
    args = parser.parse_args()

    try:
        date_yy = normalize_date_arg(args.date)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    date_yy = _resolve_date(date_yy)
    if not date_yy:
        print("未找到运行中的流水线。请先启动 daily.py。")
        print("用法: python3 scripts/daily.py &")
        sys.exit(1)

    print(f"[monitor] 监控流水线 {date_yy}（间隔 {args.interval}s，Ctrl+C 退出）")
    try:
        while True:
            progress = _read_progress(date_yy)

            if progress is None:
                print(f"[monitor] 等待进度文件...")
                time.sleep(args.interval)
                continue

            status = progress.get("status", "unknown")

            if status in {"done", "failed"}:
                progress_path = _progress_markdown_path(date_yy)
                progress_path.write_text(_build_progress_markdown(progress), encoding="utf-8")

                error_count = sum(
                    1 for s in progress.get("steps", {}).values()
                    if s.get("status") == "error"
                )
                total = _step_time({"status": "done", "started_at": progress.get("started_at"),
                                    "elapsed_s": progress.get("total_elapsed_s")})
                icon = "✅" if status == "done" else "❌"
                label = "完成" if status == "done" else "中止"
                print(f"[monitor] {icon} 流水线{label} — 总耗时 {total}，{error_count} 个错误")
                print(f"[monitor] 进度记录: {progress_path}")
                if status == "failed" or error_count:
                    sys.exit(1)
                break

            md = _build_progress_markdown(progress)
            _progress_markdown_path(date_yy).write_text(md, encoding="utf-8")

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[monitor] 已退出监控（流水线仍在后台运行）")
        print(f"[monitor] 重新连接: python3 scripts/monitor.py --date {date_yy}")


if __name__ == "__main__":
    main()
