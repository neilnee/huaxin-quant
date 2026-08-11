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
DASHBOARD_START_DATE = "260506"
MX_SEARCH_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"


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
        median_return_1 REAL NOT NULL, advance_ratio REAL NOT NULL, advance_ratio_5 REAL, volume_activity REAL NOT NULL,
        above_ma20_ratio REAL NOT NULL, above_ma60_ratio REAL NOT NULL, new_high_ratio REAL NOT NULL,
        strong_stock_density REAL NOT NULL, sector_state TEXT NOT NULL,
        history_basis TEXT NOT NULL DEFAULT 'point_in_time',
        PRIMARY KEY (trade_date, block_kind, block_name)
    )""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(sector_daily_metrics)")}
    added_rank_percentiles = "rank_pct_20" not in columns
    added_breadth_5 = "advance_ratio_5" not in columns
    added_health_model = "sector_health_level" not in columns
    if "history_basis" not in columns:
        conn.execute("ALTER TABLE sector_daily_metrics ADD COLUMN history_basis TEXT NOT NULL DEFAULT 'point_in_time'")
    daily_columns = {
        "rank_1": "INTEGER NOT NULL DEFAULT 0",
        "relative_strength_1": "REAL NOT NULL DEFAULT 0",
        "daily_strong_density": "REAL NOT NULL DEFAULT 0",
        "daily_score": "REAL NOT NULL DEFAULT 0",
        "advance_ratio_5": "REAL",
        "rank_pct_20": "REAL NOT NULL DEFAULT 1",
        "rank_pct_5": "REAL NOT NULL DEFAULT 1",
        "sector_phase": "TEXT NOT NULL DEFAULT 'NONE'",
        "sector_health": "TEXT NOT NULL DEFAULT '数据不足'",
        "sector_health_level": "INTEGER NOT NULL DEFAULT 0",
        "sector_health_score": "REAL NOT NULL DEFAULT 0",
        "short_pulse": "INTEGER NOT NULL DEFAULT 0",
        "sector_policy_tier": "TEXT NOT NULL DEFAULT 'D'",
        "data_status": "TEXT NOT NULL DEFAULT 'BUILDING'",
    }
    for name, definition in daily_columns.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE sector_daily_metrics ADD COLUMN {name} {definition}")
    if added_rank_percentiles:
        conn.execute("""UPDATE sector_daily_metrics AS target SET rank_pct_20 =
            1.0 * rank_20 / (SELECT count(*) FROM sector_daily_metrics AS peers
                WHERE peers.trade_date=target.trade_date AND peers.block_kind=target.block_kind),
            rank_pct_5 = 1.0 * rank_5 / (SELECT count(*) FROM sector_daily_metrics AS peers
                WHERE peers.trade_date=target.trade_date AND peers.block_kind=target.block_kind),
            sector_health = '数据不足',
            data_status = CASE WHEN history_basis='current_snapshot_backfill' THEN 'BACKFILL' ELSE 'READY' END""")
    if added_breadth_5:
        # Daily breadth cannot be converted into five-day member breadth.
        # Historical health stays unknown until point-in-time rows accumulate.
        conn.execute("UPDATE sector_daily_metrics SET advance_ratio_5=NULL, sector_health='数据不足'")
    if added_health_model:
        conn.execute("UPDATE sector_daily_metrics SET sector_health='数据不足', sector_health_level=0, sector_health_score=0")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sector_daily_lookup ON sector_daily_metrics(block_kind, block_name, trade_date)")
    conn.execute("""CREATE TABLE IF NOT EXISTS market_state_history (
        trade_date TEXT PRIMARY KEY, raw_state TEXT NOT NULL, confirmed_state TEXT NOT NULL,
        candidate_days INTEGER NOT NULL, confirmation_days INTEGER NOT NULL
    )""")
    conn.commit()
    return conn


def resolve_market_snapshot(conn: sqlite3.Connection, as_of: str, allow_fallback: bool = False) -> tuple[str, str]:
    """Resolve a complete reference snapshot and label any historical fallback."""
    universe_dates = {row[0] for row in conn.execute("SELECT DISTINCT trade_date FROM universe_members")}
    block_dates = {row[0] for row in conn.execute("SELECT DISTINCT snapshot_date FROM block_members")}
    industry_dates = {row[0] for row in conn.execute("SELECT DISTINCT snapshot_date FROM stock_industries")}
    available = sorted(universe_dates & block_dates & industry_dates)
    if as_of in available:
        return as_of, "point_in_time"
    if not allow_fallback:
        raise RuntimeError(f"目标日成分快照缺失: {as_of}")
    if not available:
        raise RuntimeError("没有可用于历史回填的完整成分快照")
    snapshot = min(available, key=lambda value: (abs((pd.Timestamp(value) - pd.Timestamp(as_of)).days), value))
    return snapshot, "current_snapshot_backfill"


def percentile_score(value: float, values: pd.Series) -> float:
    valid = values.dropna()
    if valid.empty or not math.isfinite(value):
        return 50.0
    return round(float((valid <= value).mean() * 100), 2)


def finalize_sector_rankings(records: list[dict]) -> list[dict]:
    """Attach independent 1/5/20-day ranks and comparable rank percentiles."""
    settings = CONFIG["daily_mainline"]
    weights = settings["weights"]
    sector_rows = []
    for kind in sorted({row["block_type"] for row in records}):
        rows = [row for row in records if row["block_type"] == kind]
        for rank, row in enumerate(sorted(rows, key=lambda item: item["relative_strength_20"], reverse=True), 1):
            row["rank_20"] = rank
            row["rank"] = rank
        for rank, row in enumerate(sorted(rows, key=lambda item: item["relative_strength_5"], reverse=True), 1):
            row["rank_5"] = rank
        total = max(1, len(rows))
        for row in rows:
            row["rank_pct_20"] = round(row["rank_20"] / total, 6)
            row["rank_pct_5"] = round(row["rank_5"] / total, 6)
        rel1_values = pd.Series([row["relative_strength_1"] for row in rows], dtype=float)
        volume_values = pd.Series([row["volume_activity"] for row in rows], dtype=float)
        density_values = pd.Series([row["daily_strong_density"] for row in rows], dtype=float)
        for row in rows:
            components = {
                "relative_strength_1": percentile_score(row["relative_strength_1"], rel1_values),
                "up_breadth": row["up_breadth"],
                "volume_activity": percentile_score(row["volume_activity"], volume_values),
                "daily_strong_density": percentile_score(row["daily_strong_density"], density_values),
            }
            row["daily_score"] = round(sum(weights[key] * components[key] for key in weights), 2)
        for rank, row in enumerate(sorted(rows, key=lambda item: (item["daily_score"], item["relative_strength_1"]), reverse=True), 1):
            row["rank_1"] = rank
        sector_rows.extend(sorted(rows, key=lambda item: item["rank_20"]))
    return sector_rows


HEALTH_LABELS = {
    "NONE": {2: "蓄势增强", 1: "温和改善", 0: "震荡观察", -1: "观察走弱", -2: "弱势恶化"},
    "转强": {2: "转强加速", 1: "转强延续", 0: "转强停滞", -1: "转强受阻", -2: "转强失败"},
    "主线": {2: "扩散增强", 1: "主线健康", 0: "主线稳定", -1: "主线降温", -2: "高位分歧"},
    "退潮": {2: "强力修复", 1: "温和修复", 0: "弱势企稳", -1: "退潮延续", -2: "退潮加速"},
}


def sector_health(phase: str, row: dict, prior: pd.DataFrame, data_status: str) -> dict:
    """Describe direction inside an already-decided phase; never decide the phase."""
    if data_status != "READY" or prior.empty:
        return {"sector_health": "数据不足", "sector_health_level": 0, "sector_health_score": 0.0}
    latest = prior.iloc[0]
    settings = CONFIG["sector_health"]
    thresholds = settings["change_thresholds"]
    weights = settings["phase_weights"][phase]
    signals = []
    for field, weight in weights.items():
        current = row.get(field)
        previous = latest.get(field)
        if current is None or previous is None or pd.isna(current) or pd.isna(previous):
            continue
        improvement = float(previous) - float(current) if field.startswith("rank_pct_") else float(current) - float(previous)
        threshold = float(thresholds[field])
        direction = 1 if improvement >= threshold else -1 if improvement <= -threshold else 0
        signals.append((direction, float(weight)))
    if not signals:
        return {"sector_health": "数据不足", "sector_health_level": 0, "sector_health_score": 0.0}
    score = sum(direction * weight for direction, weight in signals) / sum(weight for _, weight in signals)
    levels = settings["level_thresholds"]
    if score >= levels["strong_improvement"]:
        level = 2
    elif score >= levels["improvement"]:
        level = 1
    elif score <= levels["strong_deterioration"]:
        level = -2
    elif score <= levels["deterioration"]:
        level = -1
    else:
        level = 0
    return {
        "sector_health": HEALTH_LABELS[phase][level],
        "sector_health_level": level,
        "sector_health_score": round(float(score), 4),
    }


