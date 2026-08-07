#!/usr/bin/env python3
"""Build the first-stage rolling performance report for Quant events."""

from __future__ import annotations

import argparse
import json
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


ROOT = Path(PROJECT_ROOT)
QUANT_DIR = ROOT / "cache" / "quant_runs"
MARKET_CONTEXT_DIR = ROOT / "market" / "data"
MARKET_REPORT_DIR = ROOT / "market"
MARKET_DB = ROOT / "cache" / "market_data" / "market_data.sqlite"
OUTPUT_DIR = ROOT / "backtest"
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"

CONFIG, CONFIG_PATH = load_strategy_config("backtest.json")
MATURE_STAGES = set(CONFIG["event_rules"]["mature_stages"])
SETUP_SIGNALS = set(CONFIG["event_rules"]["setup_signals"])
HORIZONS = [int(value) for value in CONFIG["evaluation"]["horizons"]]
WINDOW_MIN = int(CONFIG["evaluation"]["window_min_days"])
WINDOW_MAX = int(CONFIG["evaluation"]["window_max_days"])
WIN_THRESHOLD = float(CONFIG["evaluation"]["win_return_threshold_pct"])
BREAKOUT_PIVOT_RATIO = float(CONFIG["evaluation"]["breakout_pivot_ratio"])


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


def structure_anchor(row: dict) -> str:
    group = row.get("contraction_group") or []
    if group and isinstance(group[0], dict) and group[0].get("start_date"):
        return str(group[0]["start_date"])
    contractions = row.get("contractions") or []
    if contractions and isinstance(contractions[-1], dict) and contractions[-1].get("start_date"):
        return str(contractions[-1]["start_date"])
    return str(row.get("structure_breakout_date") or "unanchored")


def discover_events(report_date_yy: str) -> list[dict]:
    """Return first mature and first setup events for each structure anchor."""
    seen: set[tuple[str, str, str]] = set()
    events: list[dict] = []
    for path in sorted(QUANT_DIR.glob("quant_*.json")):
        match = re.fullmatch(r"quant_(\d{6})\.json", path.name)
        if not match or match.group(1) > report_date_yy:
            continue
        payload = load_json(path)
        run_date = str(payload.get("meta", {}).get("run_date") or iso_date(match.group(1)))[:10]
        strategy_version = payload.get("meta", {}).get("strategy_version", "")
        for row in payload.get("results", []):
            code = str(row.get("code") or "").zfill(6)
            if not code.strip("0"):
                continue
            anchor = structure_anchor(row)
            common = {
                "signal_date": run_date,
                "signal_date_yy": date_yy(run_date),
                "code": code,
                "name": row.get("name") or code,
                "structure_anchor": anchor,
                "signal_close_snapshot": row.get("close"),
                "structure_pivot": row.get("structure_pivot") or row.get("pivot_price"),
                "structure_stage": row.get("structure_stage") or "NONE",
                "setup_signal": row.get("setup_signal") or "NONE",
                "structure_score": row.get("structure_score"),
                "structure_risk_score": row.get("structure_risk_score"),
                "quant_strategy_version": strategy_version or row.get("strategy_version", ""),
            }
            if common["structure_stage"] in MATURE_STAGES:
                key = (code, anchor, "MATURE_ENTRY")
                if key not in seen:
                    seen.add(key)
                    events.append({**common, "event_type": "MATURE_ENTRY", "setup_type": ""})
            if common["setup_signal"] in SETUP_SIGNALS:
                key = (code, anchor, "SETUP_TRIGGER")
                if key not in seen:
                    seen.add(key)
                    events.append({**common, "event_type": "SETUP_TRIGGER", "setup_type": common["setup_signal"]})
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


def price_on_or_before(prices: dict[str, float], target: str, not_before: str) -> float | None:
    dates = [value for value in prices if not_before <= value <= target]
    return prices[max(dates)] if dates else None


