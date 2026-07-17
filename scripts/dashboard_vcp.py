#!/usr/bin/env python3
"""Publish a read-only VCP dashboard package from Bloom and Model 2 outputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)
BLOOM_INPUT_DIR = ROOT / "bloom" / "state"
QUANT_RUN_DIR = ROOT / "cache" / "quant_runs"
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"
DASHBOARD_START_DATE = "260716"


def normalize_date(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        return digits[2:]
    if len(digits) == 6:
        return digits
    raise ValueError("日期应为 YYMMDD 或 YYYY-MM-DD")


def latest_date() -> str:
    files = sorted(BLOOM_INPUT_DIR.glob("bloom_input_*.json"))
    if not files:
        raise FileNotFoundError("未找到 Bloom 输入文件")
    return files[-1].stem.rsplit("_", 1)[-1]


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def compact_candidate(row: dict, quant: dict) -> dict:
    fields = (
        "code", "name", "bloom_status", "bloom_signal", "event_type", "pool_decision",
        "signal_quality", "risk_level", "model2_stage", "model2_setup_signal", "model2_action_hint",
        "structure_score", "structure_risk_score", "structure_risk_flags", "setup_score", "setup_quality",
        "setup_reasons", "setup_misses", "close", "MA20", "MA60", "distance_ma20", "pivot_distance",
        "volume_dry_up", "volume_pattern", "contraction_count", "contraction_pcts", "contraction_days",
        "days_tracked", "days_in_observation", "score_change", "watch_reason", "next_watch_point",
        "llm_insight", "valuation_candidate", "valuation_priority",
    )
    result = {field: row.get(field, "") for field in fields}
    for field in ("support_price", "invalid_price", "pivot_price", "breakout_level", "structure_type"):
        result[field] = quant.get(field, "")
    return result


def build_context(date_yy: str) -> dict:
    bloom_path = BLOOM_INPUT_DIR / f"bloom_input_{date_yy}.json"
    if not bloom_path.exists():
        raise FileNotFoundError(f"未找到 {bloom_path.name}")
    bloom = load_json(bloom_path)
    quant_path = QUANT_RUN_DIR / f"quant_{date_yy}.json"
    quant_results = load_json(quant_path).get("results", []) if quant_path.exists() else []
    quant_by_code = {str(row.get("code", "")).zfill(6): row for row in quant_results}
    active = bloom.get("sections", {}).get("active", [])
    candidates = [compact_candidate(row, quant_by_code.get(str(row.get("code", "")).zfill(6), {})) for row in active]
    candidates.sort(key=lambda row: (row["bloom_status"] != "TRIGGERED", row["bloom_status"] != "MATURE", -float(row["structure_score"] or 0)))
    return {
        "meta": {"run_date": bloom.get("summary", {}).get("date"), "source": bloom_path.name,
                 "quant_source": quant_path.name if quant_path.exists() else None},
        "summary": bloom.get("summary", {}),
        "candidates": candidates,
    }


def publish(date_yy: str) -> Path:
    context = build_context(date_yy)
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    month_dir = DASHBOARD_DATA_DIR / f"20{date_yy[:4]}"
    month_dir.mkdir(parents=True, exist_ok=True)
    output = month_dir / f"vcp_context_{date_yy}.js"
    output.write_text(
        "window.QUANT_DASHBOARD_VCP_CONTEXTS = window.QUANT_DASHBOARD_VCP_CONTEXTS || {};\n"
        f"window.QUANT_DASHBOARD_VCP_CONTEXTS[{json.dumps(date_yy)}] = "
        + json.dumps(context, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    for path in DASHBOARD_DATA_DIR.glob("vcp_context_*.js"):
        path.unlink()
    for path in DASHBOARD_DATA_DIR.glob("*/vcp_context_*.js"):
        if path.stem.rsplit("_", 1)[-1] < DASHBOARD_START_DATE:
            path.unlink()
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="发布 VCP 结构页数据包")
    parser.add_argument("--date", help="Bloom 日期，支持 YYMMDD 或 YYYY-MM-DD；默认最新 Bloom 输出")
    parser.add_argument("--all", action="store_true", help="发布所有已有 Bloom 日期")
    args = parser.parse_args()
    dates = sorted(path.stem.rsplit("_", 1)[-1] for path in BLOOM_INPUT_DIR.glob("bloom_input_*.json") if path.stem.rsplit("_", 1)[-1] >= DASHBOARD_START_DATE) if args.all else [normalize_date(args.date) if args.date else latest_date()]
    outputs = [str(publish(date_yy)) for date_yy in dates]
    print(json.dumps({"dates": dates, "outputs": outputs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
