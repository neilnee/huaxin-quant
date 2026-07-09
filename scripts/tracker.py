#!/usr/bin/env python3
"""
Model 4 Tracker: unified daily signal orchestrator.

Orchestrates Bloom (lifecycle/quality) and Signal Plan (price/volume triggers),
then writes a single consolidated daily report under tracker/.
"""

import argparse
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config
from scripts import bloom as _bloom
from scripts import signal_plan as _plan


TRACKER_STRATEGY_FILE = "04-tracker.json"
TRACKER_CONFIG, _ = load_strategy_config(TRACKER_STRATEGY_FILE)
TRACKER_VERSION = TRACKER_CONFIG["strategy_version"]

TRACKER_DIR = Path(PROJECT_ROOT) / TRACKER_CONFIG["outputs"]["report_dir"]


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


def normalize_date_arg(value):
    if not value:
        return None
    raw = value.strip()
    if re.fullmatch(r"\d{6}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%y%m%d")
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


# ── markdown section extraction ──

def _extract_section(md, heading):
    """Extract content from a ## heading to the next ## heading (or end of document).
    Returns the full section including the heading line. Heading can be a prefix
    (e.g. '📊 今日概要' matches '## 📊 今日概要')."""
    escaped = re.escape(heading)
    pattern = rf'(^## {escaped}.*?$\n)(.*?)(?=^## |\Z)'
    match = re.search(pattern, md, re.MULTILINE | re.DOTALL)
    if not match:
        return ""
    return (match.group(1) + match.group(2)).strip()


def _extract_plan_families(md):
    """Extract the three buy-point family sections from plan markdown.
    Returns dict family_name -> section text (including heading)."""
    families = {}
    for key, label in [
        ("PULLBACK", "PULLBACK 缩量回踩"),
        ("BREAKOUT", "BREAKOUT 枢轴突破"),
        ("RETEST", "RETEST 突破回踩确认"),
    ]:
        section = _extract_section(md, label)
        if section:
            families[key] = section
    return families


def _extract_bloom_section(md, heading):
    """Extract a specific bloom section by its heading prefix."""
    return _extract_section(md, heading)


# ── overview builder ──

def _build_overview(b_summary, p_summary):
    b_llm = (b_summary.get("llm") or {}).get("status", "unknown")
    p_llm = (p_summary.get("llm") or {}).get("status", "unknown")

    lines = [
        "## 📊 总览",
        "",
        "| 模块 | 数据 |",
        "|------|------|",
        f"| Bloom | 活跃 **{b_summary.get('active_total', 0)}** 只 · 触发 **{b_summary.get('triggered', 0)}** 只 · 新入 **{b_summary.get('new_entries', 0)}** 只 · 阻断 **{b_summary.get('risk_blocked', 0)}** 只 · 移出 **{b_summary.get('exits', 0)}** 只 |",
        f"| Signal Plan | 计划 **{p_summary.get('plan_total', 0)}** 条 / **{p_summary.get('stock_total', 0)}** 只 · NEW **{p_summary.get('new_total', 0)}** · FOLLOW **{p_summary.get('follow_total', 0)}** · A级 **{p_summary.get('quality_dist', {}).get('A', 0)}** · B级 **{p_summary.get('quality_dist', {}).get('B', 0)}** |",
    ]
    if p_summary.get("overextended_total"):
        lines.append(f"| | ⚠️ 过度延伸排除 **{p_summary['overextended_total']}** 只 |")
    lines.extend([
        f"| LLM | Bloom: **{b_llm}** · Plan: **{p_llm}** |",
        "",
        "> 完整报告：Bloom → `bloom/` · Plan → `signal_plan/` · 策略版本 → 见页脚",
        "",
    ])
    return "\n".join(lines)


# ── combined field notes ──

def _merged_field_notes():
    return [
        "## 📖 字段说明",
        "",
        "### Bloom（重点观察 / 全量观察）",
        "",
        "| 字段 | 说明 |",
        "|------|------|",
        "| 结构 | `VCP阶段 / 结构评分`，如 `VCP_FORMING / 83分`；阶段越高结构越成熟 |",
        "| Bloom | 生命周期状态：`EARLY` 早期 / `FORMING` 形成中 / `MATURE` 成熟 / `TRIGGERED` 触发买点 / `RISK_BLOCKED` 风险阻断 / `COOLDOWN` 冷却 / `INVALID` 失效 / `EXIT` 移出 |",
        "| 买点 | `信号类型 / 评分 / 质量 / 仓位`；无买点显示 `-` |",
        "| 风险 | `LOW` / `MEDIUM` / `HIGH` / `HARD`；⚠️ 前缀 = 高风险及以上 |",
        "| 观察要点 | LLM 生成的解读摘要，未生成时回退为规则文本 |",
        "| 🔰 | 日式新手标 = 今日新进入观察池 |",
        "| ↳ 收缩明细 | `N段：-x.xx%（d天） → ...`，每轮 VCP 回调幅度及持续天数 |",
        "",
        "### Signal Plan（买点计划）",
        "",
        "| 字段 | 说明 |",
        "|------|------|",
        "| 动作 | `NEW` = 次日可能首次触发该类买点 / `FOLLOW` = 今日已触发同类买点，次日仍在有效区间可延续观察 |",
        "| 最高 | 该计划最高潜在等级（A / B / C）；仅 A 级展示 A 类量价区 |",
        "| 触发价 | 次日收盘价落入该区间进入买点触发范围 |",
        "| A级价 | 更严格的 A 类价位区间；最高不足 A 级时显示 `-` |",
        "| 触发量 | 普通买点最低放量或最高缩量条件；「以上」多用于突破，「以下」多用于回踩 |",
        "| A级量 | A 类买点的更严格量能条件；最高不足 A 级时不展示 |",
        "| 失效 | 跌破后不再按当前买点计划处理 |",
        "| 说明 | LLM 生成的量价区间说明；调用失败时回退为确定性说明 |",
        "",
    ]


