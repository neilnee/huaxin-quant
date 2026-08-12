#!/usr/bin/env python3
"""Build the first-stage rolling performance report for Quant events."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import statistics
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.shared import PROJECT_ROOT, default_pipeline_date
from scripts.strategy_config import load_strategy_config
from scripts.dashboard_index import update_dashboard_module
from scripts.capital_observer import CONFIG as CAPITAL_CONFIG, classify_stock_capital
from scripts.data.capital_data_store import DB_PATH as CAPITAL_DB
from scripts.plan_realization import plan_hit_grade, realized_plan_event, structure_anchor


ROOT = Path(PROJECT_ROOT)
QUANT_DIR = ROOT / "cache" / "quant_runs"
PLAN_DIR = ROOT / "signal_plan"
MARKET_CONTEXT_DIR = ROOT / "market" / "data"
MARKET_REPORT_DIR = ROOT / "market"
MARKET_DB = ROOT / "cache" / "market_data" / "market_data.sqlite"
MARKET_STATE_DB = ROOT / "cache" / "market_regime" / "market_regime.sqlite"
CORPORATE_ACTION_CACHE_DIR = ROOT / "cache" / "market_data" / "corporate_actions"
OUTPUT_DIR = ROOT / "backtest"
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"

CONFIG, CONFIG_PATH = load_strategy_config("backtest.json")
HORIZONS = [int(value) for value in CONFIG["evaluation"]["horizons"]]
WINDOW_MIN = int(CONFIG["evaluation"]["window_min_days"])
WINDOW_MAX = int(CONFIG["evaluation"]["window_max_days"])
DEFAULT_SAMPLE_WINDOW = str(CONFIG["evaluation"]["default_sample_window"])
SAMPLE_WINDOWS = CONFIG["evaluation"]["sample_windows"]
DEFAULT_SAMPLE_WINDOW_LABEL = next(
    window["label"] for window in SAMPLE_WINDOWS if window["id"] == DEFAULT_SAMPLE_WINDOW
)
WIN_THRESHOLD = float(CONFIG["evaluation"]["win_return_threshold_pct"])
BREAKOUT_PIVOT_RATIO = float(CONFIG["evaluation"]["breakout_pivot_ratio"])
CONDITION_CONFIG = CONFIG["condition_evaluation"]
CAPITAL_OBSERVATION_START = str(CONDITION_CONFIG["capital_observation_start_date"])
CAPITAL_LOOKBACK = int(CONDITION_CONFIG["capital_lookback_trade_days"])
SAMPLE_THRESHOLDS = CONDITION_CONFIG["sample_thresholds"]
CONDITIONS = CONDITION_CONFIG["conditions"]
_CORPORATE_ACTION_SOURCE = None


def normalize_date(value: str | None) -> str:
    if not value:
        return default_pipeline_date()
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        return digits[2:]
    if len(digits) == 6:
        return digits
    raise ValueError("日期应为 YYMMDD 或 YYYY-MM-DD")


def iso_date(date_yy: str) -> str:
    return datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")


def date_yy(value: str) -> str:
    return datetime.strptime(value, "%Y-%m-%d").strftime("%y%m%d")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def safe_float(value) -> float | None:
    try:
        number = None if value in (None, "") else float(value)
        return number if number is not None and math.isfinite(number) else None
    except (TypeError, ValueError):
        return None


def quant_rows(date_value: str) -> tuple[dict, dict[str, dict]]:
    path = QUANT_DIR / f"quant_{date_value}.json"
    if not path.exists():
        return {}, {}
    payload = load_json(path)
    rows = {
        str(row.get("code") or "").zfill(6): row
        for row in payload.get("results", [])
        if str(row.get("code") or "").strip("0")
    }
    return payload, rows


def discover_events(report_date_yy: str, calendar: list[str]) -> list[dict]:
    """Return first realized next-session entry for each structure/family/action."""
    report_date = iso_date(report_date_yy)
    valid_dates = [value for value in calendar if value <= report_date]
    seen: set[tuple[str, str, str, str]] = set()
    events: list[dict] = []
    for index, plan_date in enumerate(valid_dates[:-1]):
        entry_date = valid_dates[index + 1]
        plan_stamp = date_yy(plan_date)
        plan_path = PLAN_DIR / f"signal_plan_{plan_stamp}.json"
        if not plan_path.exists():
            continue
        plan_payload = load_json(plan_path)
        source_payload, source_rows = quant_rows(plan_stamp)
        entry_payload, entry_rows = quant_rows(date_yy(entry_date))
        if not entry_rows:
            continue
        strategy_version = entry_payload.get("meta", {}).get("strategy_version", "")
        for plan in plan_payload.get("plans", []):
            code = str(plan.get("code") or "").zfill(6)
            actual = entry_rows.get(code)
            if actual is None:
                continue
            event = realized_plan_event(
                plan,
                actual,
                source_rows.get(code),
                plan_date,
                entry_date,
                strategy_version,
            )
            if event is None:
                continue
            key = (code, event["structure_anchor"], event["setup_family"], event["entry_action"])
            if key in seen:
                continue
            seen.add(key)
            events.append(event)
    return events


def trading_calendar(conn: sqlite3.Connection, report_date: str) -> list[str]:
    rows = conn.execute(
        """SELECT trade_date FROM daily_bars WHERE trade_date<=?
           GROUP BY trade_date HAVING count(*)>=100 ORDER BY trade_date""",
        (report_date,),
    ).fetchall()
    return [row[0] for row in rows]


def load_bars(conn: sqlite3.Connection, codes: list[str], start_date: str, end_date: str) -> dict[str, dict[str, float]]:
    if not codes:
        return {}
    placeholders = ",".join("?" for _ in codes)
    rows = conn.execute(
        f"""SELECT code,trade_date,close FROM daily_bars
             WHERE code IN ({placeholders}) AND trade_date BETWEEN ? AND ?
             ORDER BY code,trade_date""",
        [*codes, start_date, end_date],
    ).fetchall()
    result: dict[str, dict[str, float]] = {}
    for code, trade_date, close in rows:
        result.setdefault(code, {})[trade_date] = float(close)
    return result


def load_corporate_actions(code: str, through_date: str | None = None) -> list[dict]:
    """Load cached TDX dividend/rights events, fetching once when absent."""
    cache_path = CORPORATE_ACTION_CACHE_DIR / f"{code}.json"
    if cache_path.exists():
        payload = load_json(cache_path)
        if isinstance(payload, dict):
            fetched_date = str(payload.get("fetched_at") or "")[:10]
            if through_date is None or fetched_date >= through_date:
                return payload.get("actions", [])
    try:
        global _CORPORATE_ACTION_SOURCE
        from scripts.data.market_data import TDXSource

        if _CORPORATE_ACTION_SOURCE is None:
            _CORPORATE_ACTION_SOURCE = TDXSource()
        frame = _CORPORATE_ACTION_SOURCE._get_client().xdxr(symbol=code)
        actions = []
        for _, row in frame.iterrows():
            if int(row.get("category") or 0) != 1:
                continue
            actions.append({
                "date": f"{int(row['year']):04d}-{int(row['month']):02d}-{int(row['day']):02d}",
                "cash_dividend_per_10": safe_float(row.get("fenhong")) or 0.0,
                "bonus_shares_per_10": safe_float(row.get("songzhuangu")) or 0.0,
                "rights_shares_per_10": safe_float(row.get("peigu")) or 0.0,
                "rights_price": safe_float(row.get("peigujia")) or 0.0,
            })
        actions.sort(key=lambda item: item["date"])
        CORPORATE_ACTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({
            "code": code,
            "source": "tdx_xdxr",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "actions": actions,
        }, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return actions
    except Exception as exc:
        _CORPORATE_ACTION_SOURCE = None
        raise RuntimeError(f"{code} 除权数据不可用，回测已停止: {exc}") from exc


def holding_period_value(entry_date: str, target_date: str, target_close: float, actions: list[dict]) -> tuple[float, list[dict]]:
    """Return terminal value per entry-date share, including cash and share changes."""
    shares = 1.0
    cash = 0.0
    applied = []
    for action in actions:
        action_date = str(action.get("date") or "")
        if not entry_date < action_date <= target_date:
            continue
        old_shares = shares
        dividend = safe_float(action.get("cash_dividend_per_10")) or 0.0
        bonus = safe_float(action.get("bonus_shares_per_10")) or 0.0
        rights = safe_float(action.get("rights_shares_per_10")) or 0.0
        rights_price = safe_float(action.get("rights_price")) or 0.0
        cash += old_shares * dividend / 10.0
        cash -= old_shares * rights * rights_price / 10.0
        shares += old_shares * (bonus + rights) / 10.0
        applied.append(action)
    return shares * target_close + cash, applied


def price_on_or_before(prices: dict[str, float], target: str, not_before: str) -> float | None:
    dates = [value for value in prices if not_before <= value <= target]
    return prices[max(dates)] if dates else None


def breakout_performance(
    event: dict,
    prices: dict[str, float],
    calendar: list[str],
    signal_index: int,
    report_index: int,
    actions: list[dict] | None = None,
) -> dict:
    """Find the first close above the signal-time frozen pivot."""
    try:
        pivot = float(event.get("structure_pivot"))
    except (TypeError, ValueError):
        pivot = 0.0
    if pivot <= 0:
        return {"breakout_time": None, "breakout_date": None, "breakout_days": None, "breakout_return": None}

    threshold = pivot * BREAKOUT_PIVOT_RATIO
    end_index = min(report_index, signal_index + WINDOW_MAX)
    entry_date = str(event.get("signal_date") or calendar[signal_index])
    for current_index in range(signal_index, end_index + 1):
        current_date = calendar[current_index]
        current_close = prices.get(current_date)
        if current_close is None:
            continue
        comparable_value, _ = holding_period_value(entry_date, current_date, current_close, actions or [])
        if comparable_value < threshold:
            continue
        days = current_index - signal_index
        gain = round((comparable_value / pivot - 1) * 100, 3)
        return {
            "breakout_time": f"{current_date} · T+{days}",
            "breakout_date": current_date,
            "breakout_days": days,
            "breakout_return": gain,
        }
    return {"breakout_time": None, "breakout_date": None, "breakout_days": None, "breakout_return": None}


def load_signal_environment(conn: sqlite3.Connection, event: dict, state_conn: sqlite3.Connection | None = None) -> dict:
    """Load the environment on the actual entry date (signal_date compatibility alias)."""
    date = event["signal_date"]
    yy = event["signal_date_yy"]
    context_path = MARKET_CONTEXT_DIR / f"market_context_{yy}.json"
    context = load_json(context_path) if context_path.exists() else {}
    market_report_path = MARKET_REPORT_DIR / f"market_regime_{yy}.json"
    market_report = load_json(market_report_path) if market_report_path.exists() else {}
    market_state = context.get("market_state", {})
    market_code = market_report.get("state", {}).get("current") or market_state.get("raw_label") or "UNKNOWN"
    market_label = market_state.get("label") or "环境待确认"

    row = conn.execute(
        """SELECT si.snapshot_date,id.industry_name
             FROM stock_industries si LEFT JOIN industry_definitions id
               ON id.snapshot_date=si.snapshot_date AND id.industry_system='sw'
              AND id.industry_code=substr(si.sw_industry_code,1,5)
            WHERE si.code=?
            ORDER BY CASE
                       WHEN si.snapshot_date=? THEN 0
                       WHEN si.snapshot_date<? THEN 1
                       ELSE 2
                     END,
                     CASE WHEN si.snapshot_date<=? THEN si.snapshot_date END DESC,
                     CASE WHEN si.snapshot_date>? THEN si.snapshot_date END ASC
            LIMIT 1""",
        (event["code"], date, date, date, date),
    ).fetchone()
    membership_date = row[0] if row else None
    industry = row[1] if row and row[1] else ""
    membership_basis = (
        "point_in_time" if membership_date == date else
        "prior_snapshot" if membership_date and membership_date < date else
        "earliest_available_backfill" if membership_date else
        "unavailable"
    )
    sector_state = "环境待确认"
    sector_state_source = "unavailable"
    sector_history_basis = None
    if industry:
        state_row = state_conn.execute(
            """SELECT sector_state,history_basis FROM sector_daily_metrics
               WHERE trade_date=? AND block_kind='industry_sw_l2' AND block_name=?""",
            (date, industry),
        ).fetchone() if state_conn is not None else None
        if state_row:
            sector_state = state_row[0] or sector_state
            sector_history_basis = state_row[1]
            sector_state_source = "market_regime.sqlite"
        else:
            for sector in context.get("sector_rankings", {}).get("industry_sw_l2", []):
                if sector.get("block_name") == industry:
                    sector_state = sector.get("sector_state") or sector_state
                    sector_history_basis = sector.get("history_basis")
                    sector_state_source = "market_context"
                    break
    return {
        "market_state": market_code,
        "market_state_label": market_label,
        "sector_name": industry or "行业待确认",
        "sector_state": sector_state,
        "sector_membership_date": membership_date,
        "sector_membership_basis": membership_basis,
        "sector_state_source": sector_state_source,
        "sector_history_basis": sector_history_basis,
    }


def missing_capital_support(observation_date: str, status: str = "unavailable") -> dict:
    return {
        "observation_date": observation_date,
        "observation_start_date": CAPITAL_OBSERVATION_START,
        "status": status,
        "main_order_state": "INSUFFICIENT",
        "main_net_inflow_ratio": None,
        "main_positive_days_3d": 0,
        "main_observation_days_3d": 0,
        "main_data_date": None,
        "margin_state": "INSUFFICIENT",
        "financing_balance_change": None,
        "financing_net_buy": None,
        "margin_data_date": None,
        "source": "capital_data.sqlite",
        "strategy_version": CAPITAL_CONFIG["strategy_version"],
    }


def load_plan_capital(conn: sqlite3.Connection | None, event: dict) -> dict:
    """Classify cached stock capital using only facts available through Plan date."""
    observation_date = str(event.get("plan_date") or "")
    if observation_date < CAPITAL_OBSERVATION_START:
        return missing_capital_support(observation_date, "before_observation_start")
    if conn is None:
        return missing_capital_support(observation_date)
    rows = conn.execute(
        """SELECT trade_date,metrics_json FROM stock_capital
           WHERE code=? AND trade_date<=?
           ORDER BY trade_date DESC LIMIT ?""",
        (event["code"], observation_date, CAPITAL_LOOKBACK),
    ).fetchall()
    history = [
        {"trade_date": row[0], **json.loads(row[1])}
        for row in reversed(rows)
    ]
    if not history:
        return missing_capital_support(observation_date)
    result = classify_stock_capital({"amount": None, "amount_ratio_20d": None}, history)
    valid_main = [row for row in history if row.get("main_net_inflow") is not None]
    main_available = result["main_order_state"] != "INSUFFICIENT"
    margin_available = result["margin_state"] != "INSUFFICIENT"
    return {
        "observation_date": observation_date,
        "observation_start_date": CAPITAL_OBSERVATION_START,
        "status": "complete" if main_available and margin_available else "partial" if main_available else "unavailable",
        "main_order_state": result["main_order_state"],
        "main_net_inflow_ratio": result["main_net_inflow_ratio"],
        "main_positive_days_3d": sum(float(row["main_net_inflow"]) > 0 for row in valid_main[-3:]),
        "main_observation_days_3d": len(valid_main[-3:]),
        "main_data_date": result["main_data_date"],
        "margin_state": result["margin_state"],
        "financing_balance_change": result["financing_balance_change"],
        "financing_net_buy": result["financing_net_buy"],
        "margin_data_date": result["margin_data_date"],
        "source": "capital_data.sqlite",
        "strategy_version": CAPITAL_CONFIG["strategy_version"],
    }


def add_performance(events: list[dict], report_date_yy: str) -> list[dict]:
    report_date = iso_date(report_date_yy)
    conn = sqlite3.connect(MARKET_DB)
    capital_conn = sqlite3.connect(CAPITAL_DB) if CAPITAL_DB.exists() else None
    state_conn = sqlite3.connect(MARKET_STATE_DB) if MARKET_STATE_DB.exists() else None
    try:
        calendar = trading_calendar(conn, report_date)
        index = {value: idx for idx, value in enumerate(calendar)}
        eligible = [event for event in events if event["signal_date"] in index and report_date in index]
        if not eligible:
            return []
        start_date = min(event["signal_date"] for event in eligible)
        bars = load_bars(conn, sorted({event["code"] for event in eligible}), start_date, report_date)
        action_map = {code: load_corporate_actions(code, report_date) for code in sorted({event["code"] for event in eligible})}
        result = []
        for event in eligible:
            age = index[report_date] - index[event["signal_date"]]
            if age < WINDOW_MIN:
                continue
            prices = bars.get(event["code"], {})
            signal_close = prices.get(event["signal_date"])
            if signal_close is None:
                try:
                    signal_close = float(event.get("signal_close_snapshot"))
                except (TypeError, ValueError):
                    signal_close = None
            performance = {}
            actions = action_map.get(event["code"], [])
            for horizon in HORIZONS:
                value = None
                if signal_close and age >= horizon:
                    target = calendar[index[event["signal_date"]] + horizon]
                    target_close = price_on_or_before(prices, target, event["signal_date"])
                    if target_close is not None:
                        target_value, _ = holding_period_value(event["signal_date"], target, target_close, actions)
                        value = round((target_value / signal_close - 1) * 100, 3)
                performance[f"return_{horizon}d"] = value
            observation_end_date = calendar[min(index[report_date], index[event["signal_date"]] + WINDOW_MAX)]
            report_close = price_on_or_before(prices, observation_end_date, event["signal_date"])
            report_value = None
            applied_actions = []
            if report_close is not None:
                report_value, applied_actions = holding_period_value(event["signal_date"], observation_end_date, report_close, actions)
            breakout = breakout_performance(
                event,
                prices,
                calendar,
                index[event["signal_date"]],
                index[report_date],
                actions,
            )
            capital = load_plan_capital(capital_conn, event)
            result.append({
                **event,
                **load_signal_environment(conn, event, state_conn),
                "capital_support": capital,
                "main_order_state": capital["main_order_state"],
                "margin_state": capital["margin_state"],
                "age_days": age,
                "observation_age_days": min(age, WINDOW_MAX),
                "observation_end_date": observation_end_date,
                "signal_close": round(signal_close, 3) if signal_close is not None else None,
                "report_close": round(report_close, 3) if report_close is not None else None,
                "report_value_adjusted": round(report_value, 3) if report_value is not None else None,
                "return_basis": "holding_period_total_return",
                "corporate_actions": applied_actions,
                **breakout,
                **performance,
            })
        return sorted(result, key=lambda item: (item["signal_date"], item["code"], item["event_type"]), reverse=True)
    finally:
        if capital_conn is not None:
            capital_conn.close()
        if state_conn is not None:
            state_conn.close()
        conn.close()


def horizon_stats(rows: list[dict]) -> dict:
    result = {}
    for horizon in HORIZONS:
        values = [float(row[f"return_{horizon}d"]) for row in rows if row.get(f"return_{horizon}d") is not None]
        result[str(horizon)] = {
            "samples": len(values),
            "winners": sum(value > WIN_THRESHOLD for value in values),
            "win_rate": round(sum(value > WIN_THRESHOLD for value in values) / len(values) * 100, 1) if values else None,
            "avg_return": round(statistics.fmean(values), 3) if values else None,
            "median_return": round(statistics.median(values), 3) if values else None,
        }
    return result


def grouped_stats(rows: list[dict], field: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for row in rows:
        groups.setdefault(str(row.get(field) or "环境待确认"), []).append(row)
    return [
        {"group": group, "events": len(items), "horizons": horizon_stats(items)}
        for group, items in sorted(groups.items(), key=lambda item: (-len(item[1]), item[0]))
    ]


def condition_matches(value, condition: dict) -> bool:
    values = condition.get("values", [])
    operator = condition.get("operator")
    if operator == "in":
        return value in values
    if operator == "not_in":
        return value not in values
    raise ValueError(f"未知条件操作符: {operator}")


def condition_sample_status(samples: int) -> str:
    if samples < int(SAMPLE_THRESHOLDS["preliminary"]):
        return "INSUFFICIENT"
    if samples < int(SAMPLE_THRESHOLDS["stable"]):
        return "PRELIMINARY"
    return "STABILITY_WATCH"


def condition_horizon(rows: list[dict], condition: dict, horizon: int) -> dict:
    field = condition["field"]
    unavailable = set(condition.get("unavailable_values", []))
    baseline_values = set(condition.get("baseline_values", []))
    weak_values = set(condition.get("weak_values", []))
    matured = [row for row in rows if row.get(f"return_{horizon}d") is not None]
    available = [row for row in matured if row.get(field) not in (None, "") and row.get(field) not in unavailable]
    baseline = [row for row in available if not baseline_values or row.get(field) in baseline_values]
    retained = [row for row in baseline if condition_matches(row.get(field), condition)]
    excluded = [row for row in baseline if not condition_matches(row.get(field), condition)]
    weak = [row for row in available if row.get(field) in weak_values]
    baseline_stats = horizon_stats(baseline)[str(horizon)]
    retained_stats = horizon_stats(retained)[str(horizon)]
    excluded_stats = horizon_stats(excluded)[str(horizon)]
    weak_stats = horizon_stats(weak)[str(horizon)]
    baseline_winners = baseline_stats["winners"]
    excluded_winners = excluded_stats["winners"]
    return {
        "baseline_samples": len(baseline),
        "retained_samples": len(retained),
        "excluded_samples": len(excluded),
        "weak_samples": len(weak),
        "unavailable_samples": len(matured) - len(baseline) - len(weak),
        "retention_rate": round(len(retained) / len(baseline) * 100, 1) if baseline else None,
        "baseline_win_rate": baseline_stats["win_rate"],
        "retained_win_rate": retained_stats["win_rate"],
        "win_rate_lift": round(retained_stats["win_rate"] - baseline_stats["win_rate"], 1) if retained_stats["win_rate"] is not None and baseline_stats["win_rate"] is not None else None,
        "baseline_avg_return": baseline_stats["avg_return"],
        "retained_avg_return": retained_stats["avg_return"],
        "avg_return_lift": round(retained_stats["avg_return"] - baseline_stats["avg_return"], 3) if retained_stats["avg_return"] is not None and baseline_stats["avg_return"] is not None else None,
        "retained_median_return": retained_stats["median_return"],
        "excluded_avg_return": excluded_stats["avg_return"],
        "neutral_avg_return": excluded_stats["avg_return"],
        "weak_avg_return": weak_stats["avg_return"],
        "missed_winners": excluded_winners,
        "missed_winner_rate": round(excluded_winners / baseline_winners * 100, 1) if baseline_winners else None,
        "sample_status": condition_sample_status(len(retained)),
    }


def evaluate_conditions(rows: list[dict]) -> list[dict]:
    result = []
    for condition in CONDITIONS:
        result.append({
            "condition_id": condition["id"],
            "category": condition["category"],
            "label": condition["label"],
            "field": condition["field"],
            "operator": condition["operator"],
            "values": condition["values"],
            "horizons": {
                str(horizon): condition_horizon(rows, condition, horizon)
                for horizon in HORIZONS
            },
        })
    return result


def build_analysis_profile(rows: list[dict], setup_family: str) -> dict:
    selected = rows if setup_family == "ALL" else [row for row in rows if row.get("setup_family") == setup_family]
    dates = sorted(row["entry_date"] for row in selected if row.get("entry_date"))
    return {
        "setup_family": setup_family,
        "events": len(selected),
        "sample_start_date": dates[0] if dates else None,
        "sample_end_date": dates[-1] if dates else None,
        "baseline": horizon_stats(selected),
        "grade_groups": grouped_stats(selected, "entry_grade"),
        "maturity_groups": grouped_stats(selected, "maturity_stage"),
        "condition_evaluations": evaluate_conditions(selected),
    }


def build_sample_window(rows: list[dict], window: dict) -> dict:
    max_age = window.get("max_age_trade_days")
    selected = rows if max_age is None else [row for row in rows if int(row["age_days"]) <= int(max_age)]
    profiles = [
        build_analysis_profile(selected, family)
        for family in ["ALL", *CONFIG["event_rules"]["setup_families"]]
    ]
    dates = sorted(row["entry_date"] for row in selected if row.get("entry_date"))
    return {
        "id": window["id"],
        "label": window["label"],
        "max_age_trade_days": max_age,
        "events": selected,
        "summary": {
            "events": len(selected),
            "a_events": sum(row["entry_grade"] == "A" for row in selected),
            "regular_events": sum(row["entry_grade"] == "REGULAR" for row in selected),
            "sample_start_date": dates[0] if dates else None,
            "sample_end_date": dates[-1] if dates else None,
            "horizons": horizon_stats(selected),
        },
        "setup_profiles": profiles,
        "setup_groups": grouped_stats(selected, "setup_family"),
    }


def build_context(report_date_yy: str) -> dict:
    conn = sqlite3.connect(MARKET_DB)
    try:
        calendar = trading_calendar(conn, iso_date(report_date_yy))
    finally:
        conn.close()
    analysis_events = add_performance(discover_events(report_date_yy, calendar), report_date_yy)
    sample_windows = [build_sample_window(analysis_events, window) for window in SAMPLE_WINDOWS]
    default_window = next(window for window in sample_windows if window["id"] == DEFAULT_SAMPLE_WINDOW)
    events = default_window["events"]
    profiles = default_window["setup_profiles"]
    return {
        "meta": {
            "report_date": iso_date(report_date_yy),
            "report_date_yy": report_date_yy,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy_version": CONFIG["strategy_version"],
            "strategy_file": str(CONFIG_PATH),
            "window_min_days": WINDOW_MIN,
            "window_max_days": WINDOW_MAX,
            "default_sample_window": DEFAULT_SAMPLE_WINDOW,
            "sample_window_definitions": SAMPLE_WINDOWS,
            "horizons": HORIZONS,
            "breakout_pivot_ratio": BREAKOUT_PIVOT_RATIO,
            "return_basis": "holding_period_total_return",
            "corporate_action_source": "tdx_xdxr_cache",
            "capital_source": "capital_data.sqlite",
            "capital_strategy_version": CAPITAL_CONFIG["strategy_version"],
            "capital_observation_start_date": CAPITAL_OBSERVATION_START,
            "condition_sample_thresholds": SAMPLE_THRESHOLDS,
        },
        "summary": {
            "events": len(events),
            "new_events": sum(row["entry_action"] == "NEW" for row in events),
            "follow_events": sum(row["entry_action"] == "FOLLOW" for row in events),
            "a_events": sum(row["entry_grade"] == "A" for row in events),
            "regular_events": sum(row["entry_grade"] == "REGULAR" for row in events),
            "horizons": horizon_stats(events),
        },
        "analysis_summary": {
            "events": len(analysis_events),
            "horizons": horizon_stats(analysis_events),
        },
        "events": events,
        "sample_windows": sample_windows,
        "setup_profiles": profiles,
        "setup_groups": default_window["setup_groups"],
        "action_groups": grouped_stats(events, "entry_action"),
        "grade_groups": grouped_stats(events, "entry_grade"),
        "maturity_groups": grouped_stats(events, "maturity_stage"),
        "market_groups": grouped_stats(events, "market_state_label"),
        "sector_groups": grouped_stats(events, "sector_state"),
    }


def update_dashboard_index() -> None:
    dates = sorted(path.stem.rsplit("_", 1)[-1] for path in DASHBOARD_DATA_DIR.glob("*/backtest_context_*.js"))
    update_dashboard_module(DASHBOARD_DATA_DIR, "backtest", dates)


def render_markdown(context: dict) -> str:
    main_labels = {"INFLOW": "流入", "BALANCED": "平衡", "OUTFLOW": "流出", "INSUFFICIENT": "数据不足"}
    margin_labels = {"LEVERAGING": "加杠杆", "STABLE": "稳定", "DELEVERAGING": "去杠杆", "NOT_APPLICABLE": "不适用", "INSUFFICIENT": "数据不足"}
    lines = [
        f"# 实际买点回测｜{context['meta']['report_date']}", "",
        f"成立范围：{DEFAULT_SAMPLE_WINDOW_LABEL}｜收益观察：成立后 {'/'.join(map(str, HORIZONS))} 个交易日｜事件 {context['summary']['events']} 条", "",
        "| Plan日 | 成立日 | 股票 | 买点 | 等级 | 形态 | 成立价 | 年龄 | 突破时间 | 突破时涨幅 | 5日 | 10日 | 20日 | 成立时市场 | 成立时板块 | Plan日主力 | Plan日融资 |", "|---|---|---|---|---|---|---:|---:|---|---:|---:|---:|---:|---|---|---|---|",
    ]
    for row in context["events"]:
        value = lambda horizon: "—" if row.get(f"return_{horizon}d") is None else f"{row[f'return_{horizon}d']:.2f}%"
        breakout_value = "—" if row.get("breakout_return") is None else f"{row['breakout_return']:.2f}%"
        lines.append(
            f"| {row['plan_date']} | {row['entry_date']} | {row['name']}（{row['code']}） | {row['setup_family']} | {row['entry_grade']} | {row['maturity_stage']} | {row['signal_close']:.2f} | {row['age_days']} | {row['breakout_time'] or '无'} | {breakout_value} | {value(5)} | {value(10)} | {value(20)} | {row['market_state_label']} | {row['sector_name']} · {row['sector_state']} | {main_labels.get(row.get('main_order_state'), '数据不足')} | {margin_labels.get(row.get('margin_state'), '数据不足')} |"
        )
    if not context["events"]:
        lines.append("| — | — | 当前窗口没有已满5个交易日的实际买点事件 | — | — | — | — | — | — | — | — | — | — | — | — | — | — |")
    lines.extend(["", "> 以买点实际成立日收盘价为基准，送转、分红和配股按持有期总回报调整；不代表真实交易收益。", ""])
    return "\n".join(lines)


def publish(context: dict) -> tuple[Path, Path, Path]:
    date = context["meta"]["report_date_yy"]
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    month_dir = DASHBOARD_DATA_DIR / f"20{date[:4]}"
    month_dir.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"backtest_{date}.json"
    md_path = OUTPUT_DIR / f"backtest_{date}.md"
    js_path = month_dir / f"backtest_context_{date}.js"
    payload = json.dumps(context, ensure_ascii=False, indent=2)
    json_path.write_text(payload + "\n", encoding="utf-8")
    md_path.write_text(render_markdown(context), encoding="utf-8")
    js_path.write_text(
        "window.QUANT_DASHBOARD_BACKTEST_CONTEXTS = window.QUANT_DASHBOARD_BACKTEST_CONTEXTS || {};\n"
        f"window.QUANT_DASHBOARD_BACKTEST_CONTEXTS[{json.dumps(date)}] = {payload};\n",
        encoding="utf-8",
    )
    update_dashboard_index()
    return json_path, md_path, js_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Signal Plan 次日实际买点的滚动价格表现")
    parser.add_argument("--date", help="报告日期 YYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--prefetch-actions", help="逗号分隔股票代码；只预取并缓存除权数据")
    args = parser.parse_args()
    if args.prefetch_actions:
        codes = sorted({value.strip().zfill(6) for value in args.prefetch_actions.split(",") if value.strip()})
        completed = 0
        for code in codes:
            cache_path = CORPORATE_ACTION_CACHE_DIR / f"{code}.json"
            load_corporate_actions(code)
            completed += cache_path.exists()
        print(f"[backtest] corporate actions cached: {completed}/{len(codes)}")
        if completed != len(codes):
            raise RuntimeError("部分股票除权数据预取失败")
        return
    report_date = normalize_date(args.date)
    if not MARKET_DB.exists():
        raise FileNotFoundError(f"共享日线库不存在: {MARKET_DB}")
    context = build_context(report_date)
    paths = publish(context)
    print(
        f"[backtest] {report_date}: {context['summary']['events']} events in {DEFAULT_SAMPLE_WINDOW} / "
        f"{context['analysis_summary']['events']} all historical events"
    )
    for path in paths:
        print(f"[backtest] wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
