#!/usr/bin/env python3
"""Independent TDX-backed market regime and block heat module."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.data.market_data_service import MarketDataService, resolve_as_of
from scripts.data.market_data_store import connect_db, create_schema
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config


CONFIG, CONFIG_PATH = load_strategy_config("market-regime.json")
ROOT = Path(PROJECT_ROOT)
STATE_DIR = ROOT / "cache" / "market_regime"
STATE_DB_PATH = STATE_DIR / "market_regime.sqlite"
OUTPUT_DIR = ROOT / "market"
DATA_OUTPUT_DIR = OUTPUT_DIR / "data"
DASHBOARD_DIR = ROOT / "dashboard"
DASHBOARD_DATA_DIR = DASHBOARD_DIR / "data"
DASHBOARD_START_DATE = "260709"


def load_local_env() -> None:
    """Load local credentials without overriding process-level configuration."""
    env_path = ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def today_stamp(date_value: str) -> str:
    return date_value.replace("-", "")[2:]


def connect_state_db() -> sqlite3.Connection:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(STATE_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS block_metrics (
        trade_date TEXT NOT NULL, block_kind TEXT NOT NULL, block_name TEXT NOT NULL,
        heat_score REAL NOT NULL, rank INTEGER NOT NULL,
        PRIMARY KEY (trade_date, block_kind, block_name)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS sector_daily_metrics (
        trade_date TEXT NOT NULL, block_kind TEXT NOT NULL, block_name TEXT NOT NULL,
        member_count INTEGER NOT NULL, rank_20 INTEGER NOT NULL, rank_5 INTEGER NOT NULL,
        return_1 REAL NOT NULL, return_5 REAL NOT NULL, return_10 REAL NOT NULL, return_20 REAL NOT NULL,
        relative_strength_5 REAL NOT NULL, relative_strength_20 REAL NOT NULL,
        median_return_1 REAL NOT NULL, advance_ratio REAL NOT NULL, volume_activity REAL NOT NULL,
        above_ma20_ratio REAL NOT NULL, above_ma60_ratio REAL NOT NULL, new_high_ratio REAL NOT NULL,
        strong_stock_density REAL NOT NULL, sector_state TEXT NOT NULL,
        history_basis TEXT NOT NULL DEFAULT 'point_in_time',
        PRIMARY KEY (trade_date, block_kind, block_name)
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sector_daily_metrics)")}
    if "history_basis" not in columns:
        conn.execute("ALTER TABLE sector_daily_metrics ADD COLUMN history_basis TEXT NOT NULL DEFAULT 'point_in_time'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sector_daily_lookup ON sector_daily_metrics(block_kind, block_name, trade_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_state_history (
        trade_date TEXT PRIMARY KEY, raw_state TEXT NOT NULL, confirmed_state TEXT NOT NULL,
        candidate_days INTEGER NOT NULL, confirmation_days INTEGER NOT NULL
    )""")
    conn.commit()
    return conn


def percentile_score(value: float, values: pd.Series) -> float:
    valid = values.dropna()
    if valid.empty or not math.isfinite(value):
        return 50.0
    return round(float((valid <= value).mean() * 100), 2)


def indicators(frame: pd.DataFrame) -> pd.DataFrame:
    frame = frame.sort_values(["code", "trade_date"]).copy()
    group = frame.groupby("code", group_keys=False)
    close, volume = group["close"], group["volume"]
    frame["ma20"] = close.transform(lambda s: s.rolling(20).mean())
    frame["ma60"] = close.transform(lambda s: s.rolling(60).mean())
    frame["ret1"] = close.pct_change(1)
    frame["ret5"] = close.pct_change(5)
    frame["ret10"] = close.pct_change(10)
    frame["ret20"] = close.pct_change(20)
    frame["ret60"] = close.pct_change(60)
    prev = close.shift(1)
    tr = pd.concat([frame["high"] - frame["low"], (frame["high"] - prev).abs(), (frame["low"] - prev).abs()], axis=1).max(axis=1)
    frame["atr14_pct"] = tr.groupby(frame["code"]).transform(lambda s: s.rolling(14).mean()) / frame["close"]
    frame["vol_ma20"] = volume.transform(lambda s: s.rolling(20).mean())
    frame["volume_ratio"] = frame["volume"] / frame["vol_ma20"]
    frame["high60"] = group["high"].transform(lambda s: s.rolling(60).max())
    frame["low60"] = group["low"].transform(lambda s: s.rolling(60).min())
    frame["new_high60"] = frame["close"] >= frame["high60"]
    frame["new_low60"] = frame["close"] <= frame["low60"]
    return frame


def sector_rows_for_date(universe: pd.DataFrame, blocks: pd.DataFrame, trade_date: str, history_basis: str) -> list[dict]:
    current = universe[(universe.trade_date == trade_date) & universe.ma60.notna()].copy()
    merged = blocks.merge(current, on="code", how="inner")
    market_median = {window: float(current[f"ret{window}"].median()) for window in (5, 20)}
    records = []
    for (kind, name), group in merged.groupby(["block_kind", "block_name"]):
        if len(group) < 3:
            continue
        rel5 = group.ret5.median() - market_median[5]
        rel20 = group.ret20.median() - market_median[20]
        strong = ((group.new_high60) | (group.ret5 >= group.ret5.quantile(0.9))).mean()
        records.append({"date": trade_date, "block_type": kind, "block_name": name, "member_count": len(group),
            "return_1": round(float(group.ret1.median() * 100), 3), "return_5": round(float(group.ret5.median() * 100), 3),
            "return_10": round(float(group.ret10.median() * 100), 3), "return_20": round(float(group.ret20.median() * 100), 3),
            "relative_strength_5": round(float(rel5 * 100), 3), "relative_strength_20": round(float(rel20 * 100), 3),
            "volume_activity": round(float(group.volume_ratio.median()), 3), "up_breadth": round(float((group.ret1 > 0).mean() * 100), 2),
            "median_return_1": round(float(group.ret1.median() * 100), 3), "above_ma20_ratio": round(float((group.close > group.ma20).mean() * 100), 2),
            "above_ma60_ratio": round(float((group.close > group.ma60).mean() * 100), 2), "new_high_ratio": round(float(group.new_high60.mean() * 100), 2),
            "strong_stock_density": round(float(strong * 100), 2), "sector_state": "基线回填", "history_basis": history_basis})
    sector_rows = []
    for kind in sorted({row["block_type"] for row in records}):
        rows = [row for row in records if row["block_type"] == kind]
        for rank, row in enumerate(sorted(rows, key=lambda item: item["relative_strength_20"], reverse=True), 1):
            row["rank_20"] = rank; row["rank"] = rank
        for rank, row in enumerate(sorted(rows, key=lambda item: item["relative_strength_5"], reverse=True), 1):
            row["rank_5"] = rank
        sector_rows.extend(sorted(rows, key=lambda item: item["rank_20"]))
    return sector_rows