def breakout_performance(
    event: dict,
    prices: dict[str, float],
    calendar: list[str],
    signal_index: int,
    report_index: int,
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
    for current_index in range(signal_index, end_index + 1):
        current_date = calendar[current_index]
        current_close = prices.get(current_date)
        if current_close is None or current_close < threshold:
            continue
        days = current_index - signal_index
        gain = round((current_close / pivot - 1) * 100, 3)
        return {
            "breakout_time": f"{current_date} · T+{days}",
            "breakout_date": current_date,
            "breakout_days": days,
            "breakout_return": gain,
        }
    return {"breakout_time": None, "breakout_date": None, "breakout_days": None, "breakout_return": None}


def load_signal_environment(conn: sqlite3.Connection, event: dict) -> dict:
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
        """SELECT id.industry_name
             FROM stock_industries si LEFT JOIN industry_definitions id
               ON id.snapshot_date=si.snapshot_date AND id.industry_system='sw'
              AND id.industry_code=substr(si.sw_industry_code,1,5)
            WHERE si.snapshot_date=? AND si.code=?""",
        (date, event["code"]),
    ).fetchone()
    industry = row[0] if row and row[0] else ""
    sector_state = "环境待确认"
    if industry:
        for sector in context.get("sector_rankings", {}).get("industry_sw_l2", []):
            if sector.get("block_name") == industry:
                sector_state = sector.get("sector_state") or sector_state
                break
    return {
        "market_state": market_code,
        "market_state_label": market_label,
        "sector_name": industry or "行业待确认",
        "sector_state": sector_state,
    }


def add_performance(events: list[dict], report_date_yy: str) -> list[dict]:
    report_date = iso_date(report_date_yy)
    conn = sqlite3.connect(MARKET_DB)
    try:
        calendar = trading_calendar(conn, report_date)
        index = {value: idx for idx, value in enumerate(calendar)}
        eligible = [event for event in events if event["signal_date"] in index and report_date in index]
        if not eligible:
            return []
        start_date = min(event["signal_date"] for event in eligible)
        bars = load_bars(conn, sorted({event["code"] for event in eligible}), start_date, report_date)
        result = []
        for event in eligible:
            age = index[report_date] - index[event["signal_date"]]
            if age < WINDOW_MIN or age > WINDOW_MAX:
                continue
            prices = bars.get(event["code"], {})
            signal_close = prices.get(event["signal_date"])
            if signal_close is None:
                try:
                    signal_close = float(event.get("signal_close_snapshot"))
                except (TypeError, ValueError):
                    signal_close = None
            performance = {}
            for horizon in HORIZONS:
                value = None
                if signal_close and age >= horizon:
                    target = calendar[index[event["signal_date"]] + horizon]
                    target_close = price_on_or_before(prices, target, event["signal_date"])
                    if target_close is not None:
                        value = round((target_close / signal_close - 1) * 100, 3)
                performance[f"return_{horizon}d"] = value
            report_close = price_on_or_before(prices, report_date, event["signal_date"])
            breakout = breakout_performance(
                event,
                prices,
                calendar,
                index[event["signal_date"]],
                index[report_date],
            )
            result.append({
                **event,
                **load_signal_environment(conn, event),
                "age_days": age,
                "signal_close": round(signal_close, 3) if signal_close is not None else None,
                "report_close": round(report_close, 3) if report_close is not None else None,
                **breakout,
                **performance,
            })
        return sorted(result, key=lambda item: (item["signal_date"], item["code"], item["event_type"]), reverse=True)
    finally:
        conn.close()


