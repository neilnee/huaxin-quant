#!/usr/bin/env python3
"""
Progress monitor for the daily pipeline. Polls the progress JSON written by
daily.py and renders a live progress page to 花期策览_<date>.md.

When the pipeline finishes, replaces the progress page with the final report.

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
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config


TRACKER_CONFIG, _ = load_strategy_config("04-tracker.json")
TRACKER_DIR = Path(PROJECT_ROOT) / TRACKER_CONFIG["outputs"]["report_dir"]
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
    """Try to find the running/completed pipeline for the given date."""
    if date_yy:
        return date_yy
    # Look for the most recent progress file
    files = sorted(PROGRESS_DIR.glob("daily_progress_*.json"))
    if files:
        match = re.fullmatch(r"daily_progress_(\d{6})\.json", files[-1].name)
        if match:
            return match.group(1)
    return None


def _progress_file(date_yy):
    return PROGRESS_DIR / f"daily_progress_{date_yy}.json"


def _report_path(date_yy):
    return TRACKER_DIR / f"花期策览_{date_yy}.md"


def _read_progress(date_yy):
    path = _progress_file(date_yy)
    if not path.exists():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


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
        "pool": "模型一 Pool",
        "quant": "模型二 Quant",
        "bloom": "Bloom 信号层",
        "plan": "Signal Plan",
        "assemble": "合并报告",
    }
    return labels.get(step, step)


def _build_progress_markdown(progress):
    date_yy = progress.get("date", "")
    steps = progress.get("steps", {})
    started_at = progress.get("started_at", "")
    lines = [
        f"# 花期策览 {date_yy}",
        "",
        f"> ⏳ 生成中 — {started_at[:19] if started_at else ''}",
        "",
        "| 阶段 | 状态 | 进度 |",
        "|------|------|------|",
    ]

    for key in ("pool", "quant", "bloom", "plan", "assemble"):
        step = steps.get(key, {})
        status = step.get("status", "waiting")
        icon = _status_icon(status)
        label = _step_label(key)

        if key == "quant" and status == "running":
            total = step.get("total", 0)
            done = step.get("completed", 0)
            pct = f"{done / total * 100:.1f}%" if total else "-"
            cur_code = step.get("current_code", "")
            cur_name = step.get("current_name", "")
            cur_stage = step.get("current_stage", "")
            detail = f"{done}/{total} ({pct})"
            if cur_code:
                detail += f" — {cur_code} {cur_name} {cur_stage}"
        elif status == "done":
            elapsed = _step_time(step)
            detail = elapsed
        elif status == "error":
            detail = step.get("error", "未知错误")
        else:
            detail = ""

        lines.append(f"| {icon} {label} | {status} | {detail} |")

    if progress.get("status") == "done":
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
    lines.extend([
        "",
        f"> 已用时 {total_str}",
        "",
        "---",
        "",
        "*报告生成中，完成后自动替换为完整内容。运行 `python3 scripts/monitor.py` 查看实时进度。*",
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
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)

    try:
        while True:
            progress = _read_progress(date_yy)

            if progress is None:
                print(f"[monitor] 等待进度文件...")
                time.sleep(args.interval)
                continue

            status = progress.get("status", "unknown")

            if status == "done":
                # Pipeline finished — copy final report
                final_path = _report_path(date_yy)
                if final_path.exists():
                    content = final_path.read_text(encoding="utf-8")
                else:
                    content = _build_progress_markdown(progress)

                report_path = TRACKER_DIR / f"花期策览_{date_yy}.md"
                with open(report_path, "w", encoding="utf-8") as f:
                    f.write(content)

                error_count = sum(
                    1 for s in progress.get("steps", {}).values()
                    if s.get("status") == "error"
                )
                total = _step_time({"status": "done", "started_at": progress.get("started_at"),
                                    "elapsed_s": progress.get("total_elapsed_s")})
                print(f"[monitor] ✅ 流水线完成 — 总耗时 {total}，{error_count} 个错误")
                print(f"[monitor] 报告: {report_path}")
                if error_count:
                    sys.exit(1)
                break

            # Update progress page
            md = _build_progress_markdown(progress)
            report_path = TRACKER_DIR / f"花期策览_{date_yy}.md"
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(md)

            time.sleep(args.interval)

    except KeyboardInterrupt:
        print("\n[monitor] 已退出监控（流水线仍在后台运行）")
        print(f"[monitor] 重新连接: python3 scripts/monitor.py --date {date_yy}")


if __name__ == "__main__":
    main()