def ensure_sector_baseline(conn: sqlite3.Connection, state_conn: sqlite3.Connection, as_of: str) -> int:
    dates = [row[0] for row in conn.execute("""SELECT trade_date FROM daily_bars
        WHERE code='IDX:000001' AND trade_date<=? ORDER BY trade_date DESC LIMIT 20""", (as_of,))][::-1]
    existing = {row[0] for row in state_conn.execute("SELECT DISTINCT trade_date FROM sector_daily_metrics WHERE trade_date IN ({})".format(",".join("?" for _ in dates)), dates)} if dates else set()
    missing = [date for date in dates if date not in existing and date != as_of]
    if not missing:
        return 0
    universe = pd.read_sql_query("""SELECT b.* FROM daily_bars b JOIN universe_members u ON b.code=u.code
        WHERE u.trade_date=? AND u.eligible=1 AND b.trade_date<=?""", conn, params=(as_of, as_of))
    universe = indicators(universe)
    blocks = pd.read_sql_query("""SELECT m.block_kind,m.block_name,m.code FROM block_members m
        JOIN universe_members u ON m.code=u.code WHERE m.snapshot_date=? AND u.trade_date=? AND u.eligible=1""", conn, params=(as_of, as_of))
    for date in missing:
        rows = sector_rows_for_date(universe, blocks, date, "current_snapshot_backfill")
        persist_sector_metrics(state_conn, date, rows, write_legacy=False)
    return len(missing)