def sector_policy_tier(phase: str, data_status: str) -> str:
    """Keep the shadow policy stage-only; phase health is descriptive."""
    if data_status != "READY":
        return "D"
    return "A" if phase == "主线" else "B" if phase == "转强" else "D"


def legacy_sector_state(phase: str, short_pulse: bool, data_status: str) -> str:
    """Map only lifecycle facts to the legacy enum consumed by the signal layer."""
    if data_status != "READY":
        return "历史积累中"
    if phase == "退潮":
        return "弱势退潮"
    if phase == "主线":
        return "持续主线"
    if phase == "转强":
        return "强势初现"
    if short_pulse:
        return "轮动脉冲"
    return "观察中"


def classify_sector_phase(row: dict, prior: pd.DataFrame) -> dict:
    """Decide lifecycle first, then describe direction within that phase."""
    settings = CONFIG["sector_phase"]
    history_days = int(settings["history_days"])
    data_status = "BACKFILL" if row.get("history_basis") == "current_snapshot_backfill" else "READY"
    short_pulse = bool(
        row["rank_pct_5"] <= settings["short_pulse_rank_percentile_max"]
        and row["rank_pct_20"] > settings["entry_rank_percentile_max"]
    )
    if len(prior) < history_days:
        data_status = "BUILDING" if data_status == "READY" else data_status
        phase = "NONE"
    else:
        prior = prior.head(history_days)
        prior_entry_days = int((prior.rank_pct_20 <= settings["entry_rank_percentile_max"]).sum())
        latest = prior.iloc[0]
        latest_phase = str(latest.get("sector_phase") or "NONE")
        latest_legacy = str(latest.get("sector_state") or "")
        in_entry_zone = row["rank_pct_20"] <= settings["entry_rank_percentile_max"]
        in_exit_zone = row["rank_pct_20"] <= settings["exit_rank_percentile_max"]
        exit_days = max(1, int(settings["exit_confirmation_days"]))
        prior_exit = list(prior.rank_pct_20.head(exit_days - 1))
        outside_confirmed = (
            not in_exit_zone
            and len(prior_exit) == exit_days - 1
            and all(float(value) > settings["exit_rank_percentile_max"] for value in prior_exit)
        )
        established_latest = latest_phase == "主线" or latest_legacy in {"持续主线", "高位分歧"}
        # Only the new lifecycle field may carry a fading phase forward.  The
        # legacy weak-state enum was much broader and would over-migrate stale
        # observations during a schema upgrade.
        fading_latest = latest_phase == "退潮"
        retreat_to_observation_days = max(1, int(settings["retreat_to_observation_days"]))
        retreat_history = prior.head(retreat_to_observation_days)
        stale_fading = (
            fading_latest
            and not in_exit_zone
            and len(retreat_history) == retreat_to_observation_days
            and retreat_history.sector_phase.eq("退潮").all()
            and retreat_history.rank_pct_20.gt(settings["exit_rank_percentile_max"]).all()
        )
        had_lifecycle = (
            prior_entry_days >= int(settings["mainline_min_days"])
            or established_latest
            or fading_latest
        )
        if stale_fading:
            phase = "NONE"
        elif had_lifecycle and outside_confirmed:
            phase = "退潮"
        elif in_entry_zone and prior_entry_days >= int(settings["mainline_min_days"]):
            phase = "主线"
        elif established_latest and in_exit_zone:
            phase = "主线"
        elif in_entry_zone:
            phase = "转强"
        elif established_latest:
            phase = "主线"
        elif fading_latest:
            phase = "退潮"
        else:
            phase = "NONE"
    health = sector_health(phase, row, prior, data_status)
    tier = sector_policy_tier(phase, data_status)
    return {
        "sector_phase": phase,
        **health,
        "short_pulse": short_pulse,
        "sector_policy_tier": tier,
        "data_status": data_status,
        "sector_state": legacy_sector_state(phase, short_pulse, data_status),
    }


def classify_sector_state(row: dict, prior: pd.DataFrame) -> str:
    """Compatibility wrapper for callers that still require the legacy enum."""
    return classify_sector_phase(row, prior)["sector_state"]


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
    current["rps1_market"] = current["ret1"].rank(pct=True, method="average") * 100
    merged = blocks.merge(current, on="code", how="inner")
    market_median = {window: float(current[f"ret{window}"].median()) for window in (1, 5, 20)}
    records = []
    for (kind, name), group in merged.groupby(["block_kind", "block_name"]):
        if len(group) < 3:
            continue
        rel1 = group.ret1.median() - market_median[1]
        rel5 = group.ret5.median() - market_median[5]
        rel20 = group.ret20.median() - market_median[20]
        strong = ((group.new_high60) | (group.ret5 >= group.ret5.quantile(0.9))).mean()
        records.append({"date": trade_date, "block_type": kind, "block_name": name, "member_count": len(group),
            "return_1": round(float(group.ret1.median() * 100), 3), "return_5": round(float(group.ret5.median() * 100), 3),
            "return_10": round(float(group.ret10.median() * 100), 3), "return_20": round(float(group.ret20.median() * 100), 3),
            "relative_strength_1": round(float(rel1 * 100), 3),
            "relative_strength_5": round(float(rel5 * 100), 3), "relative_strength_20": round(float(rel20 * 100), 3),
            "volume_activity": round(float(group.volume_ratio.median()), 3),
            "up_breadth": round(float((group.ret1 > 0).mean() * 100), 2),
            "up_breadth_5": round(float((group.ret5 > 0).mean() * 100), 2),
            "daily_strong_density": round(float((group.rps1_market >= 90).mean() * 100), 2),
            "median_return_1": round(float(group.ret1.median() * 100), 3), "above_ma20_ratio": round(float((group.close > group.ma20).mean() * 100), 2),
            "above_ma60_ratio": round(float((group.close > group.ma60).mean() * 100), 2), "new_high_ratio": round(float(group.new_high60.mean() * 100), 2),
            "strong_stock_density": round(float(strong * 100), 2), "sector_state": "基线回填", "history_basis": history_basis})
    rows = finalize_sector_rankings(records)
    for row in rows:
        pulse = bool(
            row["rank_pct_5"] <= CONFIG["sector_phase"]["short_pulse_rank_percentile_max"]
            and row["rank_pct_20"] > CONFIG["sector_phase"]["entry_rank_percentile_max"]
        )
        row.update({
            "sector_phase": "NONE",
            "sector_health": "数据不足",
            "sector_health_level": 0,
            "sector_health_score": 0.0,
            "short_pulse": pulse,
            "sector_policy_tier": "D",
            "data_status": "BACKFILL",
            "sector_state": "历史积累中",
        })
    return rows


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


def continuous_core_mainlines(state_conn: sqlite3.Connection, as_of: str, sectors: list[dict]) -> list[str]:
    """Return core blocks that remain a persistent mainline for the configured days."""
    settings = CONFIG["state_thresholds"]["selective"]
    core_kinds = set(settings["mainline_block_types"])
    days = max(1, int(settings["persistent_mainline_days"]))
    continuous = {
        (row["block_type"], row["block_name"])
        for row in sectors
        if row["block_type"] in core_kinds and row["sector_state"] == "持续主线"
    }
    if not continuous or days == 1:
        return sorted(f"{kind}:{name}" for kind, name in continuous)
    previous_dates = [
        row[0]
        for row in state_conn.execute(
            """SELECT DISTINCT trade_date FROM sector_daily_metrics
            WHERE trade_date<? ORDER BY trade_date DESC LIMIT ?""",
            (as_of, days - 1),
        )
    ]
    if len(previous_dates) < days - 1:
        return []
    for trade_date in previous_dates:
        prior = {
            (row[0], row[1])
            for row in state_conn.execute(
                """SELECT block_kind,block_name FROM sector_daily_metrics
                WHERE trade_date=? AND sector_state='持续主线'""",
                (trade_date,),
            )
            if row[0] in core_kinds
        }
        continuous &= prior
        if not continuous:
            break
    return sorted(f"{kind}:{name}" for kind, name in continuous)


