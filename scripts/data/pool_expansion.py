"""Relative-strength expansion channel for Model 1."""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path

import pandas as pd

from scripts.data.market_data_store import DB_PATH


def _percentile(series: pd.Series) -> pd.Series:
    """Return deterministic 0-100 cross-sectional percentile ranks."""
    return series.rank(method="average", pct=True) * 100.0


def _industry_names(conn: sqlite3.Connection, as_of: str) -> dict[str, str]:
    snapshot = conn.execute(
        "SELECT max(snapshot_date) FROM stock_industries WHERE snapshot_date<=?",
        (as_of,),
    ).fetchone()[0]
    if not snapshot:
        return {}
    rows = conn.execute(
        """SELECT si.code,COALESCE(id.industry_name,'')
           FROM stock_industries si
           LEFT JOIN industry_definitions id
             ON id.snapshot_date=si.snapshot_date
            AND id.industry_system='sw'
            AND id.industry_code=si.sw_industry_code
           WHERE si.snapshot_date=?""",
        (snapshot,),
    )
    return dict(rows.fetchall())


def build_expansion_pool(
    as_of: str,
    config: dict,
    core_codes: set[str] | None = None,
    db_path: str | Path = DB_PATH,
) -> tuple[list[dict], dict]:
    """Build RS top-N, then apply hard filters without rank backfilling.

    Financial growth and cash-flow rules intentionally remain exclusive to the
    core quality channel. Expansion-only rows are explicitly unverified.
    """
    core_codes = core_codes or set()
    if not config.get("enabled", False):
        return [], {"enabled": False, "final_total": 0}

    short_window = int(config["return_windows"]["short"])
    long_window = int(config["return_windows"]["long"])
    top_n = int(config["top_n_before_filters"])
    hard = config["hard_filters"]

    conn = sqlite3.connect(str(db_path))
    try:
        membership = pd.read_sql_query(
            "SELECT code,eligible,COALESCE(exclusion_reason,'') AS exclusion_reason "
            "FROM universe_members WHERE trade_date=?",
            conn,
            params=(as_of,),
        )
        if membership.empty:
            raise RuntimeError(f"扩展池缺少 {as_of} 的 universe_members 快照")

        bars = pd.read_sql_query(
            "SELECT code,trade_date,close,amount FROM daily_bars "
            "WHERE trade_date<=? ORDER BY code,trade_date",
            conn,
            params=(as_of,),
        )
        securities = pd.read_sql_query(
            "SELECT code,name,is_st FROM securities",
            conn,
        ).set_index("code")
        industries = _industry_names(conn, as_of)
        market_dates = [row[0] for row in conn.execute(
            "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<=? "
            "ORDER BY trade_date DESC LIMIT ?",
            (as_of, short_window),
        )]
    finally:
        conn.close()

    membership_map = membership.set_index("code").to_dict("index")
    active_dates = set(market_dates)
    records = []
    for code, frame in bars.groupby("code", sort=True):
        frame = frame.sort_values("trade_date")
        if len(frame) <= long_window:
            continue
        latest = frame.iloc[-1]
        close = float(latest["close"])
        short_base = float(frame.iloc[-short_window - 1]["close"])
        long_base = float(frame.iloc[-long_window - 1]["close"])
        if close <= 0 or short_base <= 0 or long_base <= 0:
            continue
        recent = frame[frame["trade_date"].isin(active_dates)].copy()
        recent_amount = pd.to_numeric(recent["amount"], errors="coerce").fillna(0.0)
        security = securities.loc[code] if code in securities.index else None
        member = membership_map.get(code, {})
        records.append({
            "code": code,
            "name": str(security["name"]) if security is not None else code,
            "industry": industries.get(code, ""),
            "eligible": bool(member.get("eligible", 0)),
            "exclusion_reason": str(member.get("exclusion_reason", "")),
            "is_st": bool(security["is_st"]) if security is not None else False,
            "last_trade_date": str(latest["trade_date"]),
            "history_sessions": int(len(frame)),
            "active_sessions_20": int(len(recent)),
            "average_amount_20": float(recent_amount.mean()) if len(recent) else 0.0,
            "return_short_pct": (close / short_base - 1.0) * 100.0,
            "return_long_pct": (close / long_base - 1.0) * 100.0,
        })

    ranked = pd.DataFrame(records)
    if ranked.empty:
        return [], {
            "enabled": True,
            "rankable_total": 0,
            "initial_top_total": 0,
            "final_total": 0,
            "rejected": {},
        }

    ranked["rs_short_percentile"] = _percentile(ranked["return_short_pct"])
    ranked["rs_long_percentile"] = _percentile(ranked["return_long_pct"])
    weights = config["weights"]
    ranked["expansion_score"] = (
        ranked["rs_short_percentile"] * float(weights["rs_short_percentile"])
        + ranked["rs_long_percentile"] * float(weights["rs_long_percentile"])
    )
    ranked = ranked.sort_values(
        ["expansion_score", "code"], ascending=[False, True], kind="mergesort"
    ).head(top_n).copy()
    ranked["rs_rank"] = range(1, len(ranked) + 1)

    rejected = Counter()
    passed = []
    for row in ranked.to_dict("records"):
        reason = ""
        if hard.get("require_universe_eligible", True) and (not row["eligible"] or row["is_st"]):
            reason = row["exclusion_reason"] or "UNIVERSE_INELIGIBLE"
        elif row["last_trade_date"] != as_of:
            reason = "TARGET_DATE_MISSING"
        elif row["history_sessions"] < int(hard["minimum_history_sessions"]):
            reason = "HISTORY_TOO_SHORT"
        elif row["active_sessions_20"] < int(hard["minimum_active_sessions_20"]):
            reason = "INSUFFICIENT_ACTIVE_SESSIONS"
        elif row["average_amount_20"] < float(hard["minimum_average_amount_20"]):
            reason = "LOW_LIQUIDITY"
        if reason:
            rejected[reason] += 1
            continue
        row["pool_channel"] = "BOTH" if row["code"] in core_codes else "EXPANSION_RS"
        row["fundamental_status"] = (
            "CORE_VERIFIED" if row["code"] in core_codes else config["unverified_fundamental_tag"]
        )
        passed.append(row)

    overlap = sum(1 for row in passed if row["code"] in core_codes)
    return passed, {
        "enabled": True,
        "rankable_total": int(len(records)),
        "initial_top_total": int(len(ranked)),
        "rejected": dict(sorted(rejected.items())),
        "passed_after_filters": len(passed),
        "core_overlap_total": overlap,
        "expansion_only_total": len(passed) - overlap,
        "final_total": len(passed),
    }