def compute_metrics(conn: sqlite3.Connection, state_conn: sqlite3.Connection, as_of: str) -> tuple[dict, list[dict], list[dict], list[dict]]:
    total, valid, ratio = MarketDataService.coverage(conn, as_of)
    if ratio < CONFIG["data"]["minimum_coverage_ratio"]:
        raise RuntimeError(f"全 A 覆盖率不足: {ratio:.2%} ({valid}/{total})")
    universe = pd.read_sql_query("""SELECT b.* FROM daily_bars b JOIN universe_members u ON b.code=u.code
        WHERE u.trade_date=? AND u.eligible=1 AND b.trade_date<=?""", conn, params=(as_of, as_of))
    universe = indicators(universe)
    current = universe[universe.trade_date == as_of].copy()
    current = current[current.ma60.notna()].copy()
    for window in (5, 20, 60):
        current[f"rps{window}_market"] = current[f"ret{window}"].rank(pct=True, method="average") * 100
    breadth = {
        "universe_size": int(total), "valid_count": int(valid), "coverage_ratio": round(ratio, 4),
        "advance_count": int((current.ret1.fillna(0) > 0).sum()),
        "decline_count": int((current.ret1.fillna(0) < 0).sum()),
        "flat_count": int((current.ret1.fillna(0) == 0).sum()),
        "advance_ratio": round(float((current.ret1.fillna(0) > 0).mean() * 100), 2),
        "median_return_1": round(float(current.ret1.median() * 100), 3),
        "above_ma20_ratio": round(float((current.close > current.ma20).mean() * 100), 2),
        "above_ma60_ratio": round(float((current.close > current.ma60).mean() * 100), 2),
        "new_high_ratio": round(float(current.new_high60.mean() * 100), 2),
        "new_low_ratio": round(float(current.new_low60.mean() * 100), 2),
        "new_high_minus_low_ratio": round(float((current.new_high60.mean() - current.new_low60.mean()) * 100), 2),
    }
    breadth_score = np.mean([breadth["advance_ratio"], breadth["above_ma20_ratio"], breadth["above_ma60_ratio"], max(0, min(100, 50 + breadth["new_high_minus_low_ratio"]))])

    bench = pd.read_sql_query("SELECT * FROM daily_bars WHERE code LIKE 'IDX:%' AND trade_date<=?", conn, params=(as_of,))
    bench = indicators(bench)
    benchmark_metrics, trend_scores, vol_scores = {}, [], []
    for name, code in CONFIG["benchmarks"].items():
        data = bench[bench.code == f"IDX:{code}"].sort_values("trade_date")
        latest = data.iloc[-1]
        slope = (data.iloc[-1].ma20 / data.iloc[-6].ma20 - 1) if len(data) >= 6 else 0.0
        trend = np.mean([100 if latest.close > latest.ma20 else 0, percentile_score(slope, data.ma20.pct_change(5)), 100 if latest.ma20 > latest.ma60 else 0, percentile_score(latest.ret20, data.ret20)])
        realized = data.close.pct_change().rolling(10).std().iloc[-1] * np.sqrt(252)
        range10 = (data.high.tail(10).max() - data.low.tail(10).min()) / data.low.tail(10).min()
        volatility = np.mean([percentile_score(latest.atr14_pct, data.atr14_pct), percentile_score(realized, data.close.pct_change().rolling(10).std() * np.sqrt(252)), percentile_score(range10, data.high.rolling(10).max() / data.low.rolling(10).min() - 1)])
        benchmark_metrics[name] = {
            "code": code, "close": round(float(latest.close), 4),
            "return_1": round(float(latest.ret1 * 100), 2), "return_5": round(float(latest.ret5 * 100), 2),
            "return_10": round(float(latest.ret10 * 100), 2), "return_20": round(float(latest.ret20 * 100), 2), "return_60": round(float(latest.ret60 * 100), 2),
            "above_ma20": bool(latest.close > latest.ma20), "above_ma60": bool(latest.close > latest.ma60),
            "ma20_above_ma60": bool(latest.ma20 > latest.ma60),
            "ma20_slope_5": round(float(slope * 100), 3), "distance_high60_pct": round(float((latest.close / latest.high60 - 1) * 100), 2),
            "volume_ratio_20": round(float(latest.volume_ratio), 3), "atr14_pct": round(float(latest.atr14_pct * 100), 3),
            "volume_ratio_5_20": round(float(data.volume.tail(5).mean() / data.volume.tail(20).mean()), 3),
            "trend_score": round(float(trend), 2), "volatility_score": round(float(volatility), 2),
            "trend_series": [{"close": round(float(row.close), 4), "ma20": round(float(row.ma20), 4), "ma60": round(float(row.ma60), 4)}
                for row in data.tail(20).dropna(subset=["ma20", "ma60"]).itertuples(index=False)],
        }
        trend_scores.append(trend); vol_scores.append(volatility)

    blocks = pd.read_sql_query("""SELECT m.block_kind,m.block_name,m.code FROM block_members m
        JOIN universe_members u ON m.code=u.code WHERE m.snapshot_date=? AND u.trade_date=? AND u.eligible=1""", conn, params=(as_of, as_of))
    merged = blocks.merge(current, on="code", how="inner")
    market_median = {window: float(current[f"ret{window}"].median()) for window in (1, 5, 10, 20)}
    records = []
    for (kind, name), group in merged.groupby(["block_kind", "block_name"]):
        if len(group) < 3:
            continue
        rel5 = group.ret5.median() - market_median[5]
        rel20 = group.ret20.median() - market_median[20]
        strong = ((group.new_high60) | (group.ret5 >= group.ret5.quantile(0.9))).mean()
        records.append({"date": as_of, "block_type": kind, "block_name": name, "member_count": len(group),
            "return_1": round(float(group.ret1.median() * 100), 3), "return_5": round(float(group.ret5.median() * 100), 3),
            "return_10": round(float(group.ret10.median() * 100), 3), "return_20": round(float(group.ret20.median() * 100), 3),
            "relative_strength_5": round(float(rel5 * 100), 3), "relative_strength_20": round(float(rel20 * 100), 3),
            "volume_activity": round(float(group.volume_ratio.median()), 3), "up_breadth": round(float((group.ret1 > 0).mean() * 100), 2),
            "median_return_1": round(float(group.ret1.median() * 100), 3), "above_ma20_ratio": round(float((group.close > group.ma20).mean() * 100), 2),
            "above_ma60_ratio": round(float((group.close > group.ma60).mean() * 100), 2), "new_high_ratio": round(float(group.new_high60.mean() * 100), 2),
            "strong_stock_density": round(float(strong * 100), 2), "sector_state": "历史积累中", "history_basis": "point_in_time"})
    sector_rows = []
    for kind in sorted({row["block_type"] for row in records}):
        rows = [row for row in records if row["block_type"] == kind]
        for rank, row in enumerate(sorted(rows, key=lambda item: item["relative_strength_20"], reverse=True), 1):
            row["rank_20"] = rank; row["rank"] = rank
        for rank, row in enumerate(sorted(rows, key=lambda item: item["relative_strength_5"], reverse=True), 1):
            row["rank_5"] = rank
        sector_rows.extend(sorted(rows, key=lambda item: item["rank_20"]))
    top_sets = {kind: {row["block_name"] for row in sector_rows if row["block_type"] == kind and row["rank"] <= 10} for kind in {row["block_type"] for row in sector_rows}}
    previous = pd.read_sql_query("SELECT block_kind,block_name,rank_20 AS rank FROM sector_daily_metrics WHERE trade_date=(SELECT max(trade_date) FROM sector_daily_metrics WHERE trade_date<?)", state_conn, params=(as_of,))
    overlaps = []
    for kind in top_sets:
        old = set(previous[(previous.block_kind == kind) & (previous["rank"] <= 10)].block_name)
        if old:
            overlaps.append(len(top_sets[kind] & old) / 10)
    rotation = round((1 - float(np.mean(overlaps))) * 100, 2) if overlaps else 50.0
    history = pd.read_sql_query("""SELECT trade_date,block_kind,block_name,rank_20,relative_strength_5,advance_ratio AS up_breadth
        FROM sector_daily_metrics WHERE trade_date<? ORDER BY trade_date DESC LIMIT 30000""", state_conn, params=(as_of,))
    for row in sector_rows:
        prior = history[(history.block_kind == row["block_type"]) & (history.block_name == row["block_name"])].head(5)
        if len(prior) < 5:
            row["sector_state"] = "历史积累中"
            continue
        top20_days = int((prior.rank_20 <= 20).sum())
        if row["rank_20"] <= 20 and top20_days >= 4 and row["relative_strength_5"] >= 0 and row["up_breadth"] >= 50:
            row["sector_state"] = "持续主线"
        elif row["rank_20"] <= 20 and top20_days >= 4 and (row["relative_strength_5"] < 0 or row["up_breadth"] < 45):
            row["sector_state"] = "高位分歧"
        elif row["rank_20"] <= 20 and row["rank_5"] <= 10 and top20_days < 3:
            row["sector_state"] = "新晋强化"
        elif row["rank_5"] <= 10 and row["rank_20"] > 20:
            row["sector_state"] = "轮动脉冲"
        else:
            row["sector_state"] = "弱势退潮"
    trend_score, volatility_score = float(np.median(trend_scores)), float(np.median(vol_scores))
    above20 = sum(item["above_ma20"] for item in benchmark_metrics.values())
    above60 = sum(item["above_ma60"] for item in benchmark_metrics.values())
    if breadth_score <= CONFIG["state_thresholds"]["defensive"]["breadth_max"] or (volatility_score >= CONFIG["state_thresholds"]["defensive"]["volatility_min"] and rotation >= CONFIG["state_thresholds"]["defensive"]["rotation_min"]):
        state = "DEFENSIVE"
    elif trend_score >= CONFIG["state_thresholds"]["offensive"]["trend_min"] and breadth_score >= CONFIG["state_thresholds"]["offensive"]["breadth_min"] and volatility_score <= CONFIG["state_thresholds"]["offensive"]["volatility_max"]:
        state = "OFFENSIVE"
    elif above20 >= 3 and above60 <= 2 and breadth["advance_ratio"] >= 50:
        state = "RECOVERY_WATCH"
    else:
        state = "SELECTIVE"
    security_context = pd.read_sql_query("""SELECT s.code,s.name,si.sw_industry_code
        FROM securities s LEFT JOIN stock_industries si ON s.code=si.code AND si.snapshot_date=?""", conn, params=(as_of,))
    stock_frame = current.merge(security_context, on="code", how="left")
    stock_frame["sw_l2_code"] = stock_frame.sw_industry_code.fillna("").str.slice(0, 5)
    sw_l2_names = pd.read_sql_query("""SELECT industry_code,industry_name FROM industry_definitions
        WHERE snapshot_date=? AND industry_system='sw'""", conn, params=(as_of,))
    stock_frame = stock_frame.merge(sw_l2_names, left_on="sw_l2_code", right_on="industry_code", how="left")
    stock_frame["rps20_industry"] = stock_frame.groupby("sw_l2_code")["ret20"].rank(pct=True, method="average") * 100
    stock_frame["rps60_industry"] = stock_frame.groupby("sw_l2_code")["ret60"].rank(pct=True, method="average") * 100
    l2_heat = {(row["block_name"]): row for row in sector_rows if row["block_type"] == "industry_sw_l2"}
    stock_rows = []
    for row in stock_frame.itertuples(index=False):
        industry_row = l2_heat.get(row.industry_name, {}) if pd.notna(row.industry_name) else {}
        trend_position = (50 if row.close > row.ma20 else 0) + (50 if row.close > row.ma60 else 0)
        strength = 0.35 * row.rps20_market + 0.25 * row.rps60_market + 0.20 * row.rps20_industry + 0.10 * trend_position + 0.10 * min(100, row.volume_ratio * 50)
        stock_rows.append({"date": as_of, "code": row.code, "name": row.name, "close": round(float(row.close), 4), "return_5": round(float(row.ret5 * 100), 3), "return_20": round(float(row.ret20 * 100), 3), "return_60": round(float(row.ret60 * 100), 3), "rps5_market": round(float(row.rps5_market), 2), "rps20_market": round(float(row.rps20_market), 2), "rps60_market": round(float(row.rps60_market), 2), "sw_l2_code": row.sw_l2_code, "sw_l2_name": row.industry_name if pd.notna(row.industry_name) else "", "rps20_industry": round(float(row.rps20_industry), 2), "rps60_industry": round(float(row.rps60_industry), 2), "industry_relative_strength_20": industry_row.get("relative_strength_20"), "industry_rank": industry_row.get("rank_20"), "above_ma20": bool(row.close > row.ma20), "above_ma60": bool(row.close > row.ma60), "distance_high60_pct": round(float((row.close / row.high60 - 1) * 100), 3), "atr14_pct": round(float(row.atr14_pct * 100), 3), "volume_ratio_20": round(float(row.volume_ratio), 3), "strength_score": round(float(strength), 2)})
    stock_by_code = {row["code"]: row for row in stock_rows}
    sector_leaders = {}
    for (kind, name), group in merged.groupby(["block_kind", "block_name"]):
        if kind not in {"industry_sw_l1", "industry_sw_l2", "gn", "fg"}:
            continue
        members = [stock_by_code[code] for code in group.code.drop_duplicates() if code in stock_by_code]
        leaders = []
        for rank, stock in enumerate(sorted(members, key=lambda item: item["strength_score"], reverse=True)[:3], start=1):
            role = "领涨" if rank == 1 else "趋势核心" if stock["above_ma20"] and stock["above_ma60"] else "观察"
            leaders.append({key: stock[key] for key in ("code", "name", "strength_score", "rps20_market", "rps20_industry", "return_20", "above_ma20", "above_ma60", "volume_ratio_20")} | {"rank": rank, "role": role})
        sector_leaders[f"{kind}:{name}"] = leaders
    concept_heat = {(row["block_name"]): row for row in sector_rows if row["block_type"] == "gn"}
    concept_frame = merged[merged.block_kind == "gn"].merge(stock_frame[["code", "name", "ret20"]], on="code", how="left", suffixes=("", "_stock"))
    concept_rows = []
    for (name, code), group in concept_frame.groupby(["block_name", "code"]):
        row = group.iloc[0]
        same_concept = concept_frame[concept_frame.block_name == name]
        within = float((same_concept.ret20 <= row.ret20).mean() * 100)
        heat = concept_heat.get(name, {})
        concept_rows.append({"date": as_of, "code": code, "name": row.get("name", ""), "concept_name": name, "concept_relative_strength_20": heat.get("relative_strength_20"), "concept_rank": heat.get("rank_20"), "stock_return_20": round(float(row.ret20 * 100), 3), "rps20_within_concept": round(within, 2), "stock_vs_concept_return_20": round(float((row.ret20 - same_concept.ret20.mean()) * 100), 3)})
    report = {"meta": {"run_date": as_of, "strategy_version": CONFIG["strategy_version"], "data_status": "VALID", "config": str(CONFIG_PATH)}, "state": {"current": state, "raw_state": state, "trend_score": round(trend_score, 2), "volatility_score": round(volatility_score, 2), "breadth_score": round(float(breadth_score), 2), "rotation_score": rotation}, "benchmarks": benchmark_metrics, "breadth": breadth, "rotation": {"top10_sets": {k: sorted(v) for k, v in top_sets.items()}}, "sector_leaders": sector_leaders}
    return report, sector_rows, stock_rows, concept_rows


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader(); writer.writerows(rows)