# ── consolidated report builder ──

def build_consolidated_markdown(bloom_data, plan_data, date_yy, bloom_md, plan_md):
    b_summary = bloom_data.get("summary", {})
    p_summary = plan_data.get("summary", {})

    # Extract sections from bloom markdown
    bloom_focus = _extract_bloom_section(bloom_md, "🔥 重点观察")
    bloom_overview = _extract_bloom_section(bloom_md, "📋 全量观察")

    # Extract family sections from plan markdown
    plan_families = _extract_plan_families(plan_md)

    lines = [
        f"# Tracker Daily Report {date_yy}",
        "",
    ]

    # ── 1. Overview ──
    lines.append(_build_overview(b_summary, p_summary))

    # ── 2. Bloom 重点观察 ──
    lines.append("")
    if bloom_focus:
        lines.append(bloom_focus)
    else:
        lines.extend(["## 🔥 重点观察", "", "*今日无符合条件的结构*"])
    lines.append("")

    # ── 3. Signal Plan 买点计划 ──
    lines.append("## 📐 买点计划")
    lines.append("")
    if plan_families:
        for family in ("PULLBACK", "BREAKOUT", "RETEST"):
            section = plan_families.get(family)
            if section:
                lines.append(section)
                lines.append("")
    else:
        lines.extend(["*今日无买点计划*", ""])

    # ── 4. Bloom 全量观察 ──
    if bloom_overview:
        lines.append(bloom_overview)
        lines.append("")

    # ── 5. Merged field notes ──
    lines.extend(_merged_field_notes())
    lines.append(
        f"> 策略版本：Tracker {TRACKER_VERSION} · "
        f"Bloom {b_summary.get('strategy_version', '')} · "
        f"Plan {p_summary.get('strategy_version', '')}"
    )
    lines.append("")

    return "\n".join(lines) + "\n"


# ── main ──

def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Model 4 Tracker: unified daily signal orchestrator")
    parser.add_argument("--date", help="运行日期 YYMMDD 或 YYYY-MM-DD；默认取最新 quant run")
    parser.add_argument("--skip-bloom", action="store_true", help="跳过 Bloom 信号层")
    parser.add_argument("--skip-plan", action="store_true", help="跳过 Signal Plan 层")
    args = parser.parse_args()

    try:
        date_yy = normalize_date_arg(args.date)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    quant_path = _bloom.quant_run_for_date(date_yy) if date_yy else _bloom.latest_quant_run()
    if not quant_path:
        print("错误: 未找到模型二 quant run JSON")
        sys.exit(1)
    match = re.fullmatch(r"quant_(\d{6})\.json", quant_path.name)
    if not match:
        print(f"错误: 不支持的 quant run 文件名: {quant_path}")
        sys.exit(1)
    date_yy = match.group(1)

    payload = _bloom.load_json(quant_path)

    # ── 1. Bloom ──
    if not args.skip_bloom:
        _bloom.ensure_dirs()
        prev_path = _bloom.previous_quant_run(date_yy)
        previous_payload = _bloom.load_json(prev_path) if prev_path else None
        bloom_data, new_state, events = _bloom.build_bloom(payload, previous_payload, date_yy)
        _bloom.write_state_snapshot_before(bloom_data["summary"]["date"])
        _bloom.write_state(new_state)
        _bloom.write_events(bloom_data["summary"]["date"], events)
        _bloom.write_bloom_input(date_yy, bloom_data)
        bloom_md = _bloom.build_markdown(bloom_data)
        _bloom.write_markdown(date_yy, bloom_md)
    else:
        bloom_data = {"summary": {}, "sections": {}}
        bloom_md = ""

    # ── 2. Signal Plan ──
    if not args.skip_plan:
        plan_data = _plan.build_signal_plan(payload, date_yy)
        plan_data = _plan.attach_llm_notes(plan_data)
        plan_md = _plan.build_markdown(plan_data)
        _plan.write_json_plan(date_yy, plan_data)
        _plan.write_markdown_plan(date_yy, plan_md)
    else:
        plan_data = {"summary": {}, "sections": {}, "plans": []}
        plan_md = ""

    # ── 3. Consolidated report ──
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    consolidated = build_consolidated_markdown(bloom_data, plan_data, date_yy, bloom_md, plan_md)
    tracker_path = TRACKER_DIR / f"tracker_{date_yy}.md"
    with open(tracker_path, "w", encoding="utf-8") as f:
        f.write(consolidated)

    print("=" * 70)
    print("Model 4 Tracker")
    print("=" * 70)
    print(f"quant run:      {quant_path}")
    print(f"tracker report: {tracker_path}")
    print(f"bloom active:   {bloom_data.get('summary', {}).get('active_total', 0)}")
    print(f"plan total:     {plan_data.get('summary', {}).get('plan_total', 0)}")

    llm_status = (bloom_data.get("summary", {}).get("llm") or {}).get("status")
    if llm_status == "failed":
        sys.exit(3)


if __name__ == "__main__":
    main()