def classify_market_state(
    trend_score: float,
    volatility_score: float,
    breadth_score: float,
    rotation_score: float,
    above20: int,
    above60: int,
    advance_ratio: float,
) -> str:
    thresholds = CONFIG["state_thresholds"]
    if breadth_score <= thresholds["defensive"]["breadth_max"] or (
        volatility_score >= thresholds["defensive"]["volatility_min"]
        and rotation_score >= thresholds["defensive"]["rotation_min"]
    ):
        return "DEFENSIVE"
    if (
        trend_score >= thresholds["offensive"]["trend_min"]
        and breadth_score >= thresholds["offensive"]["breadth_min"]
        and volatility_score <= thresholds["offensive"]["volatility_max"]
    ):
        return "OFFENSIVE"
    if above20 >= 3 and above60 <= 2 and advance_ratio >= 50:
        return "RECOVERY_WATCH"
    if (
        volatility_score >= thresholds["consolidating"]["volatility_min"]
        and trend_score < thresholds["consolidating"]["trend_max"]
        and above20 <= thresholds["consolidating"]["above_ma20_benchmarks_max"]
        and advance_ratio >= thresholds["consolidating"]["advance_ratio_min"]
    ):
        return "CONSOLIDATING"
    return "SELECTIVE"


def compute_metrics(conn: sqlite3.Connection, state_conn: sqlite3.Connection, as_of: str, allow_snapshot_fallback: bool = False) -> tuple[dict, list[dict], list[dict], list[dict]]:
    snapshot_date, history_basis = resolve_market_snapshot(conn, as_of, allow_snapshot_fallback)
    total = conn.execute("SELECT count(*) FROM universe_members WHERE trade_date=? AND eligible=1", (snapshot_date,)).fetchone()[0]
    valid = conn.execute(
        """SELECT count(*) FROM universe_members u JOIN daily_bars b ON u.code=b.code
           WHERE u.trade_date=? AND u.eligible=1 AND b.trade_date=?""",
        (snapshot_date, as_of),
    ).fetchone()[0]
    ratio = valid / total if total else 0.0
    if ratio < CONFIG["data"]["minimum_coverage_ratio"]:
        raise RuntimeError(f"全 A 覆盖率不足: {ratio:.2%} ({valid}/{total})")
    universe = pd.read_sql_query("""SELECT b.* FROM daily_bars b JOIN universe_members u ON b.code=u.code
        WHERE u.trade_date=? AND u.eligible=1 AND b.trade_date<=?""", conn, params=(snapshot_date, as_of))
    universe = indicators(universe)
    current = universe[universe.trade_date == as_of].copy()
    current = current[current.ma60.notna()].copy()
    for window in (1, 5, 20, 60):
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
        JOIN universe_members u ON m.code=u.code WHERE m.snapshot_date=? AND u.trade_date=? AND u.eligible=1""", conn, params=(snapshot_date, snapshot_date))
    merged = blocks.merge(current, on="code", how="inner")
    market_median = {window: float(current[f"ret{window}"].median()) for window in (1, 5, 10, 20)}
    records = []
    for (kind, name), group in merged.groupby(["block_kind", "block_name"]):
        if len(group) < 3:
            continue
        rel1 = group.ret1.median() - market_median[1]
        rel5 = group.ret5.median() - market_median[5]
        rel20 = group.ret20.median() - market_median[20]
        strong = ((group.new_high60) | (group.ret5 >= group.ret5.quantile(0.9))).mean()
        records.append({"date": as_of, "block_type": kind, "block_name": name, "member_count": len(group),
            "return_1": round(float(group.ret1.median() * 100), 3), "return_5": round(float(group.ret5.median() * 100), 3),
            "return_10": round(float(group.ret10.median() * 100), 3), "return_20": round(float(group.ret20.median() * 100), 3),
            "relative_strength_1": round(float(rel1 * 100), 3),
            "relative_strength_5": round(float(rel5 * 100), 3), "relative_strength_20": round(float(rel20 * 100), 3),
            "volume_activity": round(float(group.volume_ratio.median()), 3),
            "up_breadth": round(float((group.ret1 > 0).mean() * 100), 2),
            "up_breadth_5": round(float((group.ret5 > 0).mean() * 100), 2),
            "daily_strong_density": round(float((group.rps1_market >= 90).mean() * 100), 2),
            "median_return_1": round(float(group.ret1.median() * 100), 3), "above_ma20_ratio": round(float((group.close > group.ma20).mean() * 100), 2),
            "above_ma60_ratio": round(float((group.close > group.ma60).mean() * 100), 2), "new_high_ratio": round(float(group.new_high60.mean() * 100), 2),
            "strong_stock_density": round(float(strong * 100), 2), "sector_state": "历史积累中", "history_basis": history_basis})
    sector_rows = finalize_sector_rankings(records)
    top_sets = {kind: {row["block_name"] for row in sector_rows if row["block_type"] == kind and row["rank"] <= 10} for kind in {row["block_type"] for row in sector_rows}}
    previous = pd.read_sql_query("SELECT block_kind,block_name,rank_20 AS rank FROM sector_daily_metrics WHERE trade_date=(SELECT max(trade_date) FROM sector_daily_metrics WHERE trade_date<?)", state_conn, params=(as_of,))
    overlaps = []
    for kind in top_sets:
        old = set(previous[(previous.block_kind == kind) & (previous["rank"] <= 10)].block_name)
        if old:
            overlaps.append(len(top_sets[kind] & old) / 10)
    rotation = round((1 - float(np.mean(overlaps))) * 100, 2) if overlaps else 50.0
    history = pd.read_sql_query("""SELECT trade_date,block_kind,block_name,rank_20,rank_5,rank_pct_20,rank_pct_5,
        relative_strength_5,advance_ratio AS up_breadth,advance_ratio_5 AS up_breadth_5,
        sector_state,sector_phase,sector_health,data_status
        FROM sector_daily_metrics WHERE trade_date<? ORDER BY trade_date DESC LIMIT 30000""", state_conn, params=(as_of,))
    for row in sector_rows:
        prior = history[(history.block_kind == row["block_type"]) & (history.block_name == row["block_name"])].head(5)
        row.update(classify_sector_phase(row, prior))
    trend_score, volatility_score = float(np.median(trend_scores)), float(np.median(vol_scores))
    above20 = sum(item["above_ma20"] for item in benchmark_metrics.values())
    above60 = sum(item["above_ma60"] for item in benchmark_metrics.values())
    persistent_mainlines = continuous_core_mainlines(state_conn, as_of, sector_rows)
    state = classify_market_state(
        trend_score,
        volatility_score,
        float(breadth_score),
        rotation,
        above20,
        above60,
        breadth["advance_ratio"],
    )
    security_context = pd.read_sql_query("""SELECT s.code,s.name,si.sw_industry_code
        FROM securities s LEFT JOIN stock_industries si ON s.code=si.code AND si.snapshot_date=?""", conn, params=(snapshot_date,))
    stock_frame = current.merge(security_context, on="code", how="left")
    stock_frame["sw_l2_code"] = stock_frame.sw_industry_code.fillna("").str.slice(0, 5)
    sw_l2_names = pd.read_sql_query("""SELECT industry_code,industry_name FROM industry_definitions
        WHERE snapshot_date=? AND industry_system='sw'""", conn, params=(snapshot_date,))
    stock_frame = stock_frame.merge(sw_l2_names, left_on="sw_l2_code", right_on="industry_code", how="left")
    stock_frame["rps20_industry"] = stock_frame.groupby("sw_l2_code")["ret20"].rank(pct=True, method="average") * 100
    stock_frame["rps60_industry"] = stock_frame.groupby("sw_l2_code")["ret60"].rank(pct=True, method="average") * 100
    stock_frame["volume_rank"] = stock_frame["volume_ratio"].rank(pct=True, method="average") * 100
    l2_heat = {(row["block_name"]): row for row in sector_rows if row["block_type"] == "industry_sw_l2"}
    stock_rows = []
    for row in stock_frame.itertuples(index=False):
        industry_row = l2_heat.get(row.industry_name, {}) if pd.notna(row.industry_name) else {}
        trend_position = (50 if row.close > row.ma20 else 0) + (50 if row.close > row.ma60 else 0)
        strength = 0.35 * row.rps20_market + 0.25 * row.rps60_market + 0.20 * row.rps20_industry + 0.10 * trend_position + 0.10 * min(100, row.volume_ratio * 50)
        daily_strength = 0.55 * row.rps1_market + 0.15 * row.rps5_market + 0.20 * row.volume_rank + 0.10 * trend_position
        stock_rows.append({"date": as_of, "code": row.code, "name": row.name, "close": round(float(row.close), 4), "return_1": round(float(row.ret1 * 100), 3), "return_5": round(float(row.ret5 * 100), 3), "return_20": round(float(row.ret20 * 100), 3), "return_60": round(float(row.ret60 * 100), 3), "rps1_market": round(float(row.rps1_market), 2), "rps5_market": round(float(row.rps5_market), 2), "rps20_market": round(float(row.rps20_market), 2), "rps60_market": round(float(row.rps60_market), 2), "sw_l2_code": row.sw_l2_code, "sw_l2_name": row.industry_name if pd.notna(row.industry_name) else "", "rps20_industry": round(float(row.rps20_industry), 2), "rps60_industry": round(float(row.rps60_industry), 2), "industry_relative_strength_20": industry_row.get("relative_strength_20"), "industry_rank": industry_row.get("rank_20"), "above_ma20": bool(row.close > row.ma20), "above_ma60": bool(row.close > row.ma60), "distance_high60_pct": round(float((row.close / row.high60 - 1) * 100), 3), "atr14_pct": round(float(row.atr14_pct * 100), 3), "volume_ratio_20": round(float(row.volume_ratio), 3), "daily_strength_score": round(float(daily_strength), 2), "strength_score": round(float(strength), 2)})
    stock_by_code = {row["code"]: row for row in stock_rows}
    sector_leaders = {}
    daily_sector_leaders = {}
    for (kind, name), group in merged.groupby(["block_kind", "block_name"]):
        if kind not in {"industry_sw_l1", "industry_sw_l2", "gn", "fg"}:
            continue
        members = [stock_by_code[code] for code in group.code.drop_duplicates() if code in stock_by_code]
        leaders = []
        for rank, stock in enumerate(sorted(members, key=lambda item: item["strength_score"], reverse=True)[:3], start=1):
            role = "领涨" if rank == 1 else "趋势核心" if stock["above_ma20"] and stock["above_ma60"] else "观察"
            leaders.append({key: stock[key] for key in ("code", "name", "strength_score", "rps20_market", "rps20_industry", "return_20", "above_ma20", "above_ma60", "volume_ratio_20")} | {"rank": rank, "role": role})
        sector_leaders[f"{kind}:{name}"] = leaders
        daily_leaders = []
        for rank, stock in enumerate(sorted(members, key=lambda item: item["daily_strength_score"], reverse=True)[:5], start=1):
            daily_leaders.append({key: stock[key] for key in ("code", "name", "return_1", "rps1_market", "rps5_market", "volume_ratio_20", "daily_strength_score")} | {"rank": rank})
        daily_sector_leaders[f"{kind}:{name}"] = daily_leaders
    concept_heat = {(row["block_name"]): row for row in sector_rows if row["block_type"] == "gn"}
    concept_frame = merged[merged.block_kind == "gn"].merge(stock_frame[["code", "name", "ret20"]], on="code", how="left", suffixes=("", "_stock"))
    concept_rows = []
    for (name, code), group in concept_frame.groupby(["block_name", "code"]):
        row = group.iloc[0]
        same_concept = concept_frame[concept_frame.block_name == name]
        within = float((same_concept.ret20 <= row.ret20).mean() * 100)
        heat = concept_heat.get(name, {})
        concept_rows.append({"date": as_of, "code": code, "name": row.get("name", ""), "concept_name": name, "concept_relative_strength_20": heat.get("relative_strength_20"), "concept_rank": heat.get("rank_20"), "stock_return_20": round(float(row.ret20 * 100), 3), "rps20_within_concept": round(within, 2), "stock_vs_concept_return_20": round(float((row.ret20 - same_concept.ret20.mean()) * 100), 3)})
    report = {"meta": {"run_date": as_of, "strategy_version": CONFIG["strategy_version"], "data_status": "VALID", "config": str(CONFIG_PATH), "reference_snapshot_date": snapshot_date, "history_basis": history_basis}, "state": {"current": state, "raw_state": state, "trend_score": round(trend_score, 2), "volatility_score": round(volatility_score, 2), "breadth_score": round(float(breadth_score), 2), "rotation_score": rotation, "persistent_mainline_count": len(persistent_mainlines), "persistent_mainlines": persistent_mainlines}, "benchmarks": benchmark_metrics, "breadth": breadth, "rotation": {"top10_sets": {k: sorted(v) for k, v in top_sets.items()}}, "sector_leaders": sector_leaders, "daily_sector_leaders": daily_sector_leaders}
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
        f"**市场状态：{state_label(state['current'], report)}"
        + (f" · {market_structure_tag(report)}" if market_structure_tag(report) else "") + "**  ",
        f"数据覆盖：{breadth['valid_count']}/{breadth['universe_size']}（{breadth['coverage_ratio']:.2%}）",
        "",
        "## 市场四维",
        "",
        "| 趋势 | 波动风险 | 广度 | 轮动 |",
        "|---:|---:|---:|---:|",
        f"| {state['trend_score']:.2f} | {state['volatility_score']:.2f} | {state['breadth_score']:.2f} | {state['rotation_score']:.2f} |",
    ]
    mainline = report.get("daily_mainline", {})
    if mainline:
        lines.extend([
            "", "## 今日盘面主线", "",
            f"**{mainline.get('name', '当日主线待归纳')}**  ",
            f"核心事件：{mainline.get('core_event', '—')}", "",
            mainline.get("narrative_logic", "—"), "",
            "强势板块：" + "、".join(item["block_name"] for item in mainline.get("strong_blocks", [])) if mainline.get("strong_blocks") else "强势板块：—",
            "强势个股：" + "、".join(
                f"{item['name']}（{item['code']}，{item['return_1']:.2f}%；关联：{'/'.join(item.get('selected_block_names', [])) or '—'}）"
                for item in mainline.get("strong_stocks", [])
            ) if mainline.get("strong_stocks") else "强势个股：—",
        ])
        if mainline.get("evidence"):
            lines.extend(["", "事件依据："] + [f"- {item['date']}｜{item['source']}｜{item['title']}" for item in mainline["evidence"]])
    lines.extend([
        "",
        "## 宽基与广度",
        "",
        "| 指数 | 收盘 | 20日收益 | 趋势分 | 波动分 |",
        "|---|---:|---:|---:|---:|",
    ])
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
        lines.extend(["", f"## {label}相对强度 Top 5", "", "| 排名 | 板块 | 20日相对强度 | 5日相对强度 | 5日上涨扩散 | 今日参与度 |", "|---:|---|---:|---:|---:|---:|"])
        for row in top:
            lines.append(f"| {row['rank_20']} | {row['block_name']} | {row['relative_strength_20']:.2f}% | {row['relative_strength_5']:.2f}% | {row['up_breadth_5']:.2f}% | {row['up_breadth']:.2f}% |")
    lines.extend(["", "## 全市场强度 Top 10", "", "| 代码 | 股票 | 强度分 | RPS20 | 申万二级 |", "|---|---|---:|---:|---|"])
    for row in sorted(stocks, key=lambda item: item["strength_score"], reverse=True)[:10]:
        lines.append(f"| {row['code']} | {row['name']} | {row['strength_score']:.2f} | {row['rps20_market']:.2f} | {row['sw_l2_name']} |")
    lines.extend(["", "---", "", "说明：本报告仅呈现市场、板块和相对强度事实；不改变模型二 VCP 判断，也不构成交易建议。"])
    (OUTPUT_DIR / f"market_regime_{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def state_label(state: str, report: dict) -> str:
    if state == "DEFENSIVE":
        return "防御期"
    if state == "CONSOLIDATING":
        return "弱势震荡"
    if state == "OFFENSIVE":
        return "趋势扩散"
    if state == "RECOVERY_WATCH":
        return "修复期"
    return "结构行情"


def market_structure_tag(report: dict) -> str | None:
    if report["state"].get("confirmed_state") != "SELECTIVE":
        return None
    if report["state"]["rotation_score"] >= CONFIG["state_thresholds"]["selective"]["rotation_min"]:
        return "快速轮动"
    if report["state"].get("persistent_mainline_count", 0) > 0:
        return "主线集中"
    return "结构分化"


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
    elif confirmed == "CONSOLIDATING":
        analysis = (f"普跌压力有所缓解，全 A 上涨占比回到 {breadth['advance_ratio']:.2f}%，"
                    f"但仅 {above20}/6 个宽基站上 MA20，趋势分 {report['state']['trend_score']:.2f}。"
                    f"波动风险分仍为 {report['state']['volatility_score']:.2f}，指数趋势修复尚待确认。")
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
    return {"label": state_label(confirmed, report), "raw_label": raw,
        "confirmed_state": confirmed, "candidate_state": raw, "structure_tag": market_structure_tag(report),
        "duration_days": report["state"]["duration_days"],
        "risk_tags": risks or ["暂无额外风险标签"], "analysis": llm_analysis or analysis,
        "analysis_source": "llm" if llm_analysis else "rule_fallback", "transition": transition}


def market_state_explainer(report: dict) -> dict:
    state = report["state"]
    breadth = report["breadth"]
    indexes = report["benchmarks"]
    above20 = sum(item["above_ma20"] for item in indexes.values())
    above60 = sum(item["above_ma60"] for item in indexes.values())
    thresholds = CONFIG["state_thresholds"]
    defensive_breadth = state["breadth_score"] <= thresholds["defensive"]["breadth_max"]
    defensive_risk = state["volatility_score"] >= thresholds["defensive"]["volatility_min"] and state["rotation_score"] >= thresholds["defensive"]["rotation_min"]
    offensive = state["trend_score"] >= thresholds["offensive"]["trend_min"] and state["breadth_score"] >= thresholds["offensive"]["breadth_min"] and state["volatility_score"] <= thresholds["offensive"]["volatility_max"]
    recovery = above20 >= 3 and above60 <= 2 and breadth["advance_ratio"] >= 50
    consolidating = (not defensive_breadth and not defensive_risk
        and state["volatility_score"] >= thresholds["consolidating"]["volatility_min"]
        and state["trend_score"] < thresholds["consolidating"]["trend_max"]
        and above20 <= thresholds["consolidating"]["above_ma20_benchmarks_max"]
        and breadth["advance_ratio"] >= thresholds["consolidating"]["advance_ratio_min"])
    fast_rotation = state["rotation_score"] >= thresholds["selective"]["rotation_min"]
    has_mainline = state.get("persistent_mainline_count", 0) > 0
    return {
        "current": {
            "label": state_label(state["confirmed_state"], report),
            "raw_label": state_label(state["raw_state"], report),
            "metrics": [
                {"label": "趋势分", "value": state["trend_score"]}, {"label": "波动风险分", "value": state["volatility_score"]},
                {"label": "广度分", "value": state["breadth_score"]}, {"label": "轮动分", "value": state["rotation_score"]},
                {"label": "站上 MA20 宽基", "value": f"{above20}/6"}, {"label": "全 A 上涨占比", "value": f"{breadth['advance_ratio']:.2f}%"},
                {"label": "连续主线", "value": state.get("persistent_mainline_count", 0)},
            ],
            "matched": [
                "广度分不高于 45，满足弱势广度条件" if defensive_breadth else "广度分高于弱势广度阈值",
                "高波动与高轮动同时满足弱势风险条件" if defensive_risk else "未同时满足高波动与高轮动的弱势风险条件",
            ],
        },
        "states": [
            {"name": "防御期", "rule": "广度分 ≤ 45；或波动风险分 ≥ 75 且轮动分 ≥ 65", "confirm": "2/3 日", "meaning": "趋势与广度偏弱，不开放可执行信号。", "active": state["confirmed_state"] == "DEFENSIVE"},
            {"name": "弱势震荡", "rule": "未触发防御期，波动风险分 ≥ 70、趋势分 < 50、站上 MA20 宽基 ≤ 2/6、全 A 上涨占比 ≥ 50%", "confirm": "2/3 日", "meaning": "普跌压力缓解但趋势未修复，不开放可执行信号。", "active": state["confirmed_state"] == "CONSOLIDATING", "matched": consolidating},
            {"name": "趋势扩散", "rule": "趋势分 ≥ 65、广度分 ≥ 60、波动风险分 ≤ 60", "confirm": "3/3 日", "meaning": "宽基趋势与市场广度同步改善。", "active": state["confirmed_state"] == "OFFENSIVE", "matched": offensive},
            {"name": "修复期", "rule": "至少 3/6 宽基站上 MA20、至多 2/6 站上 MA60、全 A 上涨占比 ≥ 50", "confirm": "3/3 日", "meaning": "短期修复出现，但中期趋势尚待确认。", "active": state["confirmed_state"] == "RECOVERY_WATCH", "matched": recovery},
            {"name": "结构行情", "rule": "未触发其余四种状态；快速轮动、主线集中或结构分化仅作二级说明", "confirm": "2/3 日", "meaning": "整体未形成一致趋势，机会集中在局部结构。", "active": state["confirmed_state"] == "SELECTIVE", "matched": fast_rotation or has_mainline or (not fast_rotation and not has_mainline)},
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
        "连续主线": {"数量": state.get("persistent_mainline_count", 0), "板块": state.get("persistent_mainlines", [])},
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


def completion_content(payload: dict) -> str:
    """Reject exhausted or empty completions with provider diagnostics."""
    choices = payload.get("choices") or []
    if not choices:
        raise ValueError("LLM response has no choices")
    choice = choices[0]
    finish_reason = choice.get("finish_reason") or "unknown"
    content = str((choice.get("message") or {}).get("content") or "").strip()
    usage = payload.get("usage") or {}
    details = usage.get("completion_tokens_details") or {}
    diagnostic = (
        f"finish_reason={finish_reason}, completion_tokens={usage.get('completion_tokens', 'unknown')}, "
        f"reasoning_tokens={details.get('reasoning_tokens', usage.get('reasoning_tokens', 'unknown'))}"
    )
    if finish_reason == "length":
        raise ValueError(f"LLM completion exhausted ({diagnostic})")
    if not content:
        raise ValueError(f"LLM completion content is empty ({diagnostic})")
    return content


def completion_requires_nonthinking_retry(error: Exception) -> bool:
    message = str(error)
    return message.startswith("LLM completion exhausted") or message.startswith("LLM completion content is empty")


def daily_mainline_candidates(report: dict, sectors: list[dict]) -> dict:
    settings = CONFIG["daily_mainline"]
    candidates = []
    stock_pool = {}
    for kind in settings["block_types"]:
        rows = sorted(
            (row for row in sectors if row["block_type"] == kind),
            key=lambda item: (item["daily_score"], item["relative_strength_1"]), reverse=True,
        )[:int(settings["candidate_limits"][kind])]
        for row in rows:
            block_id = f"{kind}:{row['block_name']}"
            leaders = report.get("daily_sector_leaders", {}).get(block_id, [])
            for stock in leaders:
                previous = stock_pool.get(stock["code"])
                if previous is None:
                    stock_pool[stock["code"]] = {**stock, "candidate_block_ids": [block_id]}
                else:
                    previous["candidate_block_ids"] = list(dict.fromkeys(previous["candidate_block_ids"] + [block_id]))
                    if stock["daily_strength_score"] > previous["daily_strength_score"]:
                        previous.update(stock)
            candidates.append({
                "block_id": block_id, "block_type": kind, "block_name": row["block_name"],
                "rank_1": row["rank_1"], "daily_score": row["daily_score"],
                "relative_strength_1": row["relative_strength_1"], "median_return_1": row["median_return_1"],
                "up_breadth": row["up_breadth"], "volume_activity": row["volume_activity"],
                "daily_strong_density": row["daily_strong_density"], "leaders": leaders,
                "qualified": (
                    row["daily_score"] >= settings["minimum_daily_score"]
                    and row["relative_strength_1"] >= settings["minimum_relative_strength_1"]
                    and row["up_breadth"] >= settings["minimum_up_breadth"]
                ),
            })
    stocks = sorted(stock_pool.values(), key=lambda item: item["daily_strength_score"], reverse=True)[:30]
    return {"blocks": candidates, "stocks": stocks, "qualified_count": sum(item["qualified"] for item in candidates)}


def mx_search_items(raw: dict | None) -> list[dict] | None:
    current = raw
    for key in ("data", "data", "llmSearchResponse", "data"):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, list) else None


def classify_news_phase(published_at: str, as_of: str, title: str = "") -> str:
    """Classify when an article became public relative to the target A-share session."""
    value = str(published_at or "").strip()
    match = re.match(r"^(\d{4}-\d{2}-\d{2})(?:[ T](\d{2}):(\d{2}))?", value)
    if not match:
        return "unknown"
    publish_date = match.group(1)
    if publish_date < as_of:
        return "pre_open"
    if publish_date > as_of:
        return "future"
    if publish_date == as_of and re.search(r"复盘|收评|盘后|收官|资金净流入|涨停潮|集体爆发|全面走强", title):
        return "post_close"
    if match.group(2) is None:
        return "unknown"
    minutes = int(match.group(2)) * 60 + int(match.group(3))
    if minutes < 9 * 60 + 30:
        return "pre_open"
    if minutes < 15 * 60:
        return "intraday"
    return "post_close"


def reported_prior_catalyst_date(content: str, as_of: str) -> str:
    """Return a recent prior date only when nearby text describes an auditable event."""
    event_terms = re.compile(
        r"核准|批准|获批|公告|披露|发布|出台|通过|宣布|签署|财报|业绩|上调|下调|"
        r"落地|启动|召开|决定|订单|中标|收购|回购|增持|减持"
    )
    target = pd.Timestamp(as_of)
    lookback = int(CONFIG["daily_mainline"]["core_catalyst_lookback_days"])
    dates = []
    pattern = re.compile(r"(?:(\d{4})[年/-])?(\d{1,2})[月/-](\d{1,2})日?")
    for match in pattern.finditer(str(content or "")):
        year = int(match.group(1) or target.year)
        try:
            event_date = pd.Timestamp(year=year, month=int(match.group(2)), day=int(match.group(3)))
        except ValueError:
            continue
        age_days = (target - event_date).days
        if not 1 <= age_days <= lookback:
            continue
        nearby = str(content or "")[max(0, match.start() - 40):match.end() + 100]
        if event_terms.search(nearby):
            dates.append(event_date)
    return max(dates).strftime("%Y-%m-%d") if dates else ""


def select_daily_mainline_news(raw_results: list[dict], as_of: str, limit: int) -> list[dict]:
    """Normalize, deduplicate and retain a balanced event timeline for the LLM."""
    normalized = []
    seen_titles = set()
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        published_at = str(item.get("date") or "").strip()
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip()
        content = re.sub(r"\s+", " ", str(item.get("content") or "")).strip()
        phase = classify_news_phase(published_at, as_of, title)
        if phase == "future":
            continue
        if not title or not content or title in seen_titles:
            continue
        seen_titles.add(title)
        core_eligible = False
        core_timing_basis = ""
        event_date = ""
        if phase == "pre_open" and published_at[:10]:
            try:
                age_days = (pd.Timestamp(as_of) - pd.Timestamp(published_at[:10])).days
                core_eligible = 0 <= age_days <= int(CONFIG["daily_mainline"]["core_catalyst_lookback_days"])
                if core_eligible:
                    core_timing_basis = "article_pre_open"
                    event_date = reported_prior_catalyst_date(content, as_of) or published_at[:10]
            except ValueError:
                core_eligible = False
        elif phase in {"intraday", "post_close"}:
            event_date = reported_prior_catalyst_date(content, as_of)
            if event_date:
                core_eligible = True
                core_timing_basis = "reported_prior_event"
        normalized.append({
            "title": title,
            "date": published_at[:10],
            "publish_time": published_at,
            "phase": phase,
            "core_eligible": core_eligible,
            "core_timing_basis": core_timing_basis,
            "event_date": event_date,
            "source": str(item.get("source") or ""),
            "content": content[:700],
        })

    quotas = (("pre_open", 6), ("intraday", 3), ("post_close", 2), ("unknown", 1))
    selected = []
    selected_titles = set()
    for phase, quota in quotas:
        for item in (row for row in normalized if row["phase"] == phase):
            if sum(row["phase"] == phase for row in selected) >= quota:
                break
            selected.append(item)
            selected_titles.add(item["title"])
    for item in normalized:
        if len(selected) >= limit:
            break
        if item["title"] not in selected_titles:
            selected.append(item)
            selected_titles.add(item["title"])
    return selected[:limit]


def core_evidence_is_eligible(titles: list[str], evidence_map: dict[str, dict]) -> bool:
    return bool(titles and evidence_map.get(titles[0], {}).get("core_eligible"))


def link_mainline_stocks(
    block_ids: list[str], stock_codes: list[str], block_map: dict[str, dict], stock_map: dict[str, dict],
) -> tuple[list[dict], set[str]]:
    """Attach selected block relationships and report any block without a representative stock."""
    selected_ids = set(block_ids)
    rows = []
    covered_ids = set()
    for code in stock_codes:
        stock = stock_map[code]
        related_ids = [block_id for block_id in block_ids if block_id in stock.get("candidate_block_ids", [])]
        covered_ids.update(related_ids)
        rows.append({
            **stock,
            "selected_block_ids": related_ids,
            "selected_block_names": [block_map[block_id]["block_name"] for block_id in related_ids],
        })
    return rows, selected_ids - covered_ids


def expand_mainline_block_ids(
    block_ids: list[str], stock_codes: list[str], block_map: dict[str, dict], stock_map: dict[str, dict],
) -> list[str]:
    """Deterministically add cross-level blocks represented by the selected stocks."""
    selected_types = {block_map[block_id]["block_type"] for block_id in block_ids}
    has_concept = "gn" in selected_types
    has_industry = bool(selected_types.intersection({"industry_sw_l1", "industry_sw_l2"}))
    if has_concept == has_industry:
        return block_ids
    target_types = {"industry_sw_l1", "industry_sw_l2"} if has_concept else {"gn"}
    coverage = {block_id: 0 for block_id in block_map}
    for code in stock_codes:
        for block_id in set(stock_map[code].get("candidate_block_ids", [])):
            if block_id in coverage:
                coverage[block_id] += 1
    settings = CONFIG["daily_mainline"]
    minimum_coverage = int(settings.get("minimum_extension_stock_coverage", 2))
    generic_names = {"综合", "综合类"}
    candidates = sorted(
        (
            item for item in block_map.values()
            if item["block_id"] not in block_ids
            and item["block_type"] in target_types
            and item["qualified"]
            and item["block_name"] not in generic_names
            and coverage[item["block_id"]] >= minimum_coverage
        ),
        key=lambda item: (coverage[item["block_id"]], item["daily_score"]),
        reverse=True,
    )
    room = min(
        int(settings.get("maximum_chain_extensions", 2)),
        int(settings["maximum_strong_blocks"]) - len(block_ids),
    )
    return block_ids + [item["block_id"] for item in candidates[:max(0, room)]]


def fetch_mx_search_cache(cache_path: Path, query: str) -> tuple[dict | None, str, str]:
    raw = None
    if cache_path.exists():
        try:
            raw = json.loads(cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = None
    if mx_search_items(raw) not in (None, []):
        return raw, "cached", ""
    api_key = os.environ.get("MX_APIKEY", "").strip()
    if not api_key:
        return None, "skipped", "MX_APIKEY missing"
    try:
        response = requests.post(
            MX_SEARCH_URL,
            headers={"apikey": api_key, "Content-Type": "application/json"},
            json={"query": query},
            timeout=45,
        )
        response.raise_for_status()
        raw = response.json()
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return raw, "fetched", ""
    except (requests.RequestException, ValueError, OSError) as exc:
        return None, "failed", str(exc)[:300]


def catalyst_entities(raw_results: list[dict], as_of: str) -> list[str]:
    """Extract a small deterministic entity set to focus the pre-open verification search."""
    vocabulary = (
        ("微软", ("微软", "Microsoft")), ("谷歌", ("谷歌", "Google", "Alphabet")),
        ("Meta", ("Meta",)), ("亚马逊", ("亚马逊", "Amazon", "AWS")),
        ("英伟达", ("英伟达", "NVIDIA")), ("苹果", ("苹果", "Apple")),
        ("特斯拉", ("特斯拉", "Tesla")), ("OpenAI", ("OpenAI",)),
        ("DeepSeek", ("DeepSeek", "深度求索")), ("月之暗面", ("月之暗面", "Kimi")),
        ("智谱", ("智谱", "GLM")), ("阿里", ("阿里", "Alibaba")),
        ("腾讯", ("腾讯", "Tencent")), ("字节跳动", ("字节跳动", "字节", "ByteDance")),
        ("华为", ("华为", "Huawei")), ("国务院", ("国务院",)),
        ("国家发改委", ("国家发改委", "发改委")), ("工信部", ("工信部",)),
        ("央行", ("央行",)), ("证监会", ("证监会",)),
    )
    counts = {entity: 0 for entity, _ in vocabulary}
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        publish_date = str(item.get("date") or "")[:10]
        if publish_date and publish_date > as_of:
            continue
        text = f"{item.get('title') or ''} {item.get('content') or ''}"
        lowered = text.lower()
        for entity, aliases in vocabulary:
            counts[entity] += sum(lowered.count(alias.lower()) for alias in aliases)
    order = {entity: index for index, (entity, _) in enumerate(vocabulary)}
    return sorted((entity for entity, count in counts.items() if count), key=lambda entity: (-counts[entity], order[entity]))[:6]


def daily_mainline_search_topics(block_names: list[str]) -> list[str]:
    """Add only the minimum umbrella terms needed to search a fragmented concept cluster."""
    topics = list(block_names[:6])
    ai_markers = ("AI", "ChatGPT", "AIGC", "大模型", "多模态", "智谱", "Kimi")
    if sum(any(marker.lower() in name.lower() for marker in ai_markers) for name in block_names[:12]) >= 2:
        topics = ["AI应用", "AI商业化"] + topics
    return list(dict.fromkeys(topics))


def daily_mainline_trigger_topics(candidates: dict, limit: int = 8) -> list[str]:
    """Focus catalyst verification on blocks jointly represented by strong stocks."""
    block_map = {item["block_id"]: item for item in candidates["blocks"] if item["qualified"]}
    coverage = {block_id: 0 for block_id in block_map}
    for stock in candidates["stocks"]:
        for block_id in set(stock.get("candidate_block_ids", [])):
            if block_id in coverage:
                coverage[block_id] += 1
    generic_names = {"综合", "综合类"}
    ranked = sorted(
        (item for item in block_map.values() if item["block_name"] not in generic_names),
        key=lambda item: (coverage[item["block_id"]], item["daily_score"]),
        reverse=True,
    )
    return daily_mainline_search_topics([item["block_name"] for item in ranked[:limit]])[:limit]


def fetch_daily_mainline_news(as_of: str, candidates: dict) -> dict:
    """Fetch separate trigger/context searches and attach a deterministic timeline."""
    ranked_blocks = sorted(candidates["blocks"], key=lambda item: item["daily_score"], reverse=True)
    block_names = [item["block_name"] for item in ranked_blocks[:12]]
    search_topics = daily_mainline_search_topics(block_names)
    context_topics = search_topics[:2] if search_topics[:2] == ["AI应用", "AI商业化"] else search_topics[:8]
    context_query = f"{as_of} A股 早盘高开 原因 隔夜消息 海外财报 商业化催化 " + " ".join(context_topics)
    context_raw, context_status, context_error = fetch_mx_search_cache(
        STATE_DIR / f"daily_mainline_context_news_v7_{today_stamp(as_of)}.json", context_query,
    )
    context_results = mx_search_items(context_raw) or []
    entities = catalyst_entities(context_results, as_of)
    focused_entity = entities[0] if entities else ""
    trigger_topics = daily_mainline_trigger_topics(candidates)
    trigger_query = f"{as_of} A股 开盘前 隔夜 核心催化 原始公告 事件日期 最新财报 政策 {focused_entity} " + " ".join(trigger_topics)
    trigger_raw, trigger_status, trigger_error = fetch_mx_search_cache(
        STATE_DIR / f"daily_mainline_trigger_news_v8_{today_stamp(as_of)}.json", trigger_query,
    )
    trigger_results = mx_search_items(trigger_raw) or []
    raw_results = trigger_results + context_results
    statuses = [context_status, trigger_status]
    errors = [error for error in (context_error, trigger_error) if error]
    queries = {"context": context_query, "trigger": trigger_query}
    items = select_daily_mainline_news(raw_results, as_of, int(CONFIG["daily_mainline"]["news_max_items"]))
    source_status = "fetched" if "fetched" in statuses else "cached" if "cached" in statuses else statuses[0]
    result = {"status": source_status if items else "empty", "queries": queries, "items": items}
    if errors:
        result["reason"] = "; ".join(errors)
    return result


def daily_mainline_fallback(candidates: dict, news: dict, reason: str) -> dict:
    settings = CONFIG["daily_mainline"]
    blocks = sorted((item for item in candidates["blocks"] if item["qualified"]), key=lambda item: item["daily_score"], reverse=True)[:settings["maximum_strong_blocks"]]
    allowed_codes = {stock["code"] for block in blocks for stock in block["leaders"]}
    stocks = [item for item in candidates["stocks"] if item["code"] in allowed_codes][:settings["maximum_strong_stocks"]]
    block_ids = [item["block_id"] for item in blocks]
    block_map = {item["block_id"]: item for item in blocks}
    stock_map = {item["code"]: item for item in stocks}
    linked_stocks, _ = link_mainline_stocks(block_ids, list(stock_map), block_map, stock_map)
    return {
        "status": "degraded", "name": "当日主线待归纳" if blocks else "当日无清晰主线",
        "core_event": "资讯或主线归纳不可用，未生成事件判断。",
        "narrative_logic": "仅保留当日行情筛选结果，不根据模型常识补写催化或因果关系。",
        "strong_blocks": [{key: value for key, value in item.items() if key != "leaders"} for item in blocks], "strong_stocks": linked_stocks, "evidence": [],
        "reason": reason, "news_status": news.get("status", "unknown"),
    }


def call_daily_mainline_analysis(as_of: str, candidates: dict, news: dict) -> dict:
    settings = CONFIG["daily_mainline"]
    if candidates["qualified_count"] < 2:
        result = daily_mainline_fallback(candidates, news, "fewer than two qualified blocks")
        result.update({"status": "unclear", "name": "当日无清晰主线", "core_event": "强势板块未形成足够的横向共振。"})
        return result
    if not news.get("items"):
        return daily_mainline_fallback(candidates, news, "no auditable news evidence")
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        return daily_mainline_fallback(candidates, news, "DEEPSEEK_API_KEY missing")
    model = settings.get("model", "deepseek-v4-flash")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    block_payload = [{key: item[key] for key in ("block_id", "block_type", "block_name", "rank_1", "daily_score", "relative_strength_1", "median_return_1", "up_breadth", "volume_activity", "daily_strong_density", "qualified")} for item in candidates["blocks"]]
    stock_payload = [{key: item[key] for key in ("code", "name", "return_1", "rps1_market", "rps5_market", "volume_ratio_20", "daily_strength_score", "candidate_block_ids")} for item in candidates["stocks"]]
    system_prompt = (
        "你是A股盘后主线归纳器。只能依据输入的当日行情候选和妙想资讯证据，归纳一条今日盘面主线。"
        "主线是多个强势板块和核心个股围绕同一事件与预期变化形成的叙事链，不是简单复制涨幅最高的概念名。"
        "如果候选方向互不相关、只有单点上涨，或资讯无法解释盘面共振，status必须为unclear。"
        "在资讯能够支持的前提下，优先解释daily_score靠前且被多只候选股共同覆盖的板块组合，不得仅因某条资讯更容易命名就忽略更强的盘面共振。"
        "status=clear时，从候选block_id中选择2至6个strong_block_ids，从候选股票代码中选择2至6个strong_stock_codes；"
        "每只核心股的candidate_block_ids必须至少包含一个最终选择的strong_block_id，每个最终选择的strong_block_id也必须至少被一只核心股覆盖。"
        "从资讯标题中原样选择1至3个evidence_titles，第一条必须满足core_eligible=true，core_event只能概括第一条证据。"
        "core_timing_basis=article_pre_open表示文章本身盘前可知；reported_prior_event表示文章虽在盘中或盘后发布，但正文明确记载了event_date发生的更早事件，"
        "此时core_event只能概括该明确日期附近的事件动作，不能概括文章发布后的盘面结果。phase=unknown不得承担核心催化，"
        "不得把盘中或收盘后才发生的消息倒推为启动原因。name采用‘共同母题+当日交易焦点’的4至24字中性名称，"
        "不得使用引爆、狂欢、全面爆发、主升、掀起涨停潮等情绪化或趋势预测表达；core_event用一句话概括一项主要事件，其他事件只能作为辅助催化；"
        "narrative_logic用80至180字写清‘事件→预期变化→受益环节→盘面响应’，保持客观克制。"
        "不得使用候选之外的板块或股票，不得使用资讯之外的事件和数字，不得提供买卖、仓位、涨跌预测。"
        "只返回JSON对象，字段固定为status、name、core_event、narrative_logic、strong_block_ids、strong_stock_codes、evidence_titles。"
    )
    payload = {"trade_date": as_of, "qualified_block_count": candidates["qualified_count"], "block_candidates": block_payload, "stock_candidates": stock_payload, "news_evidence": news["items"]}
    last_error = "unknown"
    thinking_mode = settings.get("thinking", "enabled")
    fallback_thinking = settings.get("fallback_thinking", "disabled")
    for _ in range(int(settings["llm_max_retries"])):
        try:
            response = requests.post(
                f"{base_url}/chat/completions", headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": json.dumps(payload, ensure_ascii=False)}], "thinking": {"type": thinking_mode}, "temperature": 0.1, "max_tokens": int(settings["llm_max_tokens"]), "response_format": {"type": "json_object"}, "stream": False}, timeout=int(settings.get("llm_timeout_seconds", 120)),
            )
            response.raise_for_status()
            parsed = parse_llm_json_object(completion_content(response.json()))
            if parsed.get("status") not in {"clear", "unclear"}:
                raise ValueError("invalid status")
            block_map = {item["block_id"]: item for item in candidates["blocks"]}
            stock_map = {item["code"]: item for item in candidates["stocks"]}
            evidence_map = {item["title"]: item for item in news["items"]}
            block_ids = list(dict.fromkeys(parsed.get("strong_block_ids") or []))
            stock_codes = list(dict.fromkeys(str(code) for code in (parsed.get("strong_stock_codes") or [])))
            titles = list(dict.fromkeys(parsed.get("evidence_titles") or []))
            if any(item not in block_map for item in block_ids) or any(item not in stock_map for item in stock_codes) or any(item not in evidence_map for item in titles):
                raise ValueError("response contains evidence outside candidate set")
            if parsed["status"] == "clear" and (not 2 <= len(block_ids) <= settings["maximum_strong_blocks"] or not 2 <= len(stock_codes) <= settings["maximum_strong_stocks"] or not titles):
                raise ValueError("clear mainline lacks cross-validated evidence")
            if parsed["status"] == "clear" and not core_evidence_is_eligible(titles, evidence_map):
                raise ValueError("first evidence is not an eligible recent pre-open catalyst")
            if parsed["status"] == "clear" and any(not block_map[item]["qualified"] for item in block_ids):
                raise ValueError("clear mainline selected an unqualified block")
            selected_ids = set(block_ids)
            if parsed["status"] == "clear" and any(not selected_ids.intersection(stock_map[code]["candidate_block_ids"]) for code in stock_codes):
                raise ValueError("selected stock is unrelated to selected blocks")
            linked_stocks, uncovered_ids = link_mainline_stocks(block_ids, stock_codes, block_map, stock_map)
            if parsed["status"] == "clear" and uncovered_ids:
                raise ValueError("selected blocks lack representative stocks: " + ", ".join(sorted(uncovered_ids)))
            if parsed["status"] == "clear":
                block_ids = expand_mainline_block_ids(block_ids, stock_codes, block_map, stock_map)
                linked_stocks, uncovered_ids = link_mainline_stocks(block_ids, stock_codes, block_map, stock_map)
                if uncovered_ids:
                    raise ValueError("expanded blocks lack representative stocks: " + ", ".join(sorted(uncovered_ids)))
            name, core_event, logic = (str(parsed.get(key) or "").strip() for key in ("name", "core_event", "narrative_logic"))
            if not name or not core_event or not logic:
                raise ValueError("mainline text fields are incomplete")
            if not 4 <= len(name) <= 24 or any(term in name for term in ("引爆", "狂欢", "全面爆发", "主升", "掀起", "暴涨", "涨停潮")):
                raise ValueError("mainline name is not neutral and concise")
            return {
                "status": parsed["status"], "name": name if parsed["status"] == "clear" else "当日无清晰主线",
                "core_event": core_event, "narrative_logic": logic,
                "strong_blocks": [{key: value for key, value in block_map[item].items() if key != "leaders"} for item in block_ids], "strong_stocks": linked_stocks,
                "evidence": [{key: evidence_map[item][key] for key in ("title", "date", "source", "event_date", "core_timing_basis")} for item in titles],
                "news_status": news["status"], "provider": "deepseek", "model": model, "thinking_mode": thinking_mode,
            }
        except Exception as exc:
            last_error = str(exc)[:300]
            if thinking_mode != fallback_thinking and completion_requires_nonthinking_retry(exc):
                thinking_mode = fallback_thinking
    print(f"[market_regime] 今日主线归纳失败（{model}）: {last_error}", file=sys.stderr)
    return daily_mainline_fallback(candidates, news, last_error)


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
    if state == "CONSOLIDATING":
        return "接下来重点观察广度改善能否延续、更多宽基能否重回 MA20 上方，以及高波动是否继续收敛。"
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
    model = CONFIG.get("reporting", {}).get("model", "deepseek-v4-flash")
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
                    "thinking": {"type": CONFIG.get("reporting", {}).get("thinking", "disabled")},
                    "temperature": 0.2,
                    "max_tokens": max_tokens,
                    "stream": False,
                },
                timeout=60,
            )
            response.raise_for_status()
            content = completion_content(response.json())
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
    print(f"[market_regime] LLM 分析失败（{model}，重试{retries}次）: {last_error}", file=sys.stderr)
    return {"status": "failed", "reason": last_error, "provider": "deepseek", "model": model, "analysis": ""}


def build_market_context(report: dict, sectors: list[dict], stocks: list[dict], state_conn: sqlite3.Connection, as_of: str) -> dict:
    kinds = ("industry_sw_l1", "industry_sw_l2", "gn", "fg")
    rankings = {kind: [row for row in sectors if row["block_type"] == kind][:20] for kind in kinds}
    selected = {(kind, row["block_name"]) for kind, rows in rankings.items() for row in rows}
    history = pd.read_sql_query("""SELECT trade_date,block_kind,block_name,rank_1,rank_20,rank_5,rank_pct_20,rank_pct_5,
        return_1,return_5,return_20,relative_strength_1,relative_strength_5,relative_strength_20,daily_score,
        advance_ratio,advance_ratio_5 AS up_breadth_5,volume_activity,above_ma20_ratio,sector_state,sector_phase,
        sector_health,sector_health_level,sector_health_score,short_pulse,
        sector_policy_tier,data_status,history_basis
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
        "daily_mainline": report.get("daily_mainline", {}),
        "sector_rankings": rankings, "sector_history": sector_history, "sector_rank_matrix": rank_matrix, "sector_leaders": sector_leaders}