def write_markdown(report: dict, sectors: list[dict], stocks: list[dict], as_of: str) -> None:
    stamp = today_stamp(as_of)
    state = report["state"]
    breadth = report["breadth"]
    lines = [
        f"# 市场状态与板块热度日报｜{as_of}",
        "",
        f"**市场状态：{state['current']}**  ",
        f"数据覆盖：{breadth['valid_count']}/{breadth['universe_size']}（{breadth['coverage_ratio']:.2%}）",
        "",
        "## 市场四维",
        "",
        "| 趋势 | 波动风险 | 广度 | 轮动 |",
        "|---:|---:|---:|---:|",
        f"| {state['trend_score']:.2f} | {state['volatility_score']:.2f} | {state['breadth_score']:.2f} | {state['rotation_score']:.2f} |",
        "",
        "## 宽基与广度",
        "",
        "| 指数 | 收盘 | 20日收益 | 趋势分 | 波动分 |",
        "|---|---:|---:|---:|---:|",
    ]
    for name, item in report["benchmarks"].items():
        lines.append(f"| {name} | {item['close']:.2f} | {item['return_20']:.2f}% | {item['trend_score']:.2f} | {item['volatility_score']:.2f} |")
    lines.extend([
        "",
        f"广度：上涨占比 {breadth['advance_ratio']:.2f}%｜站上 MA20 {breadth['above_ma20_ratio']:.2f}%｜站上 MA60 {breadth['above_ma60_ratio']:.2f}%｜60日新高减新低 {breadth['new_high_minus_low_ratio']:.2f}%",
    ])
    labels = [("industry_sw_l1", "申万一级行业"), ("industry_sw_l2", "申万二级行业"), ("gn", "概念题材"), ("fg", "风格特征")]
    for kind, label in labels:
        top = [row for row in sectors if row["block_type"] == kind][:5]
        if not top:
            continue
        lines.extend(["", f"## {label}相对强度 Top 5", "", "| 排名 | 板块 | 20日相对强度 | 5日相对强度 | 上涨扩散 |", "|---:|---|---:|---:|---:|"])
        for row in top:
            lines.append(f"| {row['rank_20']} | {row['block_name']} | {row['relative_strength_20']:.2f}% | {row['relative_strength_5']:.2f}% | {row['up_breadth']:.2f}% |")
    lines.extend(["", "## 全市场强度 Top 10", "", "| 代码 | 股票 | 强度分 | RPS20 | 申万二级 |", "|---|---|---:|---:|---|"])
    for row in sorted(stocks, key=lambda item: item["strength_score"], reverse=True)[:10]:
        lines.append(f"| {row['code']} | {row['name']} | {row['strength_score']:.2f} | {row['rps20_market']:.2f} | {row['sw_l2_name']} |")
    lines.extend(["", "---", "", "说明：本报告仅呈现市场、板块和相对强度事实；不改变模型二 VCP 判断，也不构成交易建议。"])
    (OUTPUT_DIR / f"market_regime_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def state_label(state: str, report: dict) -> str:
    if state == "DEFENSIVE":
        return "弱势下行"
    if state == "OFFENSIVE":
        return "趋势扩散"
    if state == "RECOVERY_WATCH":
        return "修复观察"
    return "震荡轮动" if report["state"]["rotation_score"] >= 60 else "结构性强势"


def confirm_market_state(state_conn: sqlite3.Connection, as_of: str, report: dict) -> None:
    raw = report["state"]["raw_state"]
    settings = CONFIG["state_thresholds"]
    required = settings["confirmation_by_state"][raw]
    window = settings["confirmation_window_days"]
    previous = state_conn.execute("""SELECT raw_state, confirmed_state FROM market_state_history
        WHERE trade_date<? ORDER BY trade_date DESC LIMIT ?""", (as_of, window - 1)).fetchall()
    last_confirmed = previous[0]["confirmed_state"] if previous else None
    if not last_confirmed:
        confirmed, candidate_days = raw, required
    elif raw == last_confirmed:
        confirmed, candidate_days = raw, required
    else:
        candidate_days = 1 + sum(row["raw_state"] == raw for row in previous)
        confirmed = raw if candidate_days >= required else last_confirmed
    state_conn.execute("""INSERT INTO market_state_history(trade_date,raw_state,confirmed_state,candidate_days,confirmation_days)
        VALUES(?,?,?,?,?) ON CONFLICT(trade_date) DO UPDATE SET raw_state=excluded.raw_state,
        confirmed_state=excluded.confirmed_state,candidate_days=excluded.candidate_days,confirmation_days=excluded.confirmation_days""",
        (as_of, raw, confirmed, candidate_days, required))
    history = state_conn.execute("""SELECT confirmed_state FROM market_state_history WHERE trade_date<=?
        ORDER BY trade_date DESC""", (as_of,)).fetchall()
    duration = 0
    for row in history:
        if row["confirmed_state"] != confirmed:
            break
        duration += 1
    state_conn.commit()
    report["state"].update({"current": confirmed, "confirmed_state": confirmed,
        "duration_days": duration, "candidate_days": candidate_days, "confirmation_days": required})


def market_state_view(report: dict) -> dict:
    confirmed = report["state"]["confirmed_state"]
    raw = report["state"]["raw_state"]
    breadth = report["breadth"]
    indexes = report["benchmarks"]
    above20 = sum(item["above_ma20"] for item in indexes.values())
    above60 = sum(item["above_ma60"] for item in indexes.values())
    if confirmed == "DEFENSIVE":
        analysis = (f"宽基仍处在偏弱结构：仅 {above20}/6 个宽基站上 MA20、{above60}/6 个站上 MA60；"
                    f"全 A 上涨占比 {breadth['advance_ratio']:.2f}%，中期趋势尚未形成广泛支撑。"
                    f"波动风险分 {report['state']['volatility_score']:.2f}，参与上应优先控制节奏与仓位。")
    elif confirmed == "OFFENSIVE":
        analysis = (f"宽基趋势与市场广度同步改善，{above20}/6 个宽基站上 MA20，"
                    f"全 A 上涨占比 {breadth['advance_ratio']:.2f}%。波动风险分 {report['state']['volatility_score']:.2f}，"
                    "市场具备更广泛的趋势参与条件。")
    elif confirmed == "RECOVERY_WATCH":
        analysis = (f"短期修复正在形成：{above20}/6 个宽基回到 MA20 上方，全 A 上涨占比 {breadth['advance_ratio']:.2f}%。"
                    f"但仅 {above60}/6 个宽基站上 MA60，中期趋势仍需后续广度和量能验证。")
    else:
        analysis = (f"市场尚未形成一致趋势，{above20}/6 个宽基站上 MA20、{above60}/6 个站上 MA60；"
                    f"全 A 上涨占比 {breadth['advance_ratio']:.2f}%。"
                    + ("板块轮动较快，机会更偏局部。" if report["state"]["rotation_score"] >= 60 else "强弱分化明显，机会集中在少数结构较强的方向。"))
    risks = []
    if report["state"]["volatility_score"] >= 75:
        risks.append("高波动")
    if above20 <= 2:
        risks.append("多数宽基弱于MA20")
    if breadth["advance_ratio"] < 45:
        risks.append("市场广度偏弱")
    transition = None
    if raw != confirmed:
        transition = f"潜在变化：{state_label(raw, report)}信号，第 {report['state']['candidate_days']}/{report['state']['confirmation_days']} 个确认日"
    llm_analysis = str(report.get("llm", {}).get("analysis", "")).strip()
    return {"label": state_label(confirmed, report), "raw_label": raw, "duration_days": report["state"]["duration_days"],
        "risk_tags": risks or ["暂无额外风险标签"], "analysis": llm_analysis or analysis,
        "analysis_source": "llm" if llm_analysis else "rule_fallback", "transition": transition}


def market_state_explainer(report: dict) -> dict:
    state = report["state"]
    breadth = report["breadth"]
    indexes = report["benchmarks"]
    above20 = sum(item["above_ma20"] for item in indexes.values())
    above60 = sum(item["above_ma60"] for item in indexes.values())
    thresholds = CONFIG["state_thresholds"]
    defensive_breadth = breadth["advance_ratio"] <= thresholds["defensive"]["breadth_max"]
    defensive_risk = state["volatility_score"] >= thresholds["defensive"]["volatility_min"] and state["rotation_score"] >= thresholds["defensive"]["rotation_min"]
    offensive = state["trend_score"] >= thresholds["offensive"]["trend_min"] and breadth["advance_ratio"] >= thresholds["offensive"]["breadth_min"] and state["volatility_score"] <= thresholds["offensive"]["volatility_max"]
    recovery = above20 >= 3 and above60 <= 2 and breadth["advance_ratio"] >= 50
    return {
        "current": {
            "label": state_label(state["confirmed_state"], report),
            "raw_label": state_label(state["raw_state"], report),
            "metrics": [
                {"label": "趋势分", "value": state["trend_score"]}, {"label": "波动风险分", "value": state["volatility_score"]},
                {"label": "广度分", "value": state["breadth_score"]}, {"label": "轮动分", "value": state["rotation_score"]},
                {"label": "站上 MA20 宽基", "value": f"{above20}/6"}, {"label": "全 A 上涨占比", "value": f"{breadth['advance_ratio']:.2f}%"},
            ],
            "matched": [
                "全 A 上涨占比不高于 45，满足弱势广度条件" if defensive_breadth else "全 A 上涨占比高于弱势广度阈值",
                "高波动与高轮动同时满足弱势风险条件" if defensive_risk else "未同时满足高波动与高轮动的弱势风险条件",
            ],
        },
        "states": [
            {"name": "弱势下行", "rule": "全 A 上涨占比 ≤ 45；或波动风险分 ≥ 75 且轮动分 ≥ 65", "confirm": "2/3 日", "meaning": "趋势与广度偏弱，优先关注风险是否收敛。", "active": state["confirmed_state"] == "DEFENSIVE"},
            {"name": "趋势扩散", "rule": "趋势分 ≥ 65、全 A 上涨占比 ≥ 60、波动风险分 ≤ 60", "confirm": "3/3 日", "meaning": "宽基趋势与市场广度同步改善。", "active": state["confirmed_state"] == "OFFENSIVE", "matched": offensive},
            {"name": "修复观察", "rule": "至少 3/6 宽基站上 MA20、至多 2/6 站上 MA60、全 A 上涨占比 ≥ 50", "confirm": "3/3 日", "meaning": "短期修复出现，但中期趋势尚待确认。", "active": state["confirmed_state"] == "RECOVERY_WATCH", "matched": recovery},
            {"name": "结构性强势", "rule": "未触发上述状态，且轮动分 < 60", "confirm": "2/3 日", "meaning": "整体未形成一致趋势，机会偏向局部强势结构。", "active": state["confirmed_state"] == "SELECTIVE" and state["rotation_score"] < 60},
            {"name": "震荡轮动", "rule": "未触发上述状态，且轮动分 ≥ 60", "confirm": "2/3 日", "meaning": "板块更替较快，持续性较弱。", "active": state["confirmed_state"] == "SELECTIVE" and state["rotation_score"] >= 60},
        ],
        "score_rules": [
            {"name": "趋势分", "rule": "六个宽基分别计算：是否站上 MA20、MA20 五日斜率的历史分位、MA20 是否高于 MA60、20 日收益的历史分位；每个宽基取四项均值，全体取中位数。", "direction": "越高代表趋势越强。"},
            {"name": "波动风险分", "rule": "六个宽基分别计算 ATR14、10 日实现波动率、10 日振幅的历史分位；每个宽基取三项均值，全体取中位数。", "direction": "越高代表风险越高。"},
            {"name": "广度分", "rule": "全 A 上涨占比、站上 MA20 比例、站上 MA60 比例，以及 60 日新高减新低比例（映射到 0–100）四项等权平均。", "direction": "越高代表上涨扩散更广。"},
            {"name": "轮动分", "rule": "按行业、概念、风格、指数成分分别比较当日与前一日 Top10 的重合度；以 1 减去平均重合度后换算为分数。", "direction": "越高代表板块更替越快。"},
        ],
    }


def market_llm_context(report: dict) -> dict:
    indexes = report["benchmarks"]
    above20 = sum(item["above_ma20"] for item in indexes.values())
    above60 = sum(item["above_ma60"] for item in indexes.values())
    index_details = {
        name: {
            "收盘": item["close"], "1日收益%": item["return_1"], "5日收益%": item["return_5"],
            "20日收益%": item["return_20"], "站上MA20": item["above_ma20"], "站上MA60": item["above_ma60"],
            "MA20五日斜率%": item["ma20_slope_5"], "量比20日均量": item["volume_ratio_20"],
            "ATR14%": item["atr14_pct"],
        }
        for name, item in report["benchmarks"].items()
    }
    state = report["state"]
    return {
        "已确认状态": state_label(state["confirmed_state"], report),
        "持续交易日": state["duration_days"],
        "原始状态": state_label(state["raw_state"], report),
        "潜在变化确认进度": f"{state['candidate_days']}/{state['confirmation_days']}",
        "市场四维分数": {key: state[key] for key in ("trend_score", "volatility_score", "breadth_score", "rotation_score")},
        "宽基覆盖摘要": {"站上MA20的宽基数量": f"{above20}/6", "站上MA60的宽基数量": f"{above60}/6"},
        "全A广度": report["breadth"],
        "宽基指标": index_details,
    }


def parse_llm_json_object(content: str) -> dict:
    """Accept JSON Output wrapped in a Markdown fence, but reject non-object replies."""
    text = str(content or "").strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.endswith("```"):
            text = text[:-3].strip()
    if not text.startswith("{"):
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            text = text[start:end + 1]
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("LLM response is not a JSON object")
    return parsed


def extract_market_analysis(content: str) -> tuple[str, bool]:
    """Prefer strict JSON, while tolerating a malformed wrapper from the provider."""
    try:
        parsed = parse_llm_json_object(content)
        return str(parsed.get("analysis", "")).strip(), False
    except (json.JSONDecodeError, ValueError):
        text = str(content or "").strip()
        if "</think>" in text:
            text = text.rsplit("</think>", 1)[-1].strip()
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
        match = re.search(r'"analysis"\s*:\s*"((?:\\.|[^"\\])*)"', text, flags=re.DOTALL)
        if match:
            try:
                return json.loads('"' + match.group(1) + '"').strip(), True
            except json.JSONDecodeError:
                return match.group(1).replace("\\n", "").strip(), True
        if not text.startswith("{"):
            return text.strip(), True
        return "", True


def market_watch_sentence(report: dict) -> str:
    state = report["state"]["confirmed_state"]
    if state == "DEFENSIVE":
        return "接下来重点观察市场广度能否连续改善、更多宽基能否重回 MA20 上方，以及波动是否回落。"
    if state == "OFFENSIVE":
        return "接下来重点观察广度能否维持、宽基趋势是否继续扩散，以及波动是否保持可控。"
    if state == "RECOVERY_WATCH":
        return "接下来重点观察上涨广度能否延续、更多宽基能否站稳 MA20，以及波动是否同步收敛。"
    return "接下来重点观察广度与均线结构能否同步改善，以及波动是否回落到更稳定的区间。"


def validate_market_analysis(analysis: str, report: dict) -> None:
    indexes = report["benchmarks"]
    above20 = sum(item["above_ma20"] for item in indexes.values())
    above60 = sum(item["above_ma60"] for item in indexes.values())
    if any(term in analysis for term in ("买入", "卖出", "仓位", "止损", "个股建议", "资金", "情绪", "赚钱效应", "承接", "持股风险", "空头主导")):
        raise ValueError("analysis contains prohibited trading or unsupported narrative language")
    if above20 and re.search(r"(全部|全线|均).{0,8}(失守|跌破|低于).{0,8}MA20", analysis, flags=re.IGNORECASE):
        raise ValueError("analysis overstates MA20 coverage")
    if above60 and re.search(r"(全部|全线|均).{0,8}(失守|跌破|低于).{0,8}MA60", analysis, flags=re.IGNORECASE):
        raise ValueError("analysis overstates MA60 coverage")


def call_market_llm_analysis(report: dict) -> dict:
    """Return a constrained natural-language interpretation without affecting regime logic."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return {"status": "skipped", "reason": "DEEPSEEK_API_KEY missing", "analysis": ""}
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    system_prompt = (
        "你是A股市场环境解读助手。只根据用户提供的结构化数据，用90-150字解释当前市场表现，"
        "帮助读者理解趋势、广度、波动和强弱分化。"
        "严格采用“主结论 → 两至三个关键证据 → 市场结构含义 → 后续观察”的顺序。"
        "证据优先用宽基均线覆盖摘要、全A广度、波动和近期强弱分化；均线覆盖必须使用输入给出的“X/6”精确表述，"
        "不得把非全量事实写成“全部”“全线”或“均”。除非解释明显分化，不得列举单个指数，"
        "更不得逐一播报指数。页面已展示状态标签和持续天数，不要复述它们，也不要写“后续观察”“关注”“留意”之类的结尾句。"
        "轮动分高才表示轮动快；轮动分低仅表示头部板块重合度较高或持续性较强，不能写成“缺乏轮动”。"
        "已确认状态、持续天数和潜在变化进度是脚本确定的事实，必须原样尊重，不能改写或重新判定。"
        "只能引用输入中的数字和事实；不得联网、不得引入新闻/政策/资金流等外部信息，也不得使用“资金”“情绪”“赚钱效应”“承接”“持股风险”等未经输入支持的叙事，不得预测涨跌。"
        "不得给出具体买卖、仓位、止损或个股建议。语言应连贯易懂，不要使用项目符号。"
        "只返回这一段正文，不要 JSON、标题、项目符号或解释。"
    )
    retries = int(CONFIG.get("reporting", {}).get("llm_max_retries", 2))
    max_tokens = int(CONFIG.get("reporting", {}).get("llm_max_tokens", 400))
    last_error = "unknown"
    for _ in range(retries):
        try:
            response = requests.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": json.dumps(market_llm_context(report), ensure_ascii=False)},
                    ],
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=60,
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"].get("content", "")
            analysis, tolerant_parse = extract_market_analysis(content)
            if not 30 <= len(analysis) <= 220:
                raise ValueError("analysis length outside 30-220 characters")
            if analysis.rstrip().endswith(("：", "，", "、", "和", "与", "及", "只")):
                raise ValueError("analysis appears to end mid-sentence")
            validate_market_analysis(analysis, report)
            if "接下来重点观察" not in analysis:
                analysis = analysis.rstrip("。") + "。" + market_watch_sentence(report)
            analysis = re.sub(r"[，；]\s*。", "。", analysis)
            if len(analysis) > 280:
                raise ValueError("analysis with observation sentence exceeds 280 characters")
            return {"status": "success", "provider": "deepseek", "model": model,
                "parse_mode": "tolerant" if tolerant_parse else "json", "analysis": analysis}
        except Exception as exc:
            last_error = str(exc)[:300]
    return {"status": "failed", "reason": last_error, "provider": "deepseek", "model": model, "analysis": ""}


def build_market_context(report: dict, sectors: list[dict], stocks: list[dict], state_conn: sqlite3.Connection, as_of: str) -> dict:
    kinds = ("industry_sw_l1", "industry_sw_l2", "gn", "fg")
    rankings = {kind: [row for row in sectors if row["block_type"] == kind][:20] for kind in kinds}
    selected = {(kind, row["block_name"]) for kind, rows in rankings.items() for row in rows}
    history = pd.read_sql_query("""SELECT trade_date,block_kind,block_name,rank_20,rank_5,return_1,return_5,return_20,
        relative_strength_5,relative_strength_20,advance_ratio,volume_activity,above_ma20_ratio,sector_state,history_basis
        FROM sector_daily_metrics WHERE trade_date<=? ORDER BY trade_date""", state_conn, params=(as_of,))
    sector_history = {kind: [] for kind in kinds}
    for kind, name in sorted(selected):
        rows = history[(history.block_kind == kind) & (history.block_name == name)]
        points = [{key: (None if pd.isna(value) else value) for key, value in row.items()} for row in rows.tail(20).to_dict("records")]
        sector_history[kind].append({"block_name": name, "points": points})
    rank_matrix = {kind: {"rank_20": [], "rank_5": []} for kind in kinds}
    for kind in kinds:
        for rank_field in ("rank_20", "rank_5"):
            subset = history[(history.block_kind == kind) & (history[rank_field] <= 20)]
            for date, rows in subset.groupby("trade_date", sort=True):
                rank_matrix[kind][rank_field].append({"trade_date": date, "ranks": [
                    {"rank": int(getattr(row, rank_field)), "block_name": row.block_name, "history_basis": row.history_basis}
                    for row in rows.sort_values(rank_field).itertuples(index=False)
                ]})
    top_scopes = []
    for kind in ("industry_sw_l2", "gn"):
        for row in rankings[kind][:5]:
            top_scopes.append({"scope_type": kind, "scope_name": row["block_name"], "advance_ratio": row["up_breadth"],
                "median_return_1": row["median_return_1"], "above_ma20_ratio": row["above_ma20_ratio"],
                "above_ma60_ratio": row["above_ma60_ratio"], "new_high_ratio": row["new_high_ratio"], "member_count": row["member_count"]})
    sector_leaders = report.get("sector_leaders", {})
    baseline_count = state_conn.execute("SELECT count(DISTINCT trade_date) FROM sector_daily_metrics WHERE trade_date<=? AND history_basis='current_snapshot_backfill'", (as_of,)).fetchone()[0]
    return {"meta": {**report["meta"], "generated_for": "market_dashboard", "history_window_days": 20,
        "sector_history_note": "当前成分快照回填（非严格点时）" if baseline_count else "每日快照（点时）",
        "baseline_history_days": baseline_count},
        "market_state": market_state_view(report), "market_state_explainer": market_state_explainer(report), "indexes": report["benchmarks"],
        "breadth": {"all_a": report["breadth"], "top_scopes": top_scopes},
        "sector_rankings": rankings, "sector_history": sector_history, "sector_rank_matrix": rank_matrix, "sector_leaders": sector_leaders}


def write_dashboard_index(market_latest: str | None, market_available: list[str]) -> None:
    index = {"market": {"latest": market_latest, "available": market_available}}
    # Every publisher owns only its module.  Rebuild the other known module
    # indexes from their published packages so a market refresh cannot erase
    # an independently published valuation index.
    for kind in ("signals", "vcp", "valuation"):
        dates = sorted(path.stem.rsplit("_", 1)[-1] for path in DASHBOARD_DATA_DIR.glob(f"*/{kind}_context_*.js"))
        if dates:
            index[kind] = {"latest": dates[-1], "available": dates}
    (DASHBOARD_DATA_DIR / "index.js").write_text(
        "window.QUANT_DASHBOARD_INDEX = " + json.dumps(index, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )


def write_dashboard_data(report: dict, sectors: list[dict], stocks: list[dict], state_conn: sqlite3.Connection, as_of: str) -> None:
    DATA_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    stamp = today_stamp(as_of)
    context = build_market_context(report, sectors, stocks, state_conn, as_of)
    (DATA_OUTPUT_DIR / f"market_context_{stamp}.json").write_text(json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8")
    available = sorted(path.stem.rsplit("_", 1)[-1] for path in DATA_OUTPUT_DIR.glob("market_context_*.json") if path.stem.rsplit("_", 1)[-1] >= DASHBOARD_START_DATE)
    latest = available[-1] if available else None
    (DATA_OUTPUT_DIR / "latest.json").write_text(json.dumps({"latest": latest, "available": available}, ensure_ascii=False, indent=2), encoding="utf-8")
    month_dir = DASHBOARD_DATA_DIR / f"20{stamp[:4]}"
    month_dir.mkdir(parents=True, exist_ok=True)
    (month_dir / f"market_context_{stamp}.js").write_text(
            "window.QUANT_DASHBOARD_MARKET_CONTEXTS = window.QUANT_DASHBOARD_MARKET_CONTEXTS || {};\n"
            f"window.QUANT_DASHBOARD_MARKET_CONTEXTS[{json.dumps(stamp)}] = "
            + json.dumps(context, ensure_ascii=False) + ";\n", encoding="utf-8"
    )
    for path in DASHBOARD_DATA_DIR.glob("market_context_*.js"):
        path.unlink()
    write_dashboard_index(latest, available)
    legacy_path = DASHBOARD_DATA_DIR / "market_context.js"
    if legacy_path.exists():
        legacy_path.unlink()


def publish_dashboard_archives() -> list[str]:
    DASHBOARD_DATA_DIR.mkdir(parents=True, exist_ok=True)
    available = sorted(path.stem.rsplit("_", 1)[-1] for path in DATA_OUTPUT_DIR.glob("market_context_*.json") if path.stem.rsplit("_", 1)[-1] >= DASHBOARD_START_DATE)
    for date in available:
        payload = json.loads((DATA_OUTPUT_DIR / f"market_context_{date}.json").read_text(encoding="utf-8"))
        month_dir = DASHBOARD_DATA_DIR / f"20{date[:4]}"; month_dir.mkdir(parents=True, exist_ok=True)
        (month_dir / f"market_context_{date}.js").write_text("window.QUANT_DASHBOARD_MARKET_CONTEXTS = window.QUANT_DASHBOARD_MARKET_CONTEXTS || {};\n" + f"window.QUANT_DASHBOARD_MARKET_CONTEXTS[{json.dumps(date)}] = " + json.dumps(payload, ensure_ascii=False) + ";\n", encoding="utf-8")
    for path in DASHBOARD_DATA_DIR.glob("market_context_*.js"):
        path.unlink()
    for path in DASHBOARD_DATA_DIR.glob("*/market_context_*.js"):
        if path.stem.rsplit("_", 1)[-1] < DASHBOARD_START_DATE:
            path.unlink()
    latest = available[-1] if available else None
    write_dashboard_index(latest, available)
    return available


def write_outputs(report: dict, sectors: list[dict], stocks: list[dict], concepts: list[dict], state_conn: sqlite3.Connection, as_of: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = today_stamp(as_of)
    (OUTPUT_DIR / f"market_regime_{stamp}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(OUTPUT_DIR / f"sector_heat_{stamp}.csv", sectors)
    write_csv(OUTPUT_DIR / f"stock_strength_{stamp}.csv", stocks)
    write_csv(OUTPUT_DIR / f"concept_strength_{stamp}.csv", concepts)
    write_markdown(report, sectors, stocks, as_of)
    write_dashboard_data(report, sectors, stocks, state_conn, as_of)


def persist_sector_metrics(conn: sqlite3.Connection, as_of: str, sectors: list[dict], write_legacy: bool) -> None:
    conn.execute("DELETE FROM sector_daily_metrics WHERE trade_date=?", (as_of,))
    if write_legacy:
        conn.execute("DELETE FROM block_metrics WHERE trade_date=?", (as_of,))
        conn.executemany(
            "INSERT INTO block_metrics(trade_date,block_kind,block_name,heat_score,rank) VALUES(?,?,?,?,?)",
            [(as_of, row["block_type"], row["block_name"], row["relative_strength_20"], row["rank_20"]) for row in sectors],
        )
    conn.executemany(
        """INSERT INTO sector_daily_metrics(trade_date,block_kind,block_name,member_count,rank_20,rank_5,
            return_1,return_5,return_10,return_20,relative_strength_5,relative_strength_20,median_return_1,
            advance_ratio,volume_activity,above_ma20_ratio,above_ma60_ratio,new_high_ratio,strong_stock_density,
            sector_state,history_basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(as_of, row["block_type"], row["block_name"], row["member_count"], row["rank_20"], row["rank_5"],
          row["return_1"], row["return_5"], row["return_10"], row["return_20"], row["relative_strength_5"],
          row["relative_strength_20"], row["median_return_1"], row["up_breadth"], row["volume_activity"],
          row["above_ma20_ratio"], row["above_ma60_ratio"], row["new_high_ratio"], row["strong_stock_density"],
          row["sector_state"], row["history_basis"])
         for row in sectors],
    )
    conn.commit()


