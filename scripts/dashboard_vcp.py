#!/usr/bin/env python3
"""Publish a read-only VCP dashboard package from Bloom and Model 2 outputs."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)
BLOOM_INPUT_DIR = ROOT / "bloom" / "state"
QUANT_RUN_DIR = ROOT / "cache" / "quant_runs"
MARKET_DATA_DB = ROOT / "cache" / "market_data" / "market_data.sqlite"
MARKET_REGIME_DB = ROOT / "cache" / "market_regime" / "market_regime.sqlite"
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"
DASHBOARD_START_DATE = "260716"
VOLUME_PATTERN_LABELS = {
    "decreasing": "量能持续递减",
    "drying": "量能逐步萎缩",
    "flat": "量能基本持平",
    "mixed": "量能未呈持续缩减",
    "failed": "量能未达到缩量要求",
}


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


def display_vcp_text(value):
    """Translate Model 2's stable enum terms only in the dashboard package."""
    if isinstance(value, list):
        return [display_vcp_text(item) for item in value]
    text = str(value or "")
    for raw, label in VOLUME_PATTERN_LABELS.items():
        text = text.replace(f"量能{raw}", label)
    return VOLUME_PATTERN_LABELS.get(text, text)


def load_industry_context(codes: list[str], run_date: str) -> dict[str, dict]:
    if not codes or not MARKET_DATA_DB.exists() or not MARKET_REGIME_DB.exists():
        return {}
    placeholders = ",".join("?" for _ in codes)
    try:
        market_conn = sqlite3.connect(f"file:{MARKET_DATA_DB}?mode=ro", uri=True)
        market_conn.row_factory = sqlite3.Row
        rows = market_conn.execute(
            f"""SELECT si.code, id.industry_name
                FROM stock_industries si LEFT JOIN industry_definitions id
                  ON id.snapshot_date=si.snapshot_date AND id.industry_system='sw'
                 AND id.industry_code=substr(si.sw_industry_code, 1, 5)
                WHERE si.snapshot_date=? AND si.code IN ({placeholders})""",
            [run_date, *codes],
        ).fetchall()
        market_conn.close()
        state_conn = sqlite3.connect(f"file:{MARKET_REGIME_DB}?mode=ro", uri=True)
        state_conn.row_factory = sqlite3.Row
        sector_rows = state_conn.execute(
            """SELECT block_name, sector_state, rank_20, relative_strength_20
                 FROM sector_daily_metrics
                 WHERE trade_date=? AND block_kind='industry_sw_l2'""",
            (run_date,),
        ).fetchall()
        state_conn.close()
    except sqlite3.Error:
        return {}
    sector_by_name = {row["block_name"]: dict(row) for row in sector_rows}
    return {row["code"]: {"sw_l2_name": row["industry_name"] or "", "sector": sector_by_name.get(row["industry_name"], {})} for row in rows}


def compact_candidate(row: dict, quant: dict, industry: dict) -> dict:
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
    quant_overrides = {
        "model2_stage": "structure_stage",
        "model2_setup_signal": "setup_signal",
        "model2_action_hint": "action_hint",
        "structure_score": "structure_score",
        "structure_risk_score": "structure_risk_score",
        "structure_risk_flags": "structure_risk_flags",
        "setup_score": "setup_score",
        "setup_quality": "setup_quality",
        "setup_reasons": "setup_reasons",
        "setup_misses": "setup_misses",
        "close": "close",
        "MA20": "MA20",
        "MA60": "MA60",
        "distance_ma20": "distance_ma20",
        "pivot_distance": "pivot_distance",
        "volume_dry_up": "volume_dry_up",
        "volume_pattern": "volume_pattern",
        "contraction_count": "contraction_count",
        "contraction_pcts": "contraction_pcts",
        "contraction_days": "contraction_days",
    }
    for target, source in quant_overrides.items():
        if source in quant:
            result[target] = quant[source]
    for field in ("support_price", "invalid_price", "pivot_price", "breakout_level", "structure_type", "score_components", "structure_conditions", "structure_misses"):
        result[field] = quant.get(field, "")
    # The full contraction scan is retained in Model 2 for audit.  The dashboard
    # must show only the group selected as the current valid VCP structure.
    result["contractions"] = quant.get("contraction_group", [])
    result["volume_pattern"] = display_vcp_text(result.get("volume_pattern"))
    result["structure_conditions"] = display_vcp_text(result["structure_conditions"])
    result["structure_misses"] = display_vcp_text(result["structure_misses"])
    result["sw_l2_name"] = industry.get("sw_l2_name", "")
    result["sector"] = industry.get("sector", {})
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
    industry_by_code = load_industry_context([str(row.get("code", "")).zfill(6) for row in active], bloom.get("summary", {}).get("date", ""))
    candidates = [compact_candidate(row, quant_by_code.get(str(row.get("code", "")).zfill(6), {}), industry_by_code.get(str(row.get("code", "")).zfill(6), {})) for row in active]
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
