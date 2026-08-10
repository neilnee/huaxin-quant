#!/usr/bin/env python3
"""Shared next-session Signal Plan realization rules."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime
from pathlib import Path


SETUP_FAMILIES = {"PULLBACK", "BREAKOUT", "RETEST"}
FAILED_LIFECYCLE_STATES = {"POST_BREAKOUT_FAILED", "POST_BREAKOUT_EXPIRED"}


def date_yy(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%y%m%d")


def iso_date(value: str) -> str:
    return datetime.strptime(value, "%y%m%d").strftime("%Y-%m-%d")


def safe_float(value) -> float | None:
    try:
        number = None if value in (None, "") else float(value)
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def normalize_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    return value in ("True", "true", "TRUE", "1", 1)


def value_in_range(value: float, low, high) -> bool:
    low_value = safe_float(low)
    high_value = safe_float(high)
    return (low_value is None or value >= low_value) and (high_value is None or value <= high_value)


def structure_anchor(row: dict) -> str:
    group = row.get("contraction_group") or []
    if group and isinstance(group[0], dict) and group[0].get("start_date"):
        return str(group[0]["start_date"])
    contractions = row.get("contractions") or []
    if contractions and isinstance(contractions[-1], dict) and contractions[-1].get("start_date"):
        return str(contractions[-1]["start_date"])
    return str(row.get("structure_breakout_date") or "unanchored")


def plan_hit_grade(plan: dict, actual: dict) -> str | None:
    """Classify a next-session close as one mutually exclusive Plan grade."""
    family = str(plan.get("setup_family") or "").upper()
    close = safe_float(actual.get("close"))
    volume = safe_float(actual.get("volume"))
    if family not in SETUP_FAMILIES or close is None or volume is None:
        return None
    invalid = safe_float(plan.get("invalid_price"))
    if invalid is not None and close <= invalid:
        return None
    if not value_in_range(close, plan.get("trigger_price_low"), plan.get("trigger_price_high")):
        return None
    if family == "BREAKOUT":
        ordinary_limit = safe_float(plan.get("volume_min"))
        ordinary_volume_ok = ordinary_limit is not None and volume >= ordinary_limit
        ideal_limit = safe_float(plan.get("ideal_volume_min"))
        ideal_volume_ok = ideal_limit is not None and volume >= ideal_limit
    else:
        ordinary_limit = safe_float(plan.get("volume_max"))
        ordinary_volume_ok = ordinary_limit is not None and volume <= ordinary_limit
        ideal_limit = safe_float(plan.get("ideal_volume_max"))
        ideal_volume_ok = ideal_limit is not None and volume <= ideal_limit
    if not ordinary_volume_ok:
        return None
    ideal_price_ok = (
        safe_float(plan.get("ideal_price_low")) is not None
        and safe_float(plan.get("ideal_price_high")) is not None
        and value_in_range(close, plan.get("ideal_price_low"), plan.get("ideal_price_high"))
    )
    if plan.get("target_quality") == "A" and ideal_price_ok and ideal_volume_ok:
        return "A"
    return "REGULAR"


def same_structure(plan: dict, actual: dict, source: dict | None = None) -> tuple[bool, str]:
    expected = str(plan.get("structure_anchor") or structure_anchor(source or {}))
    actual_anchor = structure_anchor(actual)
    return expected == actual_anchor, expected


def realized_plan_event(
    plan: dict,
    actual: dict,
    source: dict | None,
    plan_date: str,
    entry_date: str,
    strategy_version: str,
) -> dict | None:
    family = str(plan.get("setup_family") or "").upper()
    action = str(plan.get("plan_action") or "").upper()
    grade = plan_hit_grade(plan, actual)
    structure_matches, anchor = same_structure(plan, actual, source)
    if grade is None or not structure_matches or action not in {"NEW", "FOLLOW"}:
        return None
    if not normalize_bool(actual.get("model2_include")):
        return None
    if not normalize_bool(actual.get("structure_valid")) or actual.get("structure_type") != "VCP":
        return None
    lifecycle = str(actual.get("post_breakout_state") or "PRE_BREAKOUT")
    if lifecycle in FAILED_LIFECYCLE_STATES:
        return None
    if family == "PULLBACK" and lifecycle != "PRE_BREAKOUT":
        return None
    pivot = (
        (plan.get("formula_ref") or {}).get("pivot")
        or actual.get("structure_pivot")
        or actual.get("pivot_price")
    )
    event = {
        "event_type": "BUY_POINT",
        "plan_date": plan_date,
        "plan_date_yy": date_yy(plan_date),
        "entry_date": entry_date,
        "entry_date_yy": date_yy(entry_date),
        "signal_date": entry_date,
        "signal_date_yy": date_yy(entry_date),
        "code": str(actual.get("code") or plan.get("code") or "").zfill(6),
        "name": actual.get("name") or plan.get("name") or plan.get("code"),
        "structure_anchor": anchor,
        "setup_family": family,
        "setup_type": f"{family}_BUY",
        "entry_action": action,
        "entry_grade": grade,
        "plan_target_quality": plan.get("target_quality") or "",
        "maturity_stage": plan.get("model2_stage") or (source or {}).get("structure_stage") or "NONE",
        "entry_structure_stage": actual.get("structure_stage") or "NONE",
        "model2_setup_quality": actual.get("setup_quality") or "",
        "entry_model2_setup_signal": actual.get("setup_signal") or "NONE",
        "post_breakout_state": lifecycle,
        "signal_close_snapshot": actual.get("close"),
        "entry_volume": actual.get("volume"),
        "structure_pivot": pivot,
        "structure_score": plan.get("structure_score"),
        "structure_risk_score": plan.get("structure_risk_score"),
        "plan_strategy_version": plan.get("strategy_version") or "",
        "quant_strategy_version": strategy_version,
    }
    for field in (
        "trigger_price_low", "trigger_price_high", "ideal_price_low", "ideal_price_high",
        "volume_min", "volume_max", "ideal_volume_min", "ideal_volume_max", "invalid_price",
        "plan_reason", "risk_note", "plan_priority",
    ):
        event[field] = plan.get(field)
    return event


def load_quant_rows(path: Path) -> tuple[dict, dict[str, dict]]:
    if not path.exists():
        return {}, {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = {
        str(row.get("code") or "").zfill(6): row
        for row in payload.get("results", [])
        if str(row.get("code") or "").strip("0")
    }
    return payload, rows


def previous_trading_date(date_value: str, market_db: Path) -> str | None:
    """Return D only when date_value is a valid market session and D is its predecessor."""
    if not market_db.exists():
        return None
    target = iso_date(date_value)
    conn = sqlite3.connect(f"file:{market_db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT trade_date FROM daily_bars WHERE trade_date<=?
               GROUP BY trade_date HAVING count(*)>=100 ORDER BY trade_date DESC LIMIT 2""",
            (target,),
        ).fetchall()
    finally:
        conn.close()
    if len(rows) < 2 or rows[0][0] != target:
        return None
    return str(rows[1][0])