def save_block_metrics(conn: sqlite3.Connection, as_of: str, sectors: list[dict]) -> None:
    persist_sector_metrics(conn, as_of, sectors, write_legacy=True)


def main() -> int:
    load_local_env()
    parser = argparse.ArgumentParser(description="Independent TDX market regime and block heat module")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "update"):
        cmd = sub.add_parser(name); cmd.add_argument("--date"); cmd.add_argument("--lookback", type=int, default=CONFIG["data"]["initial_lookback_days"]); cmd.add_argument("--max-codes", type=int); cmd.add_argument("--resume", action="store_true")
    run = sub.add_parser("run"); run.add_argument("--date"); run.add_argument("--no-llm", action="store_true")
    sub.add_parser("status"); sub.add_parser("publish-dashboard")
    args = parser.parse_args()
    if args.command in {"init", "update"}:
        if args.command == "update" and args.lookback == CONFIG["data"]["initial_lookback_days"]:
            args.lookback = CONFIG["data"]["refresh_lookback_days"]
        snapshot = MarketDataService(CONFIG).ensure_ready(
            as_of=args.date,
            profile="market_regime",
            lookback=args.lookback,
            run_type=args.command,
            max_codes=args.max_codes,
        )
        print(json.dumps(snapshot, ensure_ascii=False, indent=2))
        return 0 if snapshot["status"] == "READY" or args.max_codes else 2
    if args.command == "status":
        print(json.dumps(MarketDataService.latest_status(), ensure_ascii=False, indent=2))
        return 0
    if args.command == "publish-dashboard":
        print(json.dumps({"available": publish_dashboard_archives()}, ensure_ascii=False)); return 0
    conn = connect_db(); create_schema(conn)
    state_conn = connect_state_db()
    try:
        as_of = resolve_as_of(args.date)
        try:
            report, sectors, stocks, concepts = compute_metrics(conn, state_conn, as_of)
        except RuntimeError as exc:
            print(f"数据未就绪：{exc}", file=sys.stderr)
            return 2
        confirm_market_state(state_conn, as_of, report)
        report["llm"] = call_market_llm_analysis(report) if not args.no_llm else {
            "status": "skipped", "reason": "disabled_by_flag", "analysis": ""
        }
        save_block_metrics(state_conn, as_of, sectors); write_outputs(report, sectors, stocks, concepts, state_conn, as_of)
        print(json.dumps(report["state"], ensure_ascii=False, indent=2)); return 0
    finally:
        state_conn.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
