#!/usr/bin/env python3
"""Independent SW L2 turnover-migration and capital-confirmation observer."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path
from statistics import median

import pandas as pd

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))

from scripts.data.capital_data_service import CapitalDataService
from scripts.data.capital_data_sources import MiaoxiangCapitalSource, RequestBudget
from scripts.data.capital_data_store import DB_PATH as CAPITAL_DB_PATH, connect_capital_db
from scripts.data.market_data_store import DB_PATH as MARKET_DB_PATH
from scripts.shared import PROJECT_ROOT, expected_trade_date, normalize_date_arg
from scripts.strategy_config import load_strategy_config
from scripts.dashboard_index import update_dashboard_module


ROOT = Path(PROJECT_ROOT)
OUTPUT_DIR = ROOT / "capital"
DASHBOARD_DATA_DIR = ROOT / "dashboard" / "data"
CONFIG, CONFIG_PATH = load_strategy_config("capital-observer.json")
DATA_CONFIG, _ = load_strategy_config("capital-data.json")


def _mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _ratio(numerator: float | None, denominator: float | None) -> float | None:
    if numerator is None or denominator in (None, 0):
        return None
    return float(numerator) / float(denominator)


def _percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return 100.0 * sum(item <= value for item in values) / len(values)


def _round(value, digits=4):
    if value is None or not math.isfinite(float(value)):
        return None
    return round(float(value), digits)


def _stamp(value: str) -> str:
    return normalize_date_arg(value).replace("-", "")[2:]


def _resolve_market_date(conn: sqlite3.Connection, target: str) -> str:
    row = conn.execute("SELECT MAX(trade_date) FROM daily_bars WHERE trade_date<=?", (target,)).fetchone()
    if not row or not row[0]:
        raise RuntimeError(f"{target} 之前没有共享日线")
    return str(row[0])


def _cross_percentiles(rows: list[dict], key: str) -> dict[str, float]:
    ordered = sorted((float(row[key]), row["sw_code"]) for row in rows)
    count = len(ordered)
    return {code: 100.0 * (index + 1) / count for index, (_, code) in enumerate(ordered)} if count else {}


def calculate_turnover_candidates(
    as_of: str,
    market_db_path: Path | str = MARKET_DB_PATH,
    config: dict | None = None,
) -> dict:
    """Calculate pure-turnover SW L2 migration metrics from the shared market DB."""
    cfg = (config or CONFIG)["turnover"]
    conn = sqlite3.connect(str(market_db_path))
    conn.row_factory = sqlite3.Row
    try:
        trade_date = _resolve_market_date(conn, normalize_date_arg(as_of))
        dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (trade_date, int(cfg["history_days"])),
        )][::-1]
        needed = int(cfg["baseline_days"]) + int(cfg["recent_days"]) + 1
        if len(dates) < needed:
            raise RuntimeError(f"资金迁移历史不足: {len(dates)}/{needed}")
        snapshot_row = conn.execute(
            "SELECT MAX(snapshot_date) FROM stock_industries WHERE snapshot_date<=?", (trade_date,)
        ).fetchone()
        snapshot = snapshot_row[0] if snapshot_row else None
        if not snapshot:
            raise RuntimeError(f"{trade_date} 之前没有申万行业快照")
        definitions = {row[0]: row[1] for row in conn.execute(
            """SELECT industry_code,industry_name FROM industry_definitions
               WHERE snapshot_date=? AND industry_system='sw' AND LENGTH(industry_code)=5""",
            (snapshot,),
        )}
        members = pd.read_sql_query(
            """SELECT si.code,substr(si.sw_industry_code,1,5) AS sw_code,s.name
               FROM stock_industries si JOIN securities s ON s.code=si.code
               WHERE si.snapshot_date=? AND si.sw_industry_code IS NOT NULL""",
            conn,
            params=(snapshot,),
        )
        placeholders = ",".join("?" for _ in dates)
        bars = pd.read_sql_query(
            f"SELECT code,trade_date,amount FROM daily_bars WHERE trade_date IN ({placeholders})",
            conn,
            params=dates,
        )
    finally:
        conn.close()

    bars["amount"] = pd.to_numeric(bars["amount"], errors="coerce")
    market_totals = bars.groupby("trade_date")["amount"].sum(min_count=1).to_dict()
    joined = members.merge(bars, on="code", how="inner")
    joined = joined[joined["sw_code"].isin(definitions)]
    sector_daily = joined.groupby(["sw_code", "trade_date"])["amount"].sum(min_count=1).to_dict()
    member_counts = members[members["sw_code"].isin(definitions)].groupby("sw_code")["code"].nunique().to_dict()
    latest_bars = joined[joined["trade_date"] == trade_date]
    latest_by_sector = {code: frame.copy() for code, frame in latest_bars.groupby("sw_code")}
    history_by_stock = {code: frame.sort_values("trade_date") for code, frame in joined.groupby("code")}

    recent_days = int(cfg["recent_days"])
    baseline_days = int(cfg["baseline_days"])
    rows = []
    for sw_code, sw_name in sorted(definitions.items()):
        shares = []
        for date in dates:
            total = float(market_totals.get(date) or 0.0)
            shares.append(float(sector_daily.get((sw_code, date)) or 0.0) / total if total else 0.0)
        prior = shares[-(baseline_days + recent_days):-recent_days]
        recent = shares[-recent_days:]
        historical = shares[-(int(cfg["stock_baseline_days"]) + 1):-1]
        baseline = _mean(prior)
        current_share = shares[-1]
        share_lift = current_share / baseline - 1 if baseline else 0.0
        recent_share_lift = _mean(recent) / baseline - 1 if baseline else 0.0
        acceleration_prior = shares[-5:-2]
        acceleration = _mean(shares[-2:]) / _mean(acceleration_prior) - 1 if _mean(acceleration_prior) else 0.0
        persistence = sum(value > baseline for value in recent)
        frame = latest_by_sector.get(sw_code, pd.DataFrame())
        effective = int(frame["code"].nunique()) if not frame.empty else 0
        total_members = int(member_counts.get(sw_code) or 0)
        coverage = effective / total_members if total_members else 0.0
        active = 0
        stock_rows = []
        if not frame.empty:
            for item in frame.itertuples(index=False):
                stock_history = history_by_stock.get(item.code)
                previous = [] if stock_history is None else stock_history[stock_history["trade_date"] < trade_date]["amount"].dropna().tail(int(cfg["stock_baseline_days"])).tolist()
                baseline_amount = float(median(previous)) if previous else None
                amount = float(item.amount or 0.0)
                amount_ratio = amount / baseline_amount if baseline_amount else None
                if amount_ratio is not None and amount_ratio >= float(cfg["stock_active_ratio"]):
                    active += 1
                stock_rows.append({
                    "code": item.code,
                    "name": item.name,
                    "amount": amount,
                    "sector_amount_share": amount / float(frame["amount"].sum()) if float(frame["amount"].sum()) else 0.0,
                    "amount_ratio_20d": amount_ratio,
                })
        stock_rows.sort(key=lambda item: (item["amount"], item["code"]), reverse=True)
        breadth = active / effective if effective else 0.0
        top1 = sum(item["amount"] for item in stock_rows[:1]) / sum(item["amount"] for item in stock_rows) if stock_rows else 0.0
        top5 = sum(item["amount"] for item in stock_rows[:5]) / sum(item["amount"] for item in stock_rows) if stock_rows else 0.0
        concentrated = top1 > float(cfg["top1_concentration_limit"]) and breadth < float(cfg["concentrated_breadth_limit"])
        eligible = (
            recent_share_lift >= float(cfg["minimum_share_lift"])
            and _percentile(historical, current_share) >= float(cfg["minimum_share_percentile"])
            and persistence >= int(cfg["minimum_persistence_days"])
            and breadth >= float(cfg["minimum_breadth"])
            and effective >= int(cfg["minimum_effective_members"])
            and coverage >= float(cfg["minimum_data_coverage"])
            and current_share >= float(cfg["minimum_market_share"])
        )
        rows.append({
            "sw_code": sw_code,
            "sw_name": sw_name,
            "trade_date": trade_date,
            "market_share": current_share,
            "share_lift_1d": share_lift,
            "share_lift_3d": recent_share_lift,
            "share_history_percentile": _percentile(historical, current_share),
            "acceleration": acceleration,
            "breadth": breadth,
            "persistence_days": persistence,
            "effective_members": effective,
            "member_count": total_members,
            "data_coverage": coverage,
            "top1_concentration": top1,
            "top5_concentration": top5,
            "concentrated": concentrated,
            "eligible": eligible,
            "top_stocks": stock_rows[:int((config or CONFIG)["stock"]["maximum_per_sector"])],
        })

    ranks = {
        key: _cross_percentiles(rows, key)
        for key in ("share_lift_3d", "share_history_percentile", "acceleration", "breadth", "persistence_days")
    }
    weights = cfg["score_weights"]
    for row in rows:
        code = row["sw_code"]
        score = sum(float(weights[key]) * ranks[key].get(code, 0.0) for key in weights)
        if row["concentrated"]:
            score -= float(cfg["concentration_penalty"])
        row["migration_score"] = max(0.0, score)
        row["turnover_state"] = (
            "CONCENTRATED" if row["concentrated"]
            else "ACCELERATING" if row["eligible"] and row["acceleration"] >= 0.10
            else "MIGRATING_IN" if row["eligible"]
            else "NEUTRAL"
        )
        for key in ("market_share", "share_lift_1d", "share_lift_3d", "acceleration", "breadth", "data_coverage", "top1_concentration", "top5_concentration", "migration_score"):
            row[key] = _round(row[key])
        row["share_history_percentile"] = _round(row["share_history_percentile"], 2)
        for stock in row["top_stocks"]:
            stock["amount"] = _round(stock["amount"], 2)
            stock["sector_amount_share"] = _round(stock["sector_amount_share"])
            stock["amount_ratio_20d"] = _round(stock["amount_ratio_20d"])
    candidates = sorted((row for row in rows if row["eligible"]), key=lambda item: (item["migration_score"], item["sw_code"]), reverse=True)
    candidates = candidates[:int(cfg["maximum_candidates"])]
    for rank, row in enumerate(candidates, 1):
        row["rank"] = rank
    return {
        "trade_date": trade_date,
        "snapshot_date": snapshot,
        "industry_count": len(rows),
        "eligible_count": sum(row["eligible"] for row in rows),
        "candidates": candidates,
    }


def classify_main_order(rows: list[dict], config: dict | None = None) -> dict:
    cfg = (config or CONFIG)["main_order"]
    valid = [row for row in rows if row.get("main_net_inflow_ratio") is not None]
    if not valid:
        return {"state": "INSUFFICIENT", "latest_ratio": None, "mean_3d_ratio": None, "positive_days_5d": 0, "data_date": None}
    ratios = [float(row["main_net_inflow_ratio"]) for row in valid]
    mean3 = _mean(ratios[-3:])
    positive_days = sum(value > 0 for value in ratios[-5:])
    delta = _mean(ratios[-2:]) - _mean(ratios[-5:-2]) if len(ratios) >= 5 else 0.0
    latest = ratios[-1]
    if mean3 >= float(cfg["inflow_ratio"]):
        if latest < 0 or delta <= -float(cfg["acceleration_delta"]):
            state = "INFLOW_WEAKENING"
        else:
            state = "INFLOW_ACCELERATING" if delta >= float(cfg["acceleration_delta"]) else "INFLOW_PERSISTENT"
    elif mean3 <= float(cfg["outflow_ratio"]):
        if latest > 0 or delta >= float(cfg["acceleration_delta"]):
            state = "OUTFLOW_WEAKENING"
        else:
            state = "OUTFLOW_ACCELERATING" if delta <= -float(cfg["acceleration_delta"]) else "OUTFLOW_PERSISTENT"
    else:
        state = "BALANCED"
    return {
        "state": state,
        "latest_ratio": _round(ratios[-1]),
        "mean_3d_ratio": _round(mean3),
        "positive_days_5d": positive_days,
        "momentum_delta": _round(delta),
        "data_date": valid[-1]["trade_date"],
    }


def classify_stock_capital(stock: dict, rows: list[dict], config: dict | None = None) -> dict:
    cfg = (config or CONFIG)["stock"]
    amount_ratio = stock.get("amount_ratio_20d")
    turnover_state = (
        "ENHANCED" if amount_ratio is not None and amount_ratio >= float(cfg["turnover_enhanced_ratio"])
        else "WEAKENED" if amount_ratio is not None and amount_ratio <= float(cfg["turnover_weakened_ratio"])
        else "STABLE" if amount_ratio is not None else "INSUFFICIENT"
    )
    valid_main = [row for row in rows if row.get("main_net_inflow") is not None]
    latest = valid_main[-1] if valid_main else None
    source_amount = latest.get("amount") if latest else None
    if not source_amount:
        source_amount = stock.get("amount")
    main_ratio = _ratio(latest.get("main_net_inflow") if latest else None, source_amount)
    main_state = (
        "INFLOW" if main_ratio is not None and main_ratio >= float(cfg["main_inflow_ratio"])
        else "OUTFLOW" if main_ratio is not None and main_ratio <= float(cfg["main_outflow_ratio"])
        else "BALANCED" if main_ratio is not None else "INSUFFICIENT"
    )
    margin_rows = [row for row in rows if row.get("financing_balance") is not None]
    balance_change = None
    if len(margin_rows) >= 2 and margin_rows[0].get("financing_balance"):
        balance_change = margin_rows[-1]["financing_balance"] / margin_rows[0]["financing_balance"] - 1
    latest_margin = margin_rows[-1] if margin_rows else None
    margin_net = None
    if latest_margin and latest_margin.get("financing_buy") is not None and latest_margin.get("financing_repay") is not None:
        margin_net = latest_margin["financing_buy"] - latest_margin["financing_repay"]
    margin_state = (
        "LEVERAGING" if balance_change is not None and balance_change >= float(cfg["margin_balance_change"])
        else "DELEVERAGING" if balance_change is not None and balance_change <= -float(cfg["margin_balance_change"])
        else "STABLE" if balance_change is not None else "INSUFFICIENT"
    )
    return {
        "turnover_state": turnover_state,
        "main_order_state": main_state,
        "main_net_inflow_ratio": _round(main_ratio),
        "margin_state": margin_state,
        "financing_net_buy": _round(margin_net, 2),
        "financing_balance_change": _round(balance_change),
        "main_data_date": latest.get("trade_date") if latest else None,
        "margin_data_date": latest_margin.get("trade_date") if latest_margin else None,
    }


def build_threshold_reference(config: dict | None = None) -> dict:
    cfg = config or CONFIG
    turnover, main, stock = cfg["turnover"], cfg["main_order"], cfg["stock"]
    pct = lambda value: f"{float(value) * 100:g}%"
    return {
        "entry_rules": [
            {"metric": "近3日份额提升", "standard": f"≥ {pct(turnover['minimum_share_lift'])}", "meaning": "近3日行业全A成交额占比相对此前10日基线明显提高"},
            {"metric": "全A成交额占比历史分位", "standard": f"≥ {float(turnover['minimum_share_percentile']):g}%", "meaning": "当日全A成交额占比处于行业自身此前20日的较高区域"},
            {"metric": "流入持续", "standard": f"最近3日至少 {int(turnover['minimum_persistence_days'])} 日", "meaning": "排除只有一天高于基线的短暂脉冲"},
            {"metric": "放量股票占比", "standard": f"≥ {pct(turnover['minimum_breadth'])}", "meaning": f"成员当日成交额达到自身20日中位数 {float(turnover['stock_active_ratio']):g} 倍的占比"},
            {"metric": "有效成员", "standard": f"≥ {int(turnover['minimum_effective_members'])} 只", "meaning": "避免用成员过少的行业形成板块结论"},
            {"metric": "行情覆盖率", "standard": f"≥ {pct(turnover['minimum_data_coverage'])}", "meaning": "当日具备有效成交数据的成员覆盖"},
            {"metric": "最低全A成交额占比", "standard": f"≥ {pct(turnover['minimum_market_share'])}", "meaning": "过滤极低成交基数导致的虚假高增速"},
            {"metric": "最终展示数量", "standard": f"最多 {int(turnover['maximum_candidates'])} 个", "meaning": "通过全部门槛后再按成交额流入评分排序，不强制选满"},
        ],
        "score_rules": [
            {"metric": "近3日份额提升分位", "standard": f"权重 {pct(turnover['score_weights']['share_lift_3d'])}", "meaning": "行业成交额份额相对基线的提升幅度"},
            {"metric": "全A成交额占比历史分位", "standard": f"权重 {pct(turnover['score_weights']['share_history_percentile'])}", "meaning": "当前全A成交额占比相对行业自身历史的位置"},
            {"metric": "流入加速度分位", "standard": f"权重 {pct(turnover['score_weights']['acceleration'])}", "meaning": "近2日份额相对此前3日是否继续提高"},
            {"metric": "放量股票占比分位", "standard": f"权重 {pct(turnover['score_weights']['breadth'])}", "meaning": "成交活跃是否扩散到更多成分股"},
            {"metric": "持续性分位", "standard": f"权重 {pct(turnover['score_weights']['persistence_days'])}", "meaning": "最近3日高于基线的天数"},
            {"metric": "单股集中惩罚", "standard": f"扣 {float(turnover['concentration_penalty']):g} 分", "meaning": f"Top1占比>{pct(turnover['top1_concentration_limit'])}且扩散度<{pct(turnover['concentrated_breadth_limit'])}"},
        ],
        "state_rules": [
            {"scope": "板块成交额", "state": "流入加速", "standard": "通过全部准入；近2日份额较此前3日提升 ≥ 10%", "meaning": "全市场成交额份额向该行业集中的速度仍在加快"},
            {"scope": "板块成交额", "state": "明显流入", "standard": "通过全部准入；流入加速度 < 10%", "meaning": "成交额份额已持续提高，但未进一步加速"},
            {"scope": "板块成交额", "state": "单股集中", "standard": f"Top1>{pct(turnover['top1_concentration_limit'])}且扩散度<{pct(turnover['concentrated_breadth_limit'])}", "meaning": "主要是单只股票驱动；当前扩散准入会将其排除在候选之外"},
            {"scope": "板块主力资金", "state": "流入增强", "standard": f"3日均值 ≥ {pct(main['inflow_ratio'])}；动量增量 ≥ {pct(main['acceleration_delta'])}", "meaning": "主力资金净流入为正且最近2日继续增强"},
            {"scope": "板块主力资金", "state": "持续流入", "standard": f"3日均值 ≥ {pct(main['inflow_ratio'])}；动量增量 < {pct(main['acceleration_delta'])}", "meaning": "主力资金维持净流入但没有明显加速"},
            {"scope": "板块主力资金", "state": "流入减弱", "standard": f"3日均值 ≥ {pct(main['inflow_ratio'])}；最新日转负或动量增量 ≤ -{pct(main['acceleration_delta'])}", "meaning": "近期仍为净流入，但最新力度已经下降"},
            {"scope": "板块主力资金", "state": "资金平衡", "standard": f"3日均值在 {pct(main['outflow_ratio'])} 至 {pct(main['inflow_ratio'])} 之间", "meaning": "主力资金没有形成明确方向"},
            {"scope": "板块主力资金", "state": "流出减弱", "standard": f"3日均值 ≤ {pct(main['outflow_ratio'])}；最新日转正或动量增量 ≥ {pct(main['acceleration_delta'])}", "meaning": "近期仍为净流出，但流出压力已经下降"},
            {"scope": "板块主力资金", "state": "持续流出", "standard": f"3日均值 ≤ {pct(main['outflow_ratio'])}；未达到流出加速", "meaning": "主力资金维持净流出"},
            {"scope": "板块主力资金", "state": "流出增强", "standard": f"3日均值 ≤ {pct(main['outflow_ratio'])}；动量增量 ≤ -{pct(main['acceleration_delta'])}", "meaning": "主力资金净流出压力继续扩大"},
            {"scope": "个股成交活跃度", "state": "成交放大", "standard": f"当日成交额/20日中位数 ≥ {float(stock['turnover_enhanced_ratio']):g}x", "meaning": "个股成交活跃度显著提高"},
            {"scope": "个股成交活跃度", "state": "成交正常", "standard": f"> {float(stock['turnover_weakened_ratio']):g}x 且 < {float(stock['turnover_enhanced_ratio']):g}x", "meaning": "成交额处于自身常态区间"},
            {"scope": "个股成交活跃度", "state": "成交收缩", "standard": f"当日成交额/20日中位数 ≤ {float(stock['turnover_weakened_ratio']):g}x", "meaning": "个股成交活跃度明显下降"},
            {"scope": "个股主力资金", "state": "主力流入", "standard": f"主力净流入额/成交额 ≥ {pct(stock['main_inflow_ratio'])}", "meaning": "主动性资金净流入"},
            {"scope": "个股主力资金", "state": "资金平衡", "standard": f"净流入率在 {pct(stock['main_outflow_ratio'])} 至 {pct(stock['main_inflow_ratio'])} 之间", "meaning": "主力资金没有明确方向"},
            {"scope": "个股主力资金", "state": "主力流出", "standard": f"主力净流入额/成交额 ≤ {pct(stock['main_outflow_ratio'])}", "meaning": "主动性资金净流出"},
            {"scope": "个股融资资金", "state": "融资增加", "standard": f"区间融资余额变化 ≥ {pct(stock['margin_balance_change'])}", "meaning": "杠杆资金余额增加"},
            {"scope": "个股融资资金", "state": "融资稳定", "standard": f"区间融资余额变化在 -{pct(stock['margin_balance_change'])} 至 {pct(stock['margin_balance_change'])} 之间", "meaning": "融资余额没有显著变化"},
            {"scope": "个股融资资金", "state": "融资下降", "standard": f"区间融资余额变化 ≤ -{pct(stock['margin_balance_change'])}", "meaning": "杠杆资金余额减少"},
            {"scope": "数据质量", "state": "映射不足", "standard": "申万—财富通映射为 weak/unavailable", "meaning": "只跳过板块主力资金；仍可直接查询前5只核心个股的主力资金和融资资金"},
            {"scope": "数据质量", "state": "数据待补", "standard": "缓存缺少所需字段或有效日期", "meaning": "保持空值，不解释为资金平衡"},
        ],
    }


def _mapping_metadata(service: CapitalDataService, candidates: list[dict], as_of: str) -> None:
    status = service.mapping_status(as_of, 2)
    version = status.get("mapping_version")
    with connect_capital_db(service.capital_db_path) as conn:
        for candidate in candidates:
            mappings = [] if not version else [dict(row) for row in conn.execute(
                "SELECT bk_code,bk_name,mapping_rank,mapping_weight,mapping_quality,coverage,purity FROM sector_mappings WHERE mapping_version=? AND sw_code=? ORDER BY mapping_rank",
                (version, candidate["sw_code"]),
            )]
            candidate["mapping"] = {
                "version": version,
                "quality": mappings[0]["mapping_quality"] if mappings else "unavailable",
                "blocks": mappings,
            }


def _decorate_stock_rows(service: CapitalDataService, candidate: dict, start: str, end: str) -> None:
    codes = [item["code"] for item in candidate["top_stocks"]]
    if not codes:
        return
    cached_service = CapitalDataService(
        config=DATA_CONFIG,
        market_db_path=service.market_db_path,
        capital_db_path=service.capital_db_path,
        capital_source=MiaoxiangCapitalSource(request_budget=RequestBudget(0)),
    )
    result = cached_service.fetch_stock_capital(codes, start, end)
    for stock in candidate["top_stocks"]:
        rows = result.get("stocks", {}).get(stock["code"], {}).get("rows", [])
        stock.update(classify_stock_capital(stock, rows))


def select_stock_check_candidates(candidates: list[dict], config: dict | None = None) -> tuple[list[dict], list[dict], list[dict]]:
    main_confirmed = [
        candidate for candidate in candidates
        if candidate["main_order"]["state"] in {"INFLOW_ACCELERATING", "INFLOW_PERSISTENT", "INFLOW_WEAKENING"}
    ]
    mapping_fallback = [
        candidate for candidate in candidates
        if candidate["mapping"]["quality"] not in {"high", "usable"}
    ]
    return main_confirmed, list(candidates), mapping_fallback


def build_observer_context(
    as_of: str,
    fetch: bool = False,
    maximum_requests: int | None = None,
    market_db_path: Path | str = MARKET_DB_PATH,
    capital_db_path: Path | str = CAPITAL_DB_PATH,
    config: dict | None = None,
) -> dict:
    cfg = config or CONFIG
    turnover = calculate_turnover_candidates(as_of, market_db_path, cfg)
    requested = int(maximum_requests if maximum_requests is not None else cfg["miaoxiang"]["default_maximum_requests_per_run"])
    hard = int(cfg["miaoxiang"]["hard_maximum_requests_per_run"])
    if requested < 0 or requested > hard:
        raise ValueError(f"max-mx-requests 必须在 0 到 {hard} 之间")
    budget = RequestBudget(requested if fetch else 0)
    service = CapitalDataService(
        config=DATA_CONFIG,
        market_db_path=market_db_path,
        capital_db_path=capital_db_path,
        capital_source=MiaoxiangCapitalSource(request_budget=budget),
    )
    candidates = turnover["candidates"]
    _mapping_metadata(service, candidates, turnover["trade_date"])
    market_conn = sqlite3.connect(str(market_db_path))
    try:
        dates = [row[0] for row in market_conn.execute(
            "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
            (turnover["trade_date"], int(cfg["main_order"]["lookback_days"])),
        )][::-1]
    finally:
        market_conn.close()
    start = dates[0]
    errors = []
    for candidate in candidates:
        mapping_quality = candidate["mapping"]["quality"]
        if mapping_quality not in {"high", "usable"}:
            candidate["main_order"] = {"state": "MAPPING_UNRELIABLE", "data_date": None}
            continue
        result = service.fetch_sector_capital(candidate["sw_code"], start, turnover["trade_date"])
        candidate["main_order"] = classify_main_order(result.get("rows", []), cfg)
        cache_only_miss = not fetch and result.get("status") == "budget_exhausted" and not result.get("rows")
        candidate["main_order"]["fetch_status"] = "cache_missing" if cache_only_miss else result.get("status")
        if not cache_only_miss:
            errors.extend(result.get("errors", []))

    main_confirmed, stock_check_candidates, fallback = select_stock_check_candidates(candidates, cfg)
    if fetch:
        for candidate in stock_check_candidates:
            if budget.used >= budget.maximum_requests:
                break
            codes = [item["code"] for item in candidate["top_stocks"]]
            result = service.fetch_stock_capital(codes, start, turnover["trade_date"])
            errors.extend(result.get("errors", []))
    for candidate in candidates:
        candidate["selected_for_stock_check"] = candidate in stock_check_candidates
        candidate["stock_check_reason"] = (
            "mapping_fallback" if candidate in fallback
            else "main_capital_confirmed" if candidate in main_confirmed
            else "turnover_candidate"
        )
        _decorate_stock_rows(service, candidate, start, turnover["trade_date"])

    status = "partial" if errors or (fetch and budget.used >= budget.maximum_requests) else "complete"
    return {
        "meta": {
            "strategy_version": cfg["strategy_version"],
            "trade_date": turnover["trade_date"],
            "snapshot_date": turnover["snapshot_date"],
            "generated_for": "capital_dashboard",
            "fetch_enabled": fetch,
            "request_budget": requested if fetch else 0,
            "requests_used": budget.used,
            "status": status,
            "errors": errors,
        },
        "summary": {
            "industry_count": turnover["industry_count"],
            "eligible_count": turnover["eligible_count"],
            "candidate_limit": int(cfg["turnover"]["maximum_candidates"]),
            "candidate_count": len(candidates),
            "main_confirmed_count": len(main_confirmed),
            "stock_checked_sector_count": sum(candidate["selected_for_stock_check"] for candidate in candidates),
            "stock_fallback_sector_count": len(fallback),
        },
        "thresholds": build_threshold_reference(cfg),
        "sectors": candidates,
    }


def _load_dashboard_index() -> dict:
    path = DASHBOARD_DATA_DIR / "index.js"
    if not path.exists():
        return {}
    match = re.search(r"=\s*(\{.*\});\s*$", path.read_text(encoding="utf-8"), re.S)
    return json.loads(match.group(1)) if match else {}


def _write_dashboard_index() -> None:
    dates = sorted(path.stem.rsplit("_", 1)[-1] for path in DASHBOARD_DATA_DIR.glob("*/capital_context_*.js"))
    update_dashboard_module(DASHBOARD_DATA_DIR, "capital", dates)


def write_outputs(context: dict) -> dict:
    stamp = _stamp(context["meta"]["trade_date"])
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    json_path = OUTPUT_DIR / f"capital_observer_{stamp}.json"
    md_path = OUTPUT_DIR / f"capital_observer_{stamp}.md"
    json_path.write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        f"# 资金观测｜{context['meta']['trade_date']}", "",
        f"- 二级行业：{context['summary']['industry_count']}",
        f"- 通过流入门槛：{context['summary']['eligible_count']}",
        f"- 展示候选：{context['summary']['candidate_count']}",
        f"- 妙想请求：{context['meta']['requests_used']}/{context['meta']['request_budget']}", "",
        "| 排名 | 申万二级行业 | 成交额流入 | 近3日份额提升 | 放量股票占比 | 主力资金 |", "|---:|---|---|---:|---:|---|",
    ]
    for sector in context["sectors"]:
        lines.append(
            f"| {sector['rank']} | {sector['sw_name']} | {sector['turnover_state']} | "
            f"{sector['share_lift_3d'] * 100:.1f}% | {sector['breadth'] * 100:.1f}% | {sector['main_order']['state']} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    publish_context(context)
    return {"json": str(json_path), "markdown": str(md_path)}


def publish_context(context: dict) -> Path:
    stamp = _stamp(context["meta"]["trade_date"])
    month_dir = DASHBOARD_DATA_DIR / f"20{stamp[:4]}"
    month_dir.mkdir(parents=True, exist_ok=True)
    path = month_dir / f"capital_context_{stamp}.js"
    path.write_text(
        "window.QUANT_DASHBOARD_CAPITAL_CONTEXTS = window.QUANT_DASHBOARD_CAPITAL_CONTEXTS || {};\n"
        f"window.QUANT_DASHBOARD_CAPITAL_CONTEXTS[{json.dumps(stamp)}] = "
        + json.dumps(context, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    _write_dashboard_index()
    return path


def publish_archives() -> list[str]:
    outputs = []
    for path in sorted(OUTPUT_DIR.glob("capital_observer_*.json")):
        context = json.loads(path.read_text(encoding="utf-8"))
        outputs.append(str(publish_context(context)))
    return outputs


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="独立资金观测业务层")
    sub = root.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="生成申万二级资金观测结果")
    run.add_argument("--date")
    run.add_argument("--fetch", action="store_true", help="显式允许按预算调用妙想")
    run.add_argument("--max-mx-requests", type=int)
    run.add_argument("--dry-run", action="store_true")
    sub.add_parser("publish-dashboard", help="从本地归档重建面板数据包")
    return root


def main() -> int:
    args = parser().parse_args()
    try:
        if args.command == "publish-dashboard":
            print(json.dumps({"outputs": publish_archives()}, ensure_ascii=False, indent=2))
            return 0
        target = normalize_date_arg(args.date) if args.date else expected_trade_date()
        context = build_observer_context(target, fetch=False if args.dry_run else args.fetch, maximum_requests=args.max_mx_requests)
        if args.dry_run:
            maximum_sector_requests = sum(len(row["mapping"]["blocks"]) for row in context["sectors"] if row["mapping"]["quality"] in {"high", "usable"})
            maximum_stock_requests = len(context["sectors"])
            maximum_total_requests = maximum_sector_requests + maximum_stock_requests
            request_budget = int(args.max_mx_requests if args.max_mx_requests is not None else CONFIG["miaoxiang"]["default_maximum_requests_per_run"])
            print(json.dumps({
                "dry_run": True,
                "trade_date": context["meta"]["trade_date"],
                "candidates": [{"sw_code": row["sw_code"], "sw_name": row["sw_name"]} for row in context["sectors"]],
                "maximum_sector_requests": maximum_sector_requests,
                "maximum_stock_requests": maximum_stock_requests,
                "maximum_total_requests": maximum_total_requests,
                "single_run_budget": request_budget,
                "minimum_runs_without_cache": (maximum_total_requests + request_budget - 1) // request_budget if request_budget else None,
            }, ensure_ascii=False, indent=2))
            return 0
        outputs = write_outputs(context)
        print(json.dumps({"meta": context["meta"], "summary": context["summary"], "outputs": outputs}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    sys.exit(main())