def write_dashboard_index(market_latest: str | None, market_available: list[str]) -> None:
    index = {"market": {"latest": market_latest, "available": market_available}}
    # Every publisher owns only its module.  Rebuild the other known module
    # indexes from their published packages so a market refresh cannot erase
    # an independently published valuation index.
    for kind in ("capital", "signals", "vcp", "backtest", "valuation"):
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
        """INSERT INTO sector_daily_metrics(trade_date,block_kind,block_name,member_count,rank_1,rank_20,rank_5,
            rank_pct_20,rank_pct_5,
            return_1,return_5,return_10,return_20,relative_strength_1,relative_strength_5,relative_strength_20,median_return_1,
            advance_ratio,advance_ratio_5,volume_activity,above_ma20_ratio,above_ma60_ratio,new_high_ratio,strong_stock_density,
            daily_strong_density,daily_score,sector_state,sector_phase,sector_health,sector_health_level,sector_health_score,
            short_pulse,sector_policy_tier,data_status,history_basis) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        [(as_of, row["block_type"], row["block_name"], row["member_count"], row["rank_1"], row["rank_20"], row["rank_5"],
          row["rank_pct_20"], row["rank_pct_5"],
          row["return_1"], row["return_5"], row["return_10"], row["return_20"], row["relative_strength_1"],
          row["relative_strength_5"], row["relative_strength_20"], row["median_return_1"], row["up_breadth"], row["up_breadth_5"], row["volume_activity"],
          row["above_ma20_ratio"], row["above_ma60_ratio"], row["new_high_ratio"], row["strong_stock_density"],
          row["daily_strong_density"], row["daily_score"], row.get("sector_state", "历史积累中"),
          row.get("sector_phase", "NONE"), row.get("sector_health", "数据不足"), row.get("sector_health_level", 0),
          row.get("sector_health_score", 0.0), int(bool(row.get("short_pulse"))),
          row.get("sector_policy_tier", "D"), row.get("data_status", "BUILDING"), row["history_basis"])
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
    run = sub.add_parser("run"); run.add_argument("--date"); run.add_argument("--no-llm", action="store_true"); run.add_argument("--allow-snapshot-fallback", action="store_true"); run.add_argument("--reuse-existing-mainline", action="store_true")
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
        existing_report_path = OUTPUT_DIR / f"market_regime_{today_stamp(as_of)}.json"
        existing_report = json.loads(existing_report_path.read_text(encoding="utf-8")) if args.reuse_existing_mainline and existing_report_path.exists() else {}
        try:
            report, sectors, stocks, concepts = compute_metrics(conn, state_conn, as_of, args.allow_snapshot_fallback)
        except RuntimeError as exc:
            print(f"数据未就绪：{exc}", file=sys.stderr)
            return 2
        confirm_market_state(state_conn, as_of, report)
        report["llm"] = call_market_llm_analysis(report) if not args.no_llm else {
            "status": "skipped", "reason": "disabled_by_flag", "analysis": ""
        }
        candidates = daily_mainline_candidates(report, sectors)
        if args.no_llm:
            report["daily_mainline"] = existing_report.get("daily_mainline") or daily_mainline_fallback(candidates, {"status": "skipped"}, "disabled_by_flag")
        else:
            news = fetch_daily_mainline_news(as_of, candidates)
            report["daily_mainline"] = call_daily_mainline_analysis(as_of, candidates, news)
        save_block_metrics(state_conn, as_of, sectors); write_outputs(report, sectors, stocks, concepts, state_conn, as_of)
        print(json.dumps(report["state"], ensure_ascii=False, indent=2)); return 0
    finally:
        state_conn.close()
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
