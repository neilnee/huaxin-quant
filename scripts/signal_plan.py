#!/usr/bin/env python3
"""
Signal Plan: next-session concrete setup plans for mature Model 2 VCP signals.

This model-4 signal layer consumes cache/quant_runs/quant_<YYMMDD>.json and
writes signal_plan/signal_plan_<YYMMDD>.json plus Markdown. It does not update
Bloom state, position ledgers, or Model 2 decisions.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config
from scripts.progress_utils import ProgressTracker


STRATEGY_FILE = "04-signal-plan.json"
CONFIG, STRATEGY_PATH = load_strategy_config(STRATEGY_FILE)
STRATEGY_VERSION = CONFIG["strategy_version"]

QUANT_RUNS_DIR = Path(PROJECT_ROOT) / CONFIG["inputs"]["quant_run_dir"]
PLAN_DIR = Path(PROJECT_ROOT) / CONFIG["outputs"]["plan_dir"]


def normalize_date_arg(value):
    if not value:
        return None
    raw = value.strip()
    if re.fullmatch(r"\d{6}", raw):
        return raw
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%y%m%d")
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def latest_quant_run():
    files = sorted(QUANT_RUNS_DIR.glob("quant_*.json"))
    return files[-1] if files else None


def quant_run_for_date(date_yy):
    path = QUANT_RUNS_DIR / f"quant_{date_yy}.json"
    return path if path.exists() else None


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def safe_float(value, default=None):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if value in ("True", "true", "TRUE", "1", 1):
        return True
    if value in ("False", "false", "FALSE", "0", 0):
        return False
    return None


def as_list(value):
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str) and ";" in value:
        return [item for item in value.split(";") if item]
    return [str(value)]


def round_price(value):
    value = safe_float(value)
    return None if value is None else round(value, 2)


def round_volume(value):
    value = safe_float(value)
    return None if value is None else int(round(value))


def fmt_price(value):
    value = safe_float(value)
    if value is None:
        return "-"
    return f"{value:.2f}"


def fmt_price_range(low, high):
    if low is None and high is None:
        return "-"
    if low is None:
        return f"{fmt_price(high)}以下"
    if high is None:
        return f"{fmt_price(low)}以上"
    return f"{fmt_price(low)}-{fmt_price(high)}"


def fmt_volume_value(value):
    value = safe_float(value)
    if value is None:
        return "-"
    wan = value / 10000.0
    if wan >= 100:
        return f"{wan:.0f}万手"
    if wan >= 10:
        return f"{wan:.1f}万手"
    return f"{wan:.2f}万手"


def fmt_volume_range(low, high):
    if low in (None, 0) and high is None:
        return "-"
    if low in (None, 0):
        return f"{fmt_volume_value(high)}以下"
    if high is None:
        return f"{fmt_volume_value(low)}以上"
    return f"{fmt_volume_value(low)}-{fmt_volume_value(high)}"


def fmt_a_price_range(row):
    if row.get("target_quality") != "A":
        return "-"
    return fmt_price_range(row.get("ideal_price_low"), row.get("ideal_price_high"))


def fmt_a_volume_range(row):
    if row.get("target_quality") != "A":
        return "-"
    return fmt_volume_range(row.get("ideal_volume_min"), row.get("ideal_volume_max"))


def fmt_quality_label(value):
    value = str(value or "").strip()
    return f"{value}级" if value in {"A", "B", "C"} else value


def _load_dotenv():
    """Load local .env values without overriding existing environment variables."""
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


def required_fields_missing(row, fields):
    missing = []
    for field in fields:
        if safe_float(row.get(field)) is None:
            missing.append(field)
    return missing


def risk_flags(row):
    return [str(flag) for flag in as_list(row.get("structure_risk_flags")) if str(flag)]


def risk_blocked(row):
    rules = CONFIG["candidate_rules"]
    risk_score = safe_float(row.get("structure_risk_score"), 0.0)
    flags = set(risk_flags(row))
    hard = flags.intersection(rules["hard_risk_flags"])
    return risk_score >= rules["risk_block_min_score"] or bool(hard)


def target_quality(row):
    score = safe_float(row.get("structure_score"), 0.0)
    risk = safe_float(row.get("structure_risk_score"), 0.0)
    rules = CONFIG["quality_rules"]
    if score >= rules["A"]["min_structure_score"] and risk <= rules["A"]["max_risk_score"]:
        return "A"
    if score >= rules["B"]["min_structure_score"] and risk <= rules["B"]["max_risk_score"]:
        return "B"
    return "C"


def priority_for_quality(quality):
    if quality == "A":
        return "HIGH"
    if quality == "B":
        return "MEDIUM"
    return "LOW"


def max_not_none(*values):
    nums = [safe_float(v) for v in values if safe_float(v) is not None]
    return max(nums) if nums else None


def min_not_none(*values):
    nums = [safe_float(v) for v in values if safe_float(v) is not None]
    return min(nums) if nums else None


def choose_support_anchor(row, cfg):
    for field in cfg["support_anchor_order"]:
        value = safe_float(row.get(field))
        if value is not None and value > 0:
            return field, value
    return None, None


def setup_plan_inputs(row, family):
    inputs = row.get("setup_plan_inputs") or {}
    value = inputs.get(family.lower()) or {}
    return value if isinstance(value, dict) else {}


def post_breakout_state(row):
    """Return the Model 2 lifecycle state, preserving compatibility with v9 runs."""
    return str(row.get("post_breakout_state") or "PRE_BREAKOUT").strip().upper()


def lifecycle_plan_permission(row):
    """State which plan families may use this VCP structure.

    A VCP pivot is only actionable as PULLBACK/BREAKOUT before its first valid
    breakout.  Afterwards, the old contraction cannot be reused as a new
    setup: a qualifying retest may only produce RETEST plans; all other
    post-breakout states remain observation or rebuild states.
    """
    state = post_breakout_state(row)
    if state == "PRE_BREAKOUT":
        return {"PULLBACK", "BREAKOUT"}, ""
    if state == "POST_BREAKOUT_RETEST":
        return {"RETEST"}, ""
    if state == "POST_BREAKOUT_HOT":
        return set(), "突破后延伸中，不生成追高计划"
    if state == "POST_BREAKOUT_CONSOLIDATING":
        return set(), "突破后整理中，旧 VCP 不生成买点计划"
    if state in {"POST_BREAKOUT_FAILED", "POST_BREAKOUT_EXPIRED"}:
        return set(), "突破后结构已失效，等待重新构建"
    return set(), f"未知突破后状态 {state}，不生成计划"


def base_plan(row, setup_family, plan_action, setup_type, quality):
    setup_signal = f"{setup_family}_BUY" if setup_family in {"PULLBACK", "BREAKOUT", "RETEST"} else setup_type
    return {
        "code": str(row.get("code", "")),
        "name": row.get("name", ""),
        "setup_family": setup_family,
        "plan_action": plan_action,
        "plan_type": f"{plan_action}_SETUP_PLAN",
        "setup_type": setup_type,
        "setup_signal": setup_signal,
        "target_quality": quality,
        "plan_priority": priority_for_quality(quality),
        "model2_stage": row.get("structure_stage", ""),
        "model2_setup_signal": row.get("setup_signal", ""),
        "post_breakout_state": post_breakout_state(row),
        "model2_action_hint": row.get("action_hint", ""),
        "suggested_position": row.get("suggested_position", ""),
        "structure_score": safe_float(row.get("structure_score"), 0.0),
        "structure_risk_score": safe_float(row.get("structure_risk_score"), 0.0),
        "structure_risk_flags": risk_flags(row),
        "close": round_price(row.get("close")),
        "volume": round_volume(row.get("volume")),
        "vol_ma5": round_volume(row.get("vol_ma5")),
        "vol_ma20": round_volume(row.get("vol_ma20")),
        "trigger_price_low": None,
        "trigger_price_high": None,
        "ideal_price_low": None,
        "ideal_price_high": None,
        "volume_min": None,
        "volume_max": None,
        "ideal_volume_min": None,
        "ideal_volume_max": None,
        "invalid_price": None,
        "formula_ref": {},
        "plan_reason": "",
        "risk_note": "",
        "strategy_version": STRATEGY_VERSION,
    }


def build_breakout_plan(row, follow=False):
    cfg = CONFIG["breakout_follow"] if follow else CONFIG["breakout_buy"]
    inputs = setup_plan_inputs(row, "breakout")
    pivot = safe_float(row.get("structure_pivot") or row.get("pivot_price"))
    vol_ma5 = safe_float(row.get("vol_ma5"))
    vol_ma20 = safe_float(row.get("vol_ma20"))
    volume_thresholds = []
    if vol_ma20 and vol_ma20 > 0:
        volume_thresholds.append(vol_ma20 * cfg["volume_ma20_ratio"])
    if vol_ma5 and vol_ma5 > 0:
        volume_thresholds.append(vol_ma5 * cfg["volume_ma5_ratio"])
    volume_min = min_not_none(*volume_thresholds)
    quality = target_quality(row)
    plan = base_plan(
        row,
        "BREAKOUT",
        "FOLLOW" if follow else "NEW",
        "BREAKOUT_FOLLOW" if follow else "BREAKOUT_BUY",
        quality,
    )
    plan.update({
        "trigger_price_low": round_price(
            inputs.get("trigger_price")
            or pivot * cfg.get("trigger_low_ratio", cfg.get("close_buffer_ratio", 1.01))
        ),
        "trigger_price_high": round_price(
            inputs.get("max_price")
            or pivot * cfg.get("trigger_high_ratio", cfg.get("max_close_extension_ratio", 1.08))
        ),
        "ideal_price_low": round_price(inputs.get("ideal_price_low") or pivot * cfg["ideal_low_ratio"]),
        "ideal_price_high": round_price(inputs.get("ideal_price_high") or pivot * cfg["ideal_high_ratio"]),
        "volume_min": round_volume(inputs.get("volume_min") or volume_min),
        "ideal_volume_min": round_volume(inputs.get("ideal_volume_min") or vol_ma20 * cfg["ideal_volume_min_ratio"]),
        "invalid_price": round_price(inputs.get("invalid_price") or max_not_none(row.get("invalid_price"), pivot * cfg["invalid_pivot_ratio"])),
        "formula_ref": {
            "source": "model2.setup_plan_inputs.breakout" if inputs else "signal_plan_fallback",
            "model2_plan_inputs": inputs,
            "pivot": round_price(pivot),
            "vol_ma5": round_volume(vol_ma5),
            "vol_ma20": round_volume(vol_ma20),
            "volume_min_uses_model2_or_rule": "min(vol_ma20 * volume_ma20_ratio, vol_ma5 * volume_ma5_ratio)",
            "config": cfg,
        },
        "plan_reason": "突破延续参与区间" if follow else "成熟 VCP 枢轴突破计划",
        "risk_note": "超过触发上沿则不属于本类买点计划",
    })
    return plan


def build_pullback_plan(row, follow=False):
    cfg = CONFIG["pullback_follow"] if follow else CONFIG["pullback_buy"]
    inputs = setup_plan_inputs(row, "pullback")
    anchor_name, anchor = choose_support_anchor(row, CONFIG["pullback_buy"])
    if inputs.get("anchor"):
        anchor_name = inputs.get("anchor")
    if safe_float(inputs.get("support_price")) is not None:
        anchor = safe_float(inputs.get("support_price"))
    vol_ma20 = safe_float(row.get("vol_ma20"))
    quality = target_quality(row)
    trigger_low = anchor * cfg["trigger_low_ratio"]
    if anchor_name == "MA20":
        trigger_low = safe_float(inputs.get("ma20_price_low"), trigger_low)
        trigger_high = safe_float(inputs.get("ma20_price_high"), anchor * cfg["trigger_high_ratio"])
    elif anchor_name == "MA60":
        trigger_low = safe_float(inputs.get("ma60_price_low"), trigger_low)
        trigger_high = safe_float(inputs.get("ma60_price_high"), anchor * cfg["trigger_high_ratio"])
    else:
        trigger_high = anchor * cfg["trigger_high_ratio"]
    invalid_candidates = [
        safe_float(row.get("last_contraction_low")) * cfg["invalid_low_ratio"]
        if safe_float(row.get("last_contraction_low")) is not None else None,
        anchor * cfg["invalid_anchor_ratio"],
        inputs.get("invalid_price"),
        row.get("invalid_price"),
    ]
    invalid = max_not_none(
        *[
            value for value in invalid_candidates
            if safe_float(value) is not None and safe_float(value) < trigger_low
        ]
    )
    plan = base_plan(
        row,
        "PULLBACK",
        "FOLLOW" if follow else "NEW",
        "PULLBACK_FOLLOW" if follow else "PULLBACK_BUY",
        quality,
    )
    plan.update({
        "trigger_price_low": round_price(trigger_low),
        "trigger_price_high": round_price(trigger_high),
        "ideal_price_low": round_price(anchor * cfg["ideal_low_ratio"]),
        "ideal_price_high": round_price(anchor * cfg["ideal_high_ratio"]),
        "volume_max": round_volume(inputs.get("volume_floor_threshold") or vol_ma20 * cfg["volume_max_ratio"]),
        "ideal_volume_max": round_volume(inputs.get("ideal_volume_max") or vol_ma20 * cfg["ideal_volume_max_ratio"]),
        "invalid_price": round_price(invalid),
        "formula_ref": {
            "source": "model2.setup_plan_inputs.pullback" if inputs else "signal_plan_fallback",
            "model2_plan_inputs": inputs,
            "support_anchor": anchor_name,
            "support_anchor_price": round_price(anchor),
            "vol_ma20": round_volume(vol_ma20),
            "last_contraction_low": round_price(row.get("last_contraction_low")),
            "config": cfg,
        },
        "plan_reason": "回踩买点延续有效区" if follow else "成熟 VCP 结构内缩量回踩计划",
        "risk_note": "跌破失效价则不按当前回踩计划处理",
    })
    return plan


def build_retest_plan(row, follow=False):
    cfg = CONFIG["retest_follow"] if follow else CONFIG["retest_buy"]
    inputs = setup_plan_inputs(row, "retest")
    pivot = safe_float(row.get("structure_pivot") or row.get("breakout_level") or row.get("pivot_price"))
    if safe_float(inputs.get("recent_breakout_level")) is not None:
        pivot = safe_float(inputs.get("recent_breakout_level"))
    current_volume = safe_float(row.get("volume"))
    quality = target_quality(row)
    plan = base_plan(
        row,
        "RETEST",
        "FOLLOW" if follow else "NEW",
        "RETEST_FOLLOW" if follow else "RETEST_BUY",
        quality,
    )
    if follow:
        volume_max_ratio = cfg["volume_max_ratio"]
        ideal_volume_max_ratio = cfg["ideal_volume_max_ratio"]
    else:
        volume_max_ratio = cfg["pullback_volume_max_ratio"]
        ideal_volume_max_ratio = cfg["ideal_pullback_volume_max_ratio"]
    plan.update({
        "trigger_price_low": round_price(inputs.get("price_low") or pivot * cfg["trigger_low_ratio"]),
        "trigger_price_high": round_price(inputs.get("price_high") or pivot * cfg["trigger_high_ratio"]),
        "ideal_price_low": round_price(inputs.get("ideal_price_low") or pivot * cfg["ideal_low_ratio"]),
        "ideal_price_high": round_price(inputs.get("ideal_price_high") or pivot * cfg["ideal_high_ratio"]),
        "volume_max": round_volume(inputs.get("volume_threshold") or current_volume * volume_max_ratio),
        "ideal_volume_max": round_volume(inputs.get("ideal_volume_max") or current_volume * ideal_volume_max_ratio),
        "invalid_price": round_price(inputs.get("invalid_price") or max_not_none(row.get("invalid_price"), pivot * cfg["invalid_pivot_ratio"])),
        "formula_ref": {
            "source": "model2.setup_plan_inputs.retest" if inputs else "signal_plan_fallback",
            "model2_plan_inputs": inputs,
            "pivot": round_price(pivot),
            "reference_volume": round_volume(current_volume),
            "config": cfg,
        },
        "plan_reason": "回踩确认买点延续有效区" if follow else "突破后回踩确认计划",
        "risk_note": "重新跌回失效价以下则回踩确认失败",
    })
    return plan


def valid_candidate(row):
    rules = CONFIG["candidate_rules"]
    if not normalize_bool(row.get("model2_include")):
        return False, "model2_include=false"
    if row.get("structure_type") != "VCP":
        return False, "structure_type 不是 VCP"
    if not normalize_bool(row.get("structure_valid")):
        return False, "structure_valid=false"
    if risk_blocked(row):
        return False, "风险阻断"

    allowed_families, lifecycle_reason = lifecycle_plan_permission(row)
    if not allowed_families:
        return False, lifecycle_reason

    stage = row.get("structure_stage")
    signal = row.get("setup_signal")
    is_new_stage = stage in rules["allowed_new_stages"]
    is_follow_signal = signal in rules["allowed_follow_signals"]
    if not is_new_stage and not is_follow_signal:
        return False, "不是成熟结构，也不是当日买点触发"

    score = safe_float(row.get("structure_score"), 0.0)
    if score < rules["min_structure_score"]:
        return False, "结构分低于 Signal Plan 门槛"

    missing = required_fields_missing(row, ["close", "volume", "vol_ma20"])
    if missing:
        return False, f"缺少量能/价格字段: {','.join(missing)}"
    if is_new_stage or signal in {"BREAKOUT_BUY", "RETEST_BUY"}:
        missing = required_fields_missing(row, ["structure_pivot"])
        if missing:
            return False, "缺少 structure_pivot"
    if signal == "PULLBACK_BUY" or is_new_stage:
        anchor_name, _ = choose_support_anchor(row, CONFIG["pullback_buy"])
        if not anchor_name:
            return False, "缺少回踩支撑锚点"
    return True, ""


def plans_for_row(row):
    stage = row.get("structure_stage")
    signal = row.get("setup_signal")
    close = safe_float(row.get("close"))
    pivot = safe_float(row.get("structure_pivot") or row.get("pivot_price"))
    plans = []
    allowed_families, _ = lifecycle_plan_permission(row)

    # A lifecycle retest state is only an observation window, not a tradable
    # setup.  Requiring Model 2's confirmed RETEST_BUY prevents volatile
    # post-limit-up pullbacks from being promoted to a next-session plan.
    # PULLBACK/BREAKOUT remain permanently unavailable after the breakout.
    if allowed_families == {"RETEST"}:
        if signal == "RETEST_BUY":
            plans.append(build_retest_plan(row, follow=True))
        return plans

    if signal == "PULLBACK_BUY" and "PULLBACK" in allowed_families:
        plans.append(build_pullback_plan(row, follow=True))
        if (
            stage in CONFIG["candidate_rules"]["allowed_new_stages"]
            and pivot and close
            and close < pivot * CONFIG["breakout_buy"]["close_buffer_ratio"]
        ):
            if "BREAKOUT" in allowed_families:
                plans.append(build_breakout_plan(row, follow=False))
        return plans

    if signal == "BREAKOUT_BUY" and "BREAKOUT" in allowed_families:
        plans.append(build_breakout_plan(row, follow=True))
        return plans

    if stage in CONFIG["candidate_rules"]["allowed_new_stages"] and allowed_families == {"PULLBACK", "BREAKOUT"}:
        if pivot and close and close > pivot * CONFIG["breakout_buy"]["overextended_ratio"]:
            return plans
        plans.append(build_pullback_plan(row, follow=False))
        plans.append(build_breakout_plan(row, follow=False))
        return plans

    return plans


def valid_plan_exclusion(row):
    stage = row.get("structure_stage")
    signal = row.get("setup_signal")
    close = safe_float(row.get("close"))
    pivot = safe_float(row.get("structure_pivot") or row.get("pivot_price"))
    if post_breakout_state(row) == "POST_BREAKOUT_RETEST" and signal != "RETEST_BUY":
        return "突破后回踩尚未获模型二确认，不生成 RETEST 计划"
    if (
        stage in CONFIG["candidate_rules"]["allowed_new_stages"]
        and signal not in CONFIG["candidate_rules"]["allowed_follow_signals"]
        and pivot and close
        and close > pivot * CONFIG["breakout_buy"]["overextended_ratio"]
    ):
        return "价格超过突破计划上沿，Signal Plan 不追高"
    return ""


def sort_key(plan):
    quality_rank = {"A": 0, "B": 1, "C": 2}
    family_rank = {"PULLBACK": 0, "BREAKOUT": 1, "RETEST": 2}
    action_rank = {"NEW": 0, "FOLLOW": 1}
    return (
        family_rank.get(plan.get("setup_family"), 9),
        quality_rank.get(plan.get("target_quality"), 9),
        action_rank.get(plan.get("plan_action"), 9),
        -safe_float(plan.get("structure_score"), 0.0),
        plan.get("code", ""),
    )


def build_signal_plan(payload, date_yy, progress_file=None):
    meta = payload.get("meta", {})
    run_date = meta.get("run_date") or datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")
    results = payload.get("results", [])
    total_results = len(results)
    plans = []
    excluded = []
    data_issues = []

    for idx, row in enumerate(results):
        ok, reason = valid_candidate(row)
        if not ok:
            item = {
                "code": str(row.get("code", "")),
                "name": row.get("name", ""),
                "structure_stage": row.get("structure_stage", ""),
                "setup_signal": row.get("setup_signal", ""),
                "reason": reason,
            }
            if reason.startswith("缺少"):
                data_issues.append(item)
            else:
                excluded.append(item)
            continue
        row_plans = plans_for_row(row)
        if not row_plans:
            reason = valid_plan_exclusion(row)
            if reason:
                excluded.append({
                    "code": str(row.get("code", "")),
                    "name": row.get("name", ""),
                    "structure_stage": row.get("structure_stage", ""),
                    "setup_signal": row.get("setup_signal", ""),
                    "reason": reason,
                    "close": round_price(row.get("close")),
                    "structure_pivot": round_price(row.get("structure_pivot") or row.get("pivot_price")),
                })
        plans.extend(row_plans)

        # Progress: every 10 stocks or at boundaries
        if progress_file and (idx % 10 == 0 or idx == total_results - 1):
            try:
                pt = ProgressTracker(progress_file)
                pt.step_update("plan", current_stage="筛选候选+生成计划",
                               total=total_results, completed=idx + 1,
                               current_code=str(row.get("code", "")))
            except Exception:
                pass

    plans.sort(key=sort_key)
    families = ["PULLBACK", "BREAKOUT", "RETEST"]
    by_family = {family: [p for p in plans if p.get("setup_family") == family] for family in families}

    def family_stats(rows):
        codes = {r.get("code") for r in rows if r.get("code")}
        return {
            "plan_total": len(rows),
            "stock_total": len(codes),
            "A": sum(1 for r in rows if r.get("target_quality") == "A"),
            "B": sum(1 for r in rows if r.get("target_quality") == "B"),
            "C": sum(1 for r in rows if r.get("target_quality") == "C"),
            "NEW": sum(1 for r in rows if r.get("plan_action") == "NEW"),
            "FOLLOW": sum(1 for r in rows if r.get("plan_action") == "FOLLOW"),
        }

    by_family_stats = {family: family_stats(rows) for family, rows in by_family.items()}
    unique_codes = {p.get("code") for p in plans if p.get("code")}

    summary = {
        "date": run_date,
        "date_yy": date_yy,
        "input_total": meta.get("total"),
        "result_total": len(results),
        "plan_total": len(plans),
        "stock_total": len(unique_codes),
        "new_total": sum(1 for p in plans if p.get("plan_action") == "NEW"),
        "follow_total": sum(1 for p in plans if p.get("plan_action") == "FOLLOW"),
        "quality_dist": {
            "A": sum(1 for p in plans if p.get("target_quality") == "A"),
            "B": sum(1 for p in plans if p.get("target_quality") == "B"),
            "C": sum(1 for p in plans if p.get("target_quality") == "C"),
        },
        "by_setup_family": by_family_stats,
        "data_issue_total": len(data_issues),
        "excluded_total": len(excluded),
        "overextended_total": sum(1 for item in excluded if "超过突破计划上沿" in item.get("reason", "")),
        "strategy_version": STRATEGY_VERSION,
        "strategy_file": STRATEGY_PATH,
        "quant_strategy_version": meta.get("strategy_version", ""),
    }

    return {
        "meta": {
            "schema": "huaxin_signal_plan_v1",
            "date": run_date,
            "date_yy": date_yy,
            "quant_run": str(QUANT_RUNS_DIR / f"quant_{date_yy}.json"),
            "strategy_version": STRATEGY_VERSION,
            "strategy_file": STRATEGY_PATH,
        },
        "summary": summary,
        "sections": {
            "by_family": by_family,
            "data_issues": data_issues,
            "excluded": excluded,
        },
        "plans": plans,
    }


def write_json_plan(date_yy, plan):
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAN_DIR / f"signal_plan_{date_yy}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
    return path


def llm_context_for_plan(plan):
    volume_text = fmt_volume_range(plan.get("volume_min"), plan.get("volume_max"))
    price_text = fmt_price_range(plan.get("trigger_price_low"), plan.get("trigger_price_high"))
    a_class_available = plan.get("target_quality") == "A"
    ideal_volume_text = fmt_a_volume_range(plan)
    ideal_price_text = fmt_a_price_range(plan)
    formula_ref = plan.get("formula_ref") or {}
    model2_inputs = formula_ref.get("model2_plan_inputs") or {}
    return {
        "code": plan.get("code", ""),
        "name": plan.get("name", ""),
        "setup_family": plan.get("setup_family", ""),
        "plan_action": plan.get("plan_action", ""),
        "target_quality": plan.get("target_quality", ""),
        "model2_stage": plan.get("model2_stage", ""),
        "model2_setup_signal": plan.get("model2_setup_signal", ""),
        "structure_score": plan.get("structure_score", ""),
        "structure_risk_score": plan.get("structure_risk_score", ""),
        "close": plan.get("close", ""),
        "volume": fmt_volume_value(plan.get("volume")),
        "a_class_available": a_class_available,
        "trigger_price_range": price_text,
        "ideal_price_range": ideal_price_text,
        "volume_requirement": volume_text,
        "ideal_volume_requirement": ideal_volume_text,
        "invalid_price": fmt_price(plan.get("invalid_price")),
        "model2_threshold_source": formula_ref.get("source", ""),
        "model2_plan_inputs": model2_inputs if a_class_available else {},
    }


def plan_key(plan):
    return f"{plan.get('code')}|{plan.get('setup_family')}|{plan.get('plan_action')}"


def deterministic_note_for_plan(plan):
    family = plan.get("setup_family", "")
    action = "延续" if plan.get("plan_action") == "FOLLOW" else "触发"
    trigger_price = fmt_price_range(plan.get("trigger_price_low"), plan.get("trigger_price_high"))
    volume_req = fmt_volume_range(plan.get("volume_min"), plan.get("volume_max"))
    invalid = fmt_price(plan.get("invalid_price"))
    formula_ref = plan.get("formula_ref") or {}
    anchor = formula_ref.get("support_anchor") or "结构支撑"
    pivot = formula_ref.get("pivot")

    if family == "BREAKOUT":
        pivot_text = f"pivot {fmt_price(pivot)}" if pivot else "突破位"
        note = f"普通买点触发价{trigger_price}对应{pivot_text}上方{action}，量能需{volume_req}，失效价{invalid}控制跌回突破位风险"
    elif family == "PULLBACK":
        note = f"普通买点触发价{trigger_price}对应{anchor}附近缩量回踩{action}，量能需{volume_req}，失效价{invalid}控制破位风险"
    elif family == "RETEST":
        note = f"普通买点触发价{trigger_price}对应突破后回踩确认{action}，量能需{volume_req}，失效价{invalid}控制跌回结构风险"
    else:
        note = f"普通买点触发价{trigger_price}，量能需{volume_req}，失效价{invalid}"

    if plan.get("target_quality") == "A":
        a_price = fmt_a_price_range(plan)
        a_volume = fmt_a_volume_range(plan)
        if a_price != "-" and a_volume != "-":
            note += f"；A类区间为{a_price}且量能{a_volume}"
    return note


def usable_llm_note(plan, note):
    note = str(note or "")
    if not note:
        return False
    forbidden = ["买入", "止损", "离场", "仓位", "建议"]
    if any(word in note for word in forbidden):
        return False
    price_tokens = [
        fmt_price(plan.get("trigger_price_low")),
        fmt_price(plan.get("trigger_price_high")),
    ]
    has_price = any(token != "-" and token in note for token in price_tokens)
    has_volume = "万手" in note or "量能" in note or "成交量" in note
    return has_price and has_volume


def chunks(items, size):
    size = max(1, int(size or 1))
    for idx in range(0, len(items), size):
        yield items[idx:idx + size]


def call_llm_notes(plans, progress_file=None):
    """Generate concise explanations for each executable plan.

    Returns (notes_by_key, status). Missing or failed LLM calls are non-fatal;
    Markdown falls back to deterministic note generation.
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return {}, {
            "status": "skipped",
            "reason": "DEEPSEEK_API_KEY missing",
            "requested": len(plans),
            "returned": 0,
        }

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    status = {
        "status": "pending",
        "provider": "deepseek",
        "model": model,
        "base_url": base_url,
        "requested": len(plans),
        "returned": 0,
        "batches": 0,
    }
    system_prompt = (
        "你是A股VCP买点计划解释助手。你的任务是解释 Signal Plan 已经算出的量价区间，"
        "不能改动区间，不能提出新的价格或成交量，不能引入外部事实。\n"
        "请分别用一句中文说明这个计划的价格锚点、量能要求和失效价依据。"
        "若 a_class_available=false，不要提 A 类区间，只解释普通买点触发区和失效价。"
        "每条说明要求35-60字，直接、具体，不要分项，不要使用冒号，提到关键锚点（如MA20/MA60/pivot/突破位）和量能条件。"
        "不要使用买入、止损、离场、建议、仓位等最终交易措辞；只能说进入触发范围、计划失效或风险增大。"
        "必须返回合法 JSON：{\"notes\":[{\"key\":\"传入key\",\"note\":\"说明\"}]}，key 必须原样保留，note 中不要使用英文双引号。"
    )

    notes = {}
    errors = []
    batch_size = int(CONFIG["reporting"].get("llm_batch_size", 20))
    base_max_tokens = int(CONFIG["reporting"].get("llm_max_tokens", 2000))
    total_batches = (len(plans) + batch_size - 1) // batch_size if plans else 0
    for batch_idx, batch in enumerate(chunks(plans, batch_size)):
        status["batches"] += 1

        # Progress
        if progress_file:
            try:
                first_plan = batch[0] if batch else {}
                pt = ProgressTracker(progress_file)
                pt.step_update("plan", current_stage="LLM备注",
                               total=total_batches, completed=batch_idx + 1,
                               current_code=first_plan.get("code", "") if first_plan else "")
            except Exception:
                pass
        payload = [
            {
                "key": plan_key(plan),
                "plan": llm_context_for_plan(plan),
            }
            for plan in batch
        ]
        requested_keys = [item["key"] for item in payload]
        last_exc = None
        for _attempt in range(2):
            try:
                max_tokens = max(base_max_tokens, min(4000, 90 * len(batch)))
                resp = requests.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {
                                "role": "user",
                                "content": "解释以下计划，只返回合法 JSON：\n"
                                + json.dumps({"plans": payload}, ensure_ascii=False),
                            },
                        ],
                        "response_format": {"type": "json_object"},
                        "stream": False,
                        "temperature": 0.2,
                        "max_tokens": max_tokens,
                    },
                    timeout=60,
                )
                resp.raise_for_status()
                data = resp.json()
                content = data["choices"][0]["message"].get("content", "")
                parsed = parse_json_object_text(content)
                batch_notes = parsed.get("notes") or {}
                if isinstance(batch_notes, list):
                    note_map = {
                        str(item.get("key", "")).strip(): str(item.get("note", "")).strip()
                        for item in batch_notes
                        if isinstance(item, dict)
                    }
                elif isinstance(batch_notes, dict):
                    note_map = {
                        str(key).strip(): str(value).strip()
                        for key, value in batch_notes.items()
                    }
                else:
                    raise ValueError("notes is not an array or object")
                for key in requested_keys:
                    note = note_map.get(key, "")
                    if note:
                        notes[key] = note
                missing = [key for key in requested_keys if key not in notes]
                if missing:
                    raise ValueError("missing notes: " + ",".join(missing[:5]))
                if all(key in notes for key in requested_keys):
                    last_exc = None
                    break
            except Exception as exc:
                last_exc = exc
        if last_exc is not None:
            errors.append(f"batch({','.join(requested_keys[:3])}): {type(last_exc).__name__}: {str(last_exc)[:160]}")

    status["returned"] = len(notes)
    if len(notes) == len(plans):
        status["status"] = "success"
    elif notes:
        status["status"] = "partial"
        status["reason"] = f"returned {len(notes)} of {len(plans)} notes; " + " | ".join(errors[:3])
    else:
        status["status"] = "failed"
        status["reason"] = " | ".join(errors[:5]) if errors else "no notes returned"
    return notes, status


