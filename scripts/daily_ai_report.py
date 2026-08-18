#!/usr/bin/env python3
"""Publish one deterministic, AI-oriented JSON package from Dashboard data."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.io_utils import atomic_write_text
from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"
OUTPUT_ROOT = ROOT / "reports" / "ai_daily"
SCHEMA_VERSION = "huaxin_ai_daily_v1.1"

MODULES = {
    "market": ("QUANT_DASHBOARD_MARKET_CONTEXTS", "run_date"),
    "capital": ("QUANT_DASHBOARD_CAPITAL_CONTEXTS", "trade_date"),
    "vcp": ("QUANT_DASHBOARD_VCP_CONTEXTS", "run_date"),
    "signals": ("QUANT_DASHBOARD_SIGNALS_CONTEXTS", "run_date"),
}

NULL_STRINGS = {"", "—", "-", "null", "None"}
INTEGER_FIELDS = {"days_tracked", "days_in_observation", "score_change"}
BOOLEAN_FIELDS = {"valuation_candidate"}

VCP_SECTOR_INTEGER_FIELDS = {
    "member_count", "rank", "rank_1", "rank_5", "rank_20", "sector_health_level",
}
VCP_SECTOR_FLOAT_FIELDS = {
    "return_1", "return_5", "return_10", "return_20",
    "relative_strength_1", "relative_strength_5", "relative_strength_20",
    "volume_activity", "up_breadth", "up_breadth_5", "daily_strong_density",
    "median_return_1", "above_ma20_ratio", "above_ma60_ratio", "new_high_ratio",
    "strong_stock_density", "rank_pct_20", "rank_pct_5", "daily_score",
    "sector_health_score",
}
VCP_SECTOR_BOOLEAN_FIELDS = {"short_pulse"}

PLAN_FAMILY_PREFIXES = {
    "BREAKOUT": "breakout",
    "PULLBACK": "pullback",
    "RETEST": "retest",
}

ABSOLUTE_VOLUME_FIELDS = [
    "volume", "vol_ma5", "vol_ma20", "vol_ma60", "avg_volume",
    "volume_min", "volume_max", "ideal_volume_min", "ideal_volume_max",
    "fixed_window_volume_threshold", "segment_volume_threshold", "volume_floor_threshold",
    "volume_ma20_threshold", "volume_ma5_threshold", "recent_breakout_volume",
    "recent_breakout_vol_ma20", "volume_threshold", "reference_volume",
    "latest_volume", "previous_volume_avg", "breakout_volume", "entry_volume",
]


class ReportInputError(RuntimeError):
    """Raised when a required same-day Dashboard input is unavailable or invalid."""


def normalize_date(value: str) -> tuple[str, str]:
    raw = str(value or "").strip()
    for fmt in ("%y%m%d", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.strftime("%y%m%d"), parsed.strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def context_path(data_dir: Path, module: str, date_yy: str) -> Path:
    month = f"20{date_yy[:4]}"
    return data_dir / month / f"{module}_context_{date_yy}.js"


def extract_context(path: Path, variable: str, date_yy: str) -> dict:
    if not path.exists():
        raise ReportInputError(f"required Dashboard package missing: {path}")
    text = path.read_text(encoding="utf-8")
    pattern = re.compile(
        rf"window\.{re.escape(variable)}\[{re.escape(json.dumps(date_yy))}\]\s*=\s*(\{{.*\}});\s*$",
        re.DOTALL,
    )
    match = pattern.search(text)
    if not match:
        raise ReportInputError(f"Dashboard assignment missing or malformed: {path}")
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError as exc:
        raise ReportInputError(f"Dashboard JSON invalid: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ReportInputError(f"Dashboard payload must be an object: {path}")
    return payload


def load_contexts(date_yy: str, date_iso: str, data_dir: Path = DASHBOARD_DATA_DIR) -> tuple[dict, dict]:
    contexts = {}
    source_files = {}
    for module, (variable, date_field) in MODULES.items():
        path = context_path(data_dir, module, date_yy)
        payload = extract_context(path, variable, date_yy)
        actual_date = str((payload.get("meta") or {}).get(date_field) or "")
        if actual_date != date_iso:
            raise ReportInputError(
                f"Dashboard date mismatch for {module}: {actual_date or 'missing'} != {date_iso}"
            )
        contexts[module] = payload
        source_files[module] = path.name
    return contexts, source_files


def normalize_value(value: Any, key: str = "") -> Any:
    if isinstance(value, dict):
        return {item_key: normalize_value(item_value, item_key) for item_key, item_value in value.items()}
    if isinstance(value, list):
        return [normalize_value(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"non-finite number in field {key or '<array>'}")
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in NULL_STRINGS:
            return None
        if stripped.startswith("/"):
            return Path(stripped).name
        if key in BOOLEAN_FIELDS and stripped.lower() in {"true", "false"}:
            return stripped.lower() == "true"
        if key in INTEGER_FIELDS and re.fullmatch(r"-?\d+(?:\.0+)?", stripped):
            return int(float(stripped))
    return value


def clean_market_meta(meta: dict) -> dict:
    allowed = (
        "run_date", "strategy_version", "data_status", "reference_snapshot_date",
        "history_basis", "history_window_days", "sector_history_note", "baseline_history_days",
    )
    return {key: meta.get(key) for key in allowed}


def build_sector_rankings(market: dict) -> dict:
    rankings = market.get("sector_rankings") or {}
    leaders = market.get("sector_leaders") or {}
    categories = {}
    for category in ("industry_sw_l1", "industry_sw_l2", "gn", "fg"):
        items = []
        for source in rankings.get(category) or []:
            row = dict(source)
            leader_key = f"{row.get('block_type', category)}:{row.get('block_name', '')}"
            row["representative_stocks"] = leaders.get(leader_key) or []
            items.append(row)
        categories[category] = items
    return {
        "source_type": "calculated_metric",
        "categories": categories,
    }


def matched_plan_type(setup_signal: Any) -> str | None:
    normalized = str(setup_signal or "").strip().upper()
    for prefix, plan_type in PLAN_FAMILY_PREFIXES.items():
        if normalized.startswith(prefix):
            return plan_type
    return None


def typed_machine_value(value: Any, field: str, target: str) -> Any:
    normalized = normalize_value(value, field)
    if normalized is None:
        return None
    if target == "boolean":
        if isinstance(normalized, bool):
            return normalized
        if isinstance(normalized, str) and normalized.lower() in {"true", "false"}:
            return normalized.lower() == "true"
        raise ValueError(f"invalid boolean machine value for sector.{field}: {value!r}")
    if isinstance(normalized, bool):
        raise ValueError(f"invalid numeric machine value for sector.{field}: {value!r}")
    if isinstance(normalized, (int, float)):
        number = float(normalized)
    elif isinstance(normalized, str) and re.fullmatch(r"-?\d+(?:\.\d+)?", normalized):
        number = float(normalized)
    else:
        raise ValueError(f"invalid numeric machine value for sector.{field}: {value!r}")
    if target == "integer":
        if not number.is_integer():
            raise ValueError(f"non-integer machine value for sector.{field}: {value!r}")
        return int(number)
    return number


def normalize_vcp_sector(source: Any) -> Any:
    if not isinstance(source, dict):
        return source
    sector = dict(source)
    for field in VCP_SECTOR_INTEGER_FIELDS:
        if field in sector:
            sector[field] = typed_machine_value(sector[field], field, "integer")
    for field in VCP_SECTOR_FLOAT_FIELDS:
        if field in sector:
            sector[field] = typed_machine_value(sector[field], field, "float")
    for field in VCP_SECTOR_BOOLEAN_FIELDS:
        if field in sector:
            sector[field] = typed_machine_value(sector[field], field, "boolean")
    return sector


def structure_anchor(source: dict) -> str | None:
    contractions = source.get("contractions") or []
    if contractions and isinstance(contractions[0], dict):
        anchor = str(contractions[0].get("start_date") or "").strip()
        if anchor:
            return anchor
    return None


def structure_id(code: Any, anchor: Any) -> str | None:
    normalized_code = str(code or "").strip()
    normalized_anchor = str(anchor or "").strip()
    if not re.fullmatch(r"\d{6}", normalized_code) or not normalized_anchor or normalized_anchor == "unanchored":
        return None
    return f"{normalized_code}:{normalized_anchor}"


def build_vcp_items(source_items: list) -> list:
    items = []
    for source in source_items:
        row = dict(source)
        row["sector"] = normalize_vcp_sector(row.get("sector"))
        anchor = structure_anchor(row)
        row["structure_anchor"] = anchor
        row["structure_id"] = structure_id(row.get("code"), anchor)
        items.append(row)
    return items


def build_vcp_summary(source: dict, vcp_items: list) -> dict:
    summary = dict(source)
    legacy_counts = dict(summary.pop("status_dist", {}) or {})
    source_counts = dict(summary.pop("source_status_dist", {}) or legacy_counts)
    display_statuses = ("TRIGGERED", "MATURE", "FORMING", "EARLY", "RISK_BLOCKED", "COOLDOWN")
    display_counts = {
        status: sum(row.get("bloom_status") == status for row in vcp_items)
        for status in display_statuses
    }
    summary["status_counts"] = {
        "source_lifecycle_snapshot": {
            "count_basis": "bloom_lifecycle_snapshot_after_state_merge",
            "mutually_exclusive": True,
            "total": sum(int(value or 0) for value in source_counts.values()),
            "counts": source_counts,
            "comparison_note": "This includes previously tracked lifecycle state and is not directly comparable to current-day Quant input_total or result_total.",
        },
        "display_vcp_items": {
            "count_basis": "vcp_structures.items",
            "mutually_exclusive": True,
            "total": len(vcp_items),
            "counts": display_counts,
        },
    }
    return summary


def signal_record_role(signal_kind: Any) -> str:
    return {
        "TRIGGERED": "TODAY_TRIGGER",
        "PLAN": "NEXT_DAY_PLAN",
    }.get(str(signal_kind or "").strip().upper(), "UNKNOWN")


def build_signal_items(source_items: list, vcp_by_code: dict, date_iso: str) -> list:
    items = []
    for source in source_items:
        row = dict(source)
        plans = row.pop("setup_plan_inputs", None)
        plans = plans if isinstance(plans, dict) else {}
        matched_plan = matched_plan_type(row.get("setup_signal"))
        code = str(row.get("code") or "").strip()
        role = signal_record_role(row.get("signal_kind"))
        plan_anchor = str(((row.get("plan_inputs") or {}).get("structure_anchor") or "")).strip()
        parent_structure_id = structure_id(code, plan_anchor)
        if parent_structure_id is None:
            parent_structure_id = (vcp_by_code.get(code) or {}).get("structure_id")
        row["record_role"] = role
        row["signal_id"] = f"{code}:{date_iso}:{role}:{row.get('setup_signal') or 'NONE'}"
        row["parent_structure_id"] = parent_structure_id
        row.pop("support_price", None)
        row.pop("invalid_price", None)
        row["trade_price_semantics"] = {
            "canonical_for_ai_and_downstream": True,
            "matched_plan": matched_plan if matched_plan in plans else None,
            "structure": {
                "pivot_price": row.get("pivot_price"),
                "structure_pivot": row.get("structure_pivot"),
                "breakout_level": row.get("breakout_level"),
                "last_contraction_low": row.get("last_contraction_low"),
            },
            "plans": {
                plan_type: plans.get(plan_type) or {}
                for plan_type in ("breakout", "pullback", "retest")
            },
        }
        items.append(row)
    return items


def market_llm_validation(market: dict) -> dict:
    state = market.get("market_state") or {}
    analysis = str(state.get("analysis") or "").strip()
    indexes = [row for row in (market.get("indexes") or {}).values() if isinstance(row, dict)]
    total = len(indexes)
    facts = {
        "broad_index_total": total,
        "above_ma20_count": sum(row.get("above_ma20") is True for row in indexes),
        "above_ma60_count": sum(row.get("above_ma60") is True for row in indexes),
    }
    result = {
        "status": "NOT_APPLICABLE",
        "analysis_safe_to_use": None,
        "validator": "deterministic_rule",
        "checks_run": ["broad_index_ma20_coverage", "broad_index_ma60_coverage"],
        "structured_facts": facts,
        "fact_conflicts": [],
    }
    if not analysis or state.get("analysis_source") != "llm" or total == 0:
        return result

    sentences = [part.strip() for part in re.split(r"[。！？；;，,]", analysis) if part.strip()]
    recovery_verbs = ("重回", "站上", "收复", "回到")
    for ma_key, count_key in (("MA20", "above_ma20_count"), ("MA60", "above_ma60_count")):
        count = facts[count_key]
        for sentence in sentences:
            compact = re.sub(r"\s+", "", sentence).upper()
            if ma_key not in compact:
                continue
            conflict_code = None
            if count == total and "更多宽基" in compact and any(verb in compact for verb in recovery_verbs):
                conflict_code = f"ALL_ABOVE_{ma_key}_BUT_MORE_RECOVERY_REQUESTED"
            elif count != total and "宽基" in compact and "全部站上" in compact:
                conflict_code = f"NOT_ALL_ABOVE_{ma_key}_BUT_ALL_CLAIMED"
            elif count > 0 and "宽基" in compact and any(term in compact for term in ("无一站上", "均未站上")):
                conflict_code = f"SOME_ABOVE_{ma_key}_BUT_NONE_CLAIMED"
            if conflict_code:
                result["fact_conflicts"].append({
                    "code": conflict_code,
                    "field": count_key,
                    "structured_fact": f"{count}/{total}",
                    "narrative_excerpt": sentence,
                })

    result["status"] = "WARNING" if result["fact_conflicts"] else "PASS"
    result["analysis_safe_to_use"] = not bool(result["fact_conflicts"])
    return result


def numeric_absolute_volume_fields(source: Any) -> set[str]:
    fields = set()
    if isinstance(source, dict):
        for key, value in source.items():
            normalized_key = str(key).lower()
            is_numeric = isinstance(value, (int, float)) and not isinstance(value, bool)
            excluded = any(token in normalized_key for token in (
                "ratio", "_vs_", "dry_up", "activity", "score", "days", "pattern", "confirmation",
            ))
            looks_like_volume = normalized_key == "volume" or "volume" in normalized_key or bool(
                re.fullmatch(r"vol_ma\d+", normalized_key)
            )
            if is_numeric and looks_like_volume and not excluded:
                fields.add(str(key))
            fields.update(numeric_absolute_volume_fields(value))
    elif isinstance(source, list):
        for value in source:
            fields.update(numeric_absolute_volume_fields(value))
    return fields


def source_module_status(contexts: dict, source_files: dict) -> dict:
    return {
        "market": {
            "status": "complete",
            "source_file": source_files["market"],
            "source_date": contexts["market"]["meta"]["run_date"],
        },
        "capital": {
            "status": contexts["capital"]["meta"].get("status") or "unknown",
            "source_file": source_files["capital"],
            "source_date": contexts["capital"]["meta"]["trade_date"],
        },
        "vcp": {
            "status": "complete",
            "source_file": source_files["vcp"],
            "source_date": contexts["vcp"]["meta"]["run_date"],
        },
        "signals": {
            "status": "complete",
            "source_file": source_files["signals"],
            "source_date": contexts["signals"]["meta"]["run_date"],
        },
    }


def build_data_quality(contexts: dict, llm_validation: dict) -> dict:
    market = contexts["market"]
    capital = contexts["capital"]
    vcp = contexts["vcp"]
    signals = contexts["signals"]
    warnings = []
    if (market.get("meta") or {}).get("data_status") != "VALID":
        warnings.append({"module": "market", "detail": (market.get("meta") or {}).get("data_status")})
    for detail in (capital.get("meta") or {}).get("errors") or []:
        warnings.append({"module": "capital", "detail": detail})
    for detail in (signals.get("meta") or {}).get("capital_errors") or []:
        warnings.append({"module": "signals", "detail": detail})
    quant_stats = (vcp.get("summary") or {}).get("quant_stats") or {}
    if int(quant_stats.get("pull_fail") or 0) > 0:
        warnings.append({"module": "vcp", "detail": {"quant_pull_fail": quant_stats["pull_fail"]}})
    if llm_validation.get("status") == "WARNING":
        warnings.append({
            "module": "market_llm_validation",
            "detail": llm_validation.get("fact_conflicts") or [],
        })
    return {
        "status": "complete_with_warnings" if warnings else "complete",
        "warnings": warnings,
        "market_data_status": (market.get("meta") or {}).get("data_status"),
        "capital_status": (capital.get("meta") or {}).get("status"),
        "capital_errors": (capital.get("meta") or {}).get("errors") or [],
        "signal_capital_errors": (signals.get("meta") or {}).get("capital_errors") or [],
        "quant_stats": quant_stats,
    }


def build_report(contexts: dict, source_files: dict, date_yy: str, date_iso: str) -> dict:
    market = contexts["market"]
    capital = contexts["capital"]
    vcp = contexts["vcp"]
    signals = contexts["signals"]
    sector_rankings = build_sector_rankings(market)
    ranking_categories = sector_rankings["categories"]
    ranking_count = sum(len(rows) for rows in ranking_categories.values())
    ranking_stock_count = sum(
        len(row.get("representative_stocks") or [])
        for rows in ranking_categories.values()
        for row in rows
    )
    capital_sectors = capital.get("sectors") or []
    source_vcp_items = vcp.get("candidates") or []
    vcp_items = build_vcp_items(source_vcp_items)
    vcp_by_code = {row.get("code"): row for row in vcp_items}
    signal_items = signals.get("signals") or []
    normalized_signal_items = build_signal_items(signal_items, vcp_by_code, date_iso)
    vcp_summary = build_vcp_summary(vcp.get("summary") or {}, vcp_items)
    llm_validation = market_llm_validation(market)

    report = {
        "schema_version": SCHEMA_VERSION,
        "report_date": date_iso,
        "report_date_yy": date_yy,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "content_index": {
            "sector_ranking_items": ranking_count,
            "sector_representative_stock_entries": ranking_stock_count,
            "capital_sectors": len(capital_sectors),
            "capital_stock_entries": sum(len(row.get("top_stocks") or []) for row in capital_sectors),
            "vcp_structures": len(vcp_items),
            "trading_signals": len(signal_items),
        },
        "workflow_status": source_module_status(contexts, source_files),
        "market_trend": {
            "meta": clean_market_meta(market.get("meta") or {}),
            "state": market.get("market_state") or {},
            "state_explainer": market.get("market_state_explainer") or {},
            "indexes": market.get("indexes") or {},
            "breadth": market.get("breadth") or {},
            "llm_validation": llm_validation,
        },
        "daily_hotspot": market.get("daily_mainline") or {},
        "sector_strength_rankings": sector_rankings,
        "sector_capital": {
            "meta": capital.get("meta") or {},
            "summary": capital.get("summary") or {},
            "thresholds": capital.get("thresholds") or {},
            "sectors": capital_sectors,
        },
        "vcp_structures": {
            "meta": vcp.get("meta") or {},
            "summary": vcp_summary,
            "items": vcp_items,
        },
        "trading_signals": {
            "meta": signals.get("meta") or {},
            "market_notice": signals.get("market_notice") or {},
            "summary": signals.get("summary") or {},
            "items": normalized_signal_items,
        },
        "data_quality": build_data_quality(contexts, llm_validation),
        "field_provenance": {
            "market_trend.state": "deterministic_rule",
            "market_trend.state.analysis": (
                "existing_llm_output"
                if (market.get("market_state") or {}).get("analysis_source") == "llm"
                else "deterministic_rule"
            ),
            "market_trend.llm_validation": "deterministic_rule",
            "daily_hotspot": (
                "existing_llm_output"
                if (market.get("daily_mainline") or {}).get("provider")
                else "deterministic_rule"
            ),
            "sector_strength_rankings": "calculated_metric",
            "sector_capital": "calculated_metric_and_external_market_data",
            "vcp_structures": "deterministic_rule",
            "vcp_structures.items[].sector": "typed_normalization_from_published_vcp_sector_snapshot",
            "vcp_structures.items[].structure_id": "deterministic_identity_from_code_and_structure_anchor",
            "vcp_structures.items[].llm_insight": "existing_llm_output_or_null",
            "trading_signals": "deterministic_rule_and_external_evidence",
            "trading_signals.items[].signal_id": "deterministic_identity_from_code_date_role_and_setup",
            "trading_signals.items[].trade_price_semantics": "normalized_from_published_signal_fields",
        },
        "dictionaries": {
            "source_types": {
                "deterministic_rule": "scripted rule result",
                "calculated_metric": "script-calculated numeric fact",
                "existing_llm_output": "LLM text already present in the published Dashboard source",
                "external_market_data": "external market, capital, financial, or news evidence",
            },
            "units": {
                "*_pct": "percentage points, where 5.2 means 5.2%",
                "*_ratio": "decimal ratio, where 0.052 means 5.2%",
                "*_yuan": "CNY yuan",
                "volume_activity": "dimensionless volume ratio relative to its comparison baseline",
                "volume_dry_up": "dimensionless moving-average volume ratio",
            },
            "units_by_path": {
                "trading_signals.items[]": {
                    "recursive": False,
                    "fields": ["volume", "vol_ma5", "vol_ma20", "vol_ma60"],
                    "unit": "lot",
                    "lot_size_shares": 100,
                },
                "trading_signals.items[].plan_inputs": {
                    "recursive": True,
                    "fields": ABSOLUTE_VOLUME_FIELDS,
                    "unit": "lot",
                    "lot_size_shares": 100,
                },
                "trading_signals.items[].trade_price_semantics.plans.*": {
                    "recursive": True,
                    "fields": ABSOLUTE_VOLUME_FIELDS,
                    "unit": "lot",
                    "lot_size_shares": 100,
                },
                "vcp_structures.items[].contractions[]": {
                    "recursive": False,
                    "fields": ["avg_volume"],
                    "unit": "lot",
                    "lot_size_shares": 100,
                },
                "vcp_structures.items[].contraction_extensions[]": {
                    "recursive": False,
                    "fields": ["avg_volume"],
                    "unit": "lot",
                    "lot_size_shares": 100,
                },
            },
            "canonical_layers": {
                "trading_signals.items[].trade_price_semantics": "Canonical trade-price semantics for AI and downstream decisions.",
                "trading_signals.items[].plan_inputs": "Raw plan payload retained for model provenance; not canonical when semantic fields overlap.",
                "trading_signals.items[].plan_inputs.formula_ref": "Raw formula and Model 2 input provenance; not canonical when semantic fields overlap.",
            },
            "enums": {
                "record_role": {
                    "TODAY_TRIGGER": "A setup actually triggered on report_date.",
                    "NEXT_DAY_PLAN": "A conditional plan prepared on report_date for the next trading session.",
                    "UNKNOWN": "The published signal_kind is not recognized; do not infer an execution role.",
                },
                "signal_kind": {
                    "TRIGGERED": "Today event record.",
                    "PLAN": "Next-session conditional plan record.",
                },
                "setup_signal": {
                    "PULLBACK_BUY": "Low-volume pullback setup inside a valid VCP structure.",
                    "BREAKOUT_BUY": "Price-and-volume breakout through the VCP pivot.",
                    "RETEST_BUY": "Post-breakout low-volume retest and recovery setup.",
                    "NONE": "No actionable setup trigger.",
                },
                "structure_stage": {
                    "VCP_EARLY": "One valid standard contraction; early observation only.",
                    "VCP_FORMING": "At least two standard contractions; structure is forming.",
                    "VCP_MATURE": "At least three generally narrowing standard contractions.",
                    "VCP_TIGHT": "Mature VCP with a tight final contraction near the pivot.",
                    "TREND_WATCH": "Strong trend without a valid standard VCP contraction sequence.",
                    "POST_BREAKOUT": "Old VCP has broken out and is no longer a current pre-breakout structure.",
                    "TREND_REBUILD": "Prior structure needs rebuilding after a deep post-breakout pullback.",
                    "STRUCTURE_INVALID": "The current structure is broken or expired.",
                    "NONE": "No recognized structure.",
                    "DATA_ISSUE": "Insufficient or invalid market data.",
                },
                "bloom_status": {
                    "EARLY": "Early structure, low-priority observation.",
                    "FORMING": "Structure forming, normal observation.",
                    "MATURE": "Mature or tight structure, priority observation.",
                    "TRIGGERED": "Model 2 emitted a PULLBACK, BREAKOUT, or RETEST buy setup.",
                    "RISK_BLOCKED": "Structure exists but current risk blocks participation.",
                    "COOLDOWN": "Temporarily outside active observation while retained in lifecycle state.",
                    "INVALID": "Structure invalid or waiting to rebuild.",
                    "EXIT": "Removed from the Bloom pool.",
                    "DATA_ISSUE": "Data abnormality; long-term state is not advanced.",
                },
                "pool_channel": {
                    "CORE_QUALITY": "Passed the core fundamental quality pool.",
                    "EXPANSION_RS": "Entered through the relative-strength expansion pool.",
                    "BOTH": "Present in both core quality and relative-strength expansion pools.",
                    "UNKNOWN": "Pool source is unavailable.",
                },
                "pool_decision": {
                    "ADD": "Newly added to Bloom.",
                    "KEEP_FOCUS": "Keep as priority observation.",
                    "KEEP_LOW": "Keep as low-priority observation.",
                    "COOLDOWN": "Retain without advancing the setup.",
                    "EXIT": "Remove from Bloom.",
                    "DATA_HOLD": "Hold prior state because of data issues.",
                },
                "position_status": {
                    "ACTIONABLE": "An A/B triggered setup has a non-zero environment-adjusted position range.",
                    "PLAN_CONDITIONAL": "Conditional A/B plan ranges; recalculate if actually triggered.",
                    "OBSERVE_QUALITY": "Setup quality or position mapping is not eligible for sizing.",
                    "OBSERVE_MARKET": "Market state does not open position allocation.",
                    "OBSERVE_SECTOR": "Sector state does not open position allocation.",
                },
                "sector_policy_tier": {
                    "A": "Sector phase is confirmed mainline and data is ready.",
                    "B": "Sector phase is strengthening and data is ready.",
                    "D": "Other phases or sector data is not ready; no stage-based promotion.",
                },
                "count_basis": {
                    "bloom_lifecycle_snapshot_after_state_merge": "Complete mutually exclusive Bloom state after merging prior tracked state with current Quant results, including lifecycle exits for the run.",
                    "vcp_structures.items": "Mutually exclusive statuses of VCP items published in this report.",
                },
            },
            "excluded_sections": [
                "sector_history",
                "sector_rank_matrix",
                "backtest",
                "post_entry_lifecycle",
                "valuation",
                "position",
                "watchlist_sync",
            ],
        },
    }
    return normalize_value(report)


def validate_report(report: dict) -> None:
    expected_sections = (
        "market_trend", "daily_hotspot", "sector_strength_rankings",
        "sector_capital", "vcp_structures", "trading_signals", "data_quality",
    )
    missing = [key for key in expected_sections if key not in report]
    if missing:
        raise ValueError("report sections missing: " + ", ".join(missing))
    if report.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unexpected AI report schema_version")
    index = report["content_index"]
    if index["capital_sectors"] != len(report["sector_capital"]["sectors"]):
        raise ValueError("capital sector count mismatch")
    if index["vcp_structures"] != len(report["vcp_structures"]["items"]):
        raise ValueError("VCP structure count mismatch")
    if index["trading_signals"] != len(report["trading_signals"]["items"]):
        raise ValueError("trading signal count mismatch")
    if "sector_history" in report["sector_strength_rankings"] or "sector_rank_matrix" in report["sector_strength_rankings"]:
        raise ValueError("excluded sector history content present")
    for row in report["vcp_structures"]["items"] + report["trading_signals"]["items"]:
        code = row.get("code")
        if code is not None and (not isinstance(code, str) or not re.fullmatch(r"\d{6}", code)):
            raise ValueError(f"invalid stock code: {code!r}")
    structure_ids = [row.get("structure_id") for row in report["vcp_structures"]["items"] if row.get("structure_id")]
    if len(structure_ids) != len(set(structure_ids)):
        raise ValueError("duplicate VCP structure_id")
    signal_ids = []
    for row in report["trading_signals"]["items"]:
        if "support_price" in row or "invalid_price" in row:
            raise ValueError("unscoped trade price field present in signal")
        semantics = row.get("trade_price_semantics")
        if not isinstance(semantics, dict) or not isinstance(semantics.get("plans"), dict):
            raise ValueError("trade price semantics missing in signal")
        if semantics.get("canonical_for_ai_and_downstream") is not True:
            raise ValueError("trade price semantics must be marked canonical")
        expected_role = signal_record_role(row.get("signal_kind"))
        if row.get("record_role") != expected_role:
            raise ValueError("signal record_role does not match signal_kind")
        signal_id_value = row.get("signal_id")
        if not signal_id_value:
            raise ValueError("signal_id missing")
        signal_ids.append(signal_id_value)
        parent_id = row.get("parent_structure_id")
        if parent_id is not None and not re.fullmatch(r"\d{6}:.+", str(parent_id)):
            raise ValueError(f"invalid parent_structure_id: {parent_id!r}")
    if len(signal_ids) != len(set(signal_ids)):
        raise ValueError("duplicate signal_id")
    status_groups = (report["vcp_structures"]["summary"].get("status_counts") or {}).values()
    for group in status_groups:
        counts = group.get("counts") or {}
        if group.get("mutually_exclusive") is not True:
            raise ValueError("VCP status count basis must be explicitly mutually exclusive")
        if int(group.get("total") or 0) != sum(int(value or 0) for value in counts.values()):
            raise ValueError("VCP status count total mismatch")
    units_by_path = (report.get("dictionaries") or {}).get("units_by_path") or {}
    required_volume_paths = {
        "trading_signals.items[]",
        "trading_signals.items[].plan_inputs",
        "trading_signals.items[].trade_price_semantics.plans.*",
        "vcp_structures.items[].contractions[]",
        "vcp_structures.items[].contraction_extensions[]",
    }
    if not required_volume_paths.issubset(units_by_path):
        raise ValueError("absolute volume unit paths missing")
    for path in required_volume_paths:
        rule = units_by_path[path]
        if rule.get("unit") != "lot" or rule.get("lot_size_shares") != 100:
            raise ValueError(f"invalid absolute volume unit contract: {path}")
    top_volume_fields = set(units_by_path["trading_signals.items[]"]["fields"])
    nested_volume_fields = set(ABSOLUTE_VOLUME_FIELDS)
    for row in report["trading_signals"]["items"]:
        top_detected = {
            key for key, value in row.items()
            if key in numeric_absolute_volume_fields({key: value})
        }
        if not top_detected.issubset(top_volume_fields):
            raise ValueError(f"undocumented top-level signal volume fields: {sorted(top_detected - top_volume_fields)}")
        for nested in (row.get("plan_inputs") or {}, (row.get("trade_price_semantics") or {}).get("plans") or {}):
            detected = numeric_absolute_volume_fields(nested)
            if not detected.issubset(nested_volume_fields):
                raise ValueError(f"undocumented nested signal volume fields: {sorted(detected - nested_volume_fields)}")
    vcp_volume_fields = set()
    for row in report["vcp_structures"]["items"]:
        vcp_volume_fields.update(numeric_absolute_volume_fields(row.get("contractions") or []))
        vcp_volume_fields.update(numeric_absolute_volume_fields(row.get("contraction_extensions") or []))
    if not vcp_volume_fields.issubset({"avg_volume"}):
        raise ValueError(f"undocumented VCP contraction volume fields: {sorted(vcp_volume_fields - {'avg_volume'})}")
    llm_validation = report["market_trend"].get("llm_validation") or {}
    expected_safe = {"PASS": True, "WARNING": False, "NOT_APPLICABLE": None}.get(llm_validation.get("status"))
    if llm_validation.get("analysis_safe_to_use") is not expected_safe:
        raise ValueError("LLM analysis_safe_to_use is inconsistent with validation status")
    serialized = json.dumps(report, ensure_ascii=False, allow_nan=False)
    if re.search(r'/(?:Users|home|var|tmp)/', serialized):
        raise ValueError("local absolute path leaked into report")


def publish(date_value: str, data_dir: Path = DASHBOARD_DATA_DIR, output_root: Path = OUTPUT_ROOT) -> Path:
    date_yy, date_iso = normalize_date(date_value)
    contexts, source_files = load_contexts(date_yy, date_iso, data_dir)
    report = build_report(contexts, source_files, date_yy, date_iso)
    validate_report(report)
    output = output_root / f"20{date_yy[:4]}" / f"huaxin_quant_ai_report_{date_yy}.json"
    serialized = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    atomic_write_text(output, serialized)
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description="生成指定日期的 Huaxin Quant AI 研读 JSON")
    parser.add_argument("--date", required=True, help="报告日期，支持 YYMMDD 或 YYYY-MM-DD")
    args = parser.parse_args()
    try:
        output = publish(args.date)
    except (OSError, ValueError, ReportInputError) as exc:
        print(f"AI 日报生成失败: {exc}", file=sys.stderr)
        return 1
    payload = json.loads(output.read_text(encoding="utf-8"))
    print(json.dumps({"date": payload["report_date"], "output": str(output), "content_index": payload["content_index"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