def realized_events_for_date(date_value: str, plan_dir: Path, quant_dir: Path, market_db: Path) -> list[dict]:
    """Resolve only the previous valid session's Plans against date_value facts."""
    entry_date = iso_date(date_value)
    plan_date = previous_trading_date(date_value, market_db)
    if plan_date is None:
        return []
    plan_path = plan_dir / f"signal_plan_{date_yy(plan_date)}.json"
    if not plan_path.exists():
        return []
    source_payload, source_rows = load_quant_rows(quant_dir / f"quant_{date_yy(plan_date)}.json")
    entry_payload, entry_rows = load_quant_rows(quant_dir / f"quant_{date_value}.json")
    if not entry_rows:
        return []
    plan_payload = json.loads(plan_path.read_text(encoding="utf-8"))
    strategy_version = entry_payload.get("meta", {}).get("strategy_version", "")
    seen: set[tuple[str, str, str, str]] = set()
    events = []
    for plan in plan_payload.get("plans", []):
        code = str(plan.get("code") or "").zfill(6)
        actual = entry_rows.get(code)
        if actual is None:
            continue
        event = realized_plan_event(plan, actual, source_rows.get(code), plan_date, entry_date, strategy_version)
        if event is None:
            continue
        key = (code, event["structure_anchor"], event["setup_family"], event["entry_action"])
        if key in seen:
            continue
        seen.add(key)
        events.append(event)
    return events