def parse_json_object_text(content):
    content = str(content or "").strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*", "", content)
        content = re.sub(r"\s*```$", "", content).strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end > start:
            return json.loads(content[start:end + 1])
        raise


def attach_llm_notes(plan, progress_file=None):
    notes, status = call_llm_notes(plan.get("plans", []), progress_file=progress_file)
    for item in plan.get("plans", []):
        note = notes.get(plan_key(item))
        item["llm_note"] = note if usable_llm_note(item, note) else deterministic_note_for_plan(item)
    for rows in plan.get("sections", {}).get("by_family", {}).values():
        for item in rows:
            note = notes.get(plan_key(item))
            item["llm_note"] = note if usable_llm_note(item, note) else deterministic_note_for_plan(item)
    plan["summary"]["llm"] = status
    return plan


def plan_table(rows, limit=None):
    if limit is not None:
        rows = rows[:limit]
    lines = [
        "| 代码 | 名称 | 动作 | 最高 | 触发价 | A级价 | 触发量 | A级量 | 失效 | 说明 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            "| {code} | {name} | {action} | {quality} | {trigger_price} | {ideal_price} | "
            "{volume} | {ideal_volume} | {invalid} | {note} |".format(
                code=r.get("code", ""),
                name=r.get("name", ""),
                action=r.get("plan_action", ""),
                quality=fmt_quality_label(r.get("target_quality", "")),
                trigger_price=fmt_price_range(r.get("trigger_price_low"), r.get("trigger_price_high")),
                ideal_price=fmt_a_price_range(r),
                volume=fmt_volume_range(r.get("volume_min"), r.get("volume_max")),
                ideal_volume=fmt_a_volume_range(r),
                invalid=fmt_price(r.get("invalid_price")),
                note=r.get("llm_note") or deterministic_note_for_plan(r),
            )
        )
    return lines