def horizon_stats(rows: list[dict]) -> dict:
    result = {}
    for horizon in HORIZONS:
        values = [float(row[f"return_{horizon}d"]) for row in rows if row.get(f"return_{horizon}d") is not None]
        result[str(horizon)] = {
            "samples": len(values),
            "win_rate": round(sum(value > WIN_THRESHOLD for value in values) / len(values) * 100, 1) if values else None,
            "avg_return": round(statistics.fmean(values), 3) if values else None,
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


def build_context(report_date_yy: str) -> dict:
    events = add_performance(discover_events(report_date_yy), report_date_yy)
    return {
        "meta": {
            "report_date": iso_date(report_date_yy),
            "report_date_yy": report_date_yy,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "strategy_version": CONFIG["strategy_version"],
            "strategy_file": str(CONFIG_PATH),
            "window_min_days": WINDOW_MIN,
            "window_max_days": WINDOW_MAX,
            "horizons": HORIZONS,
            "breakout_pivot_ratio": BREAKOUT_PIVOT_RATIO,
        },
        "summary": {
            "events": len(events),
            "mature_events": sum(row["event_type"] == "MATURE_ENTRY" for row in events),
            "trigger_events": sum(row["event_type"] == "SETUP_TRIGGER" for row in events),
            "horizons": horizon_stats(events),
        },
        "events": events,
        "event_groups": grouped_stats(events, "event_type"),
        "market_groups": grouped_stats(events, "market_state_label"),
        "sector_groups": grouped_stats(events, "sector_state"),
    }


def update_dashboard_index() -> None:
    index_path = DASHBOARD_DATA_DIR / "index.js"
    index = {}
    if index_path.exists():
        match = re.search(r"=\s*(\{.*\});\s*$", index_path.read_text(encoding="utf-8"), re.S)
        if match:
            index = json.loads(match.group(1))
    dates = sorted(path.stem.rsplit("_", 1)[-1] for path in DASHBOARD_DATA_DIR.glob("*/backtest_context_*.js"))
    index["backtest"] = {"latest": dates[-1] if dates else None, "available": dates}
    index_path.write_text("window.QUANT_DASHBOARD_INDEX = " + json.dumps(index, ensure_ascii=False) + ";\n", encoding="utf-8")


def render_markdown(context: dict) -> str:
    lines = [
        f"# 策略回测｜{context['meta']['report_date']}", "",
        f"窗口：首次事件后 {WINDOW_MIN}~{WINDOW_MAX} 个交易日｜事件 {context['summary']['events']} 条", "",
        "| 信号日 | 股票 | 事件 | 买点 | 年龄 | 突破时间 | 突破时涨幅 | 5日 | 10日 | 20日 | 信号时市场 | 信号时板块 |", "|---|---|---|---|---:|---|---:|---:|---:|---:|---|---|",
    ]
    for row in context["events"]:
        value = lambda horizon: "—" if row.get(f"return_{horizon}d") is None else f"{row[f'return_{horizon}d']:.2f}%"
        breakout_value = "—" if row.get("breakout_return") is None else f"{row['breakout_return']:.2f}%"
        lines.append(
            f"| {row['signal_date']} | {row['name']}（{row['code']}） | {row['event_type']} | {row['setup_type'] or '—'} | {row['age_days']} | {row['breakout_time'] or '无'} | {breakout_value} | {value(5)} | {value(10)} | {value(20)} | {row['market_state_label']} | {row['sector_name']} · {row['sector_state']} |"
        )
    if not context["events"]:
        lines.append("| — | 当前窗口没有已满5个交易日的首次成熟或买点事件 | — | — | — | — | — | — | — | — | — | — |")
    lines.extend(["", "> 仅评价策略信号后的价格表现，不代表实际交易收益。", ""])
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
    parser = argparse.ArgumentParser(description="策略首次成熟/买点的滚动价格表现")
    parser.add_argument("--date", help="报告日期 YYMMDD 或 YYYY-MM-DD")
    args = parser.parse_args()
    report_date = normalize_date(args.date)
    if not MARKET_DB.exists():
        raise FileNotFoundError(f"共享日线库不存在: {MARKET_DB}")
    context = build_context(report_date)
    paths = publish(context)
    print(f"[backtest] {report_date}: {context['summary']['events']} events")
    for path in paths:
        print(f"[backtest] wrote {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