def family_title(family):
    titles = {
        "PULLBACK": "PULLBACK 缩量回踩",
        "BREAKOUT": "BREAKOUT 枢轴突破",
        "RETEST": "RETEST 突破回踩确认",
    }
    return titles.get(family, family)


def field_notes():
    return [
        "## 字段说明",
        "",
        "- `动作`: `NEW` 表示次日可能首次触发该类买点；`FOLLOW` 表示今日已触发同类买点，次日仍在有效区间时可延续观察。",
        "- `最高`: Signal Plan 依据模型二结构分、风险分和买点阈值给出的该计划最高潜在等级；只有最高为 A级 的计划才展示 A 类量价区。",
        "- `触发价`: 次日收盘价落入该区间，表示进入模型二同类普通买点的价位触发范围，不等同于 A 类买点。",
        "- `A级价`: 普通触发区内更严格的 A 类价位区间；最高不足 A级 时不展示。",
        "- `触发量`: 次日成交量需要满足的普通买点最低放量或最高缩量条件；“以上”多用于突破，“以下”多用于回踩。",
        "- `A级量`: A 类买点需要满足的更严格量能条件；最高不足 A级 时不展示。",
        "- `失效`: 跌破后不再按当前买点计划处理。",
        "- `说明`: LLM 基于模型二阈值生成的量价区间说明；调用失败时回退为确定性量价说明，具体公式来源保留在 JSON 的 `formula_ref` 中。",
    ]


def build_markdown(plan):
    summary = plan["summary"]
    sections = plan["sections"]
    reporting = CONFIG["reporting"]
    lines = [
        f"# Signal Plan {summary['date']}",
        "",
        f"- 输入结果: {summary['result_total']} 只",
        f"- 计划总数: {summary['plan_total']} 条 / {summary['stock_total']} 只股票",
        f"- NEW: {summary['new_total']} 条",
        f"- FOLLOW: {summary['follow_total']} 条",
        f"- 最高等级分布: A级 {summary['quality_dist']['A']} / B级 {summary['quality_dist']['B']} / C级 {summary['quality_dist']['C']}",
    ]
    if summary.get("overextended_total"):
        lines.append(
            f"- 过度延伸排除: {summary.get('overextended_total', 0)} 只"
        )
    lines.extend([
        "",
        "> 表内价格和成交量均为已计算好的具体执行区间；公式来源保留在 JSON 的 formula_ref 中。",
    ])
    by_family = sections["by_family"]
    for family in ["PULLBACK", "BREAKOUT", "RETEST"]:
        lines.extend(["", f"## {family_title(family)}", ""])
        rows = by_family.get(family, [])
        if rows:
            lines.extend(plan_table(rows, reporting["main_limit"]))
        else:
            lines.append(f"今日没有 {family} 类买点计划。")
    lines.extend([""])
    lines.extend(field_notes())
    return "\n".join(lines) + "\n"


def write_markdown_plan(date_yy, markdown):
    PLAN_DIR.mkdir(parents=True, exist_ok=True)
    path = PLAN_DIR / f"signal_plan_{date_yy}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    return path


def main():
    _load_dotenv()

    parser = argparse.ArgumentParser(description="Model 4 Signal Plan: next-session VCP setup plans")
    parser.add_argument("--date", help="运行日期，支持 YYMMDD 或 YYYY-MM-DD；默认取最新 quant run")
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 说明生成，使用确定性量价说明兜底")
    parser.add_argument("--progress-file", default=None, help="进度文件路径（供 daily.py 流水线使用）")
    args = parser.parse_args()

    try:
        date_yy = normalize_date_arg(args.date)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    quant_path = quant_run_for_date(date_yy) if date_yy else latest_quant_run()
    if not quant_path:
        print("错误: 未找到模型二 quant run JSON")
        sys.exit(1)
    match = re.fullmatch(r"quant_(\d{6})\.json", quant_path.name)
    if not match:
        print(f"错误: 不支持的 quant run 文件名: {quant_path}")
        sys.exit(1)
    date_yy = match.group(1)

    payload = load_json(quant_path)
    plan = build_signal_plan(payload, date_yy, progress_file=args.progress_file)
    if not args.no_llm:
        plan = attach_llm_notes(plan, progress_file=args.progress_file)
    json_path = write_json_plan(date_yy, plan)
    md_path = write_markdown_plan(date_yy, build_markdown(plan))

    print("=" * 70)
    print("Model 4 Signal Plan")
    print("=" * 70)
    print(f"quant run: {quant_path}")
    print(f"json: {json_path}")
    print(f"markdown: {md_path}")
    print(f"summary: {plan['summary']}")


if __name__ == "__main__":
    main()
