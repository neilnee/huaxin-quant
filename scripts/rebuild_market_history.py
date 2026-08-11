#!/usr/bin/env python3
"""Replay historical market derivatives without rerunning investment conclusions."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts import dashboard_signals, dashboard_vcp, market_regime
from scripts.shared import PROJECT_ROOT

ROOT = Path(PROJECT_ROOT)
LIVE_MARKET = ROOT / "market"
LIVE_STATE_DB = ROOT / "cache" / "market_regime" / "market_regime.sqlite"
LIVE_DASHBOARD_DATA = ROOT / "dashboard" / "data"
DEFAULT_WORKSPACE = ROOT / ".tmp" / "market_history_refresh"
BACKUP_ROOT = ROOT / ".tmp" / "market_history_backups"
PROTECTED_PATTERNS = (
    "cache/quant_runs/quant_*.json", "bloom/state/bloom_input_*.json",
    "bloom/state/bloom_state.csv", "bloom/state/bloom_events.jsonl",
    "signal_plan/signal_plan_*.json", "signal_plan/signal_plan_*.md",
    "position/**/*", "reports/**/*",
)


def normalize_stamp(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        return digits[2:]
    if len(digits) == 6:
        return digits
    raise ValueError("日期应为 YYMMDD 或 YYYY-MM-DD")


def iso_date(stamp: str) -> str:
    return datetime.strptime(stamp, "%y%m%d").strftime("%Y-%m-%d")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_hashes() -> dict[str, str]:
    paths: set[Path] = set()
    for pattern in PROTECTED_PATTERNS:
        paths.update(path for path in ROOT.glob(pattern) if path.is_file())
    return {str(path.relative_to(ROOT)): sha256(path) for path in sorted(paths)}


def atomic_copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix=f".{target.name}.", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if temporary.exists():
            temporary.unlink()


def read_context_js(path: Path) -> dict:
    match = re.search(r"=\s*(\{.*\});\s*$", path.read_text(encoding="utf-8"), re.S)
    if not match:
        raise ValueError(f"无法解析 Dashboard 数据包: {path}")
    return json.loads(match.group(1))


def write_context_js(path: Path, namespace: str, payload: dict) -> None:
    stamp = path.stem.rsplit("_", 1)[-1]
    text = f"window.{namespace} = window.{namespace} || {{}};\nwindow.{namespace}[{json.dumps(stamp)}] = " + json.dumps(payload, ensure_ascii=False) + ";\n"
    temporary = path.with_name(f".{path.name}.refresh")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def refresh_markdown_state(source: Path, target: Path, report: dict) -> None:
    """Update only the deterministic state heading in the preserved daily archive."""
    text = source.read_text(encoding="utf-8")
    label = market_regime.state_label(report["state"]["confirmed_state"], report)
    tag = market_regime.market_structure_tag(report)
    heading = f"**市场状态：{label}" + (f" · {tag}" if tag else "") + "**  "
    refreshed, count = re.subn(r"^\*\*市场状态：.*\*\*  $", heading, text, count=1, flags=re.M)
    if count != 1:
        raise ValueError(f"无法定位市场状态标题: {source}")
    target.write_text(refreshed, encoding="utf-8")


def available_dates(start: str | None, end: str | None) -> list[str]:
    dates = sorted(path.stem.rsplit("_", 1)[-1] for path in LIVE_MARKET.glob("market_regime_*.json"))
    if start:
        dates = [date for date in dates if date >= normalize_stamp(start)]
    if end:
        dates = [date for date in dates if date <= normalize_stamp(end)]
    return dates


def configure_shadow(workspace: Path) -> dict[str, Path]:
    paths = {
        "state_db": workspace / "cache" / "market_regime" / "market_regime.sqlite",
        "market": workspace / "market", "market_data": workspace / "market" / "data",
        "dashboard_data": workspace / "dashboard" / "data",
    }
    market_regime.STATE_DIR = paths["state_db"].parent
    market_regime.STATE_DB_PATH = paths["state_db"]
    market_regime.OUTPUT_DIR = paths["market"]
    market_regime.DATA_OUTPUT_DIR = paths["market_data"]
    market_regime.DASHBOARD_DATA_DIR = paths["dashboard_data"]
    return paths


def validate_workspace(workspace: Path) -> Path:
    root = (ROOT / ".tmp").resolve()
    resolved = workspace.resolve()
    if resolved == root or root not in resolved.parents:
        raise ValueError("workspace 必须是项目 .tmp 下的独立子目录")
    return resolved


def reclassify_market_state(report: dict, sectors: list[dict], persistent_mainlines: list[str]) -> None:
    """Recompute deterministic market state while preserving narrative conclusions."""
    state = report["state"]
    benchmarks = report["benchmarks"]
    breadth = report["breadth"]
    evidence = market_regime.selective_opportunity_evidence(sectors, persistent_mainlines)
    raw_state = market_regime.classify_market_state(
        state["trend_score"], state["volatility_score"], state["breadth_score"], state["rotation_score"],
        sum(item["above_ma20"] for item in benchmarks.values()),
        sum(item["above_ma60"] for item in benchmarks.values()),
        breadth["advance_ratio"], evidence["qualified"],
    )
    state.update({
        "current": raw_state,
        "raw_state": raw_state,
        "persistent_mainline_count": len(evidence["persistent_mainlines"]),
        "persistent_mainlines": evidence["persistent_mainlines"],
        "local_opportunity": evidence["qualified"],
        "local_opportunity_basis": evidence["basis"],
        "strong_industry_count": evidence["strong_industry_count"],
        "strong_concept_count": evidence["strong_concept_count"],
    })


def replay(dates: list[str], workspace: Path) -> dict:
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    paths = configure_shadow(workspace)
    protected_before = protected_hashes()
    live_conn = sqlite3.connect(f"file:{LIVE_STATE_DB}?mode=ro", uri=True)
    live_conn.row_factory = sqlite3.Row
    state_conn = market_regime.connect_state_db()
    preserved = {"llm": 0, "daily_mainline": 0}
    state_changes = []
    try:
        for index, stamp in enumerate(dates, 1):
            old = json.loads((LIVE_MARKET / f"market_regime_{stamp}.json").read_text(encoding="utf-8"))
            report = json.loads(json.dumps(old, ensure_ascii=False))
            report["llm"] = old.get("llm", {})
            report["daily_mainline"] = old.get("daily_mainline", {})
            source_rows = [dict(row) for row in live_conn.execute(
                "SELECT * FROM sector_daily_metrics WHERE trade_date=? ORDER BY block_kind,rank_20", (iso_date(stamp),)
            )]
            peer_counts: dict[str, int] = {}
            for source in source_rows:
                peer_counts[source["block_kind"]] = peer_counts.get(source["block_kind"], 0) + 1
            history = pd.read_sql_query(
                """SELECT trade_date,block_kind,block_name,rank_20,rank_5,rank_pct_20,rank_pct_5,
                   relative_strength_5,advance_ratio AS up_breadth,advance_ratio_5 AS up_breadth_5,
                   sector_state,sector_phase,sector_health,data_status
                   FROM sector_daily_metrics WHERE trade_date<? ORDER BY trade_date DESC LIMIT 30000""",
                state_conn, params=(iso_date(stamp),),
            )
            sectors = []
            for source in source_rows:
                row = {
                    "date": source["trade_date"], "block_type": source["block_kind"], "block_name": source["block_name"],
                    "member_count": source["member_count"], "rank_1": source["rank_1"], "rank_20": source["rank_20"],
                    "rank": source["rank_20"], "rank_5": source["rank_5"],
                    "rank_pct_20": source["rank_20"] / peer_counts[source["block_kind"]],
                    "rank_pct_5": source["rank_5"] / peer_counts[source["block_kind"]],
                    "return_1": source["return_1"], "return_5": source["return_5"], "return_10": source["return_10"],
                    "return_20": source["return_20"], "relative_strength_1": source["relative_strength_1"],
                    "relative_strength_5": source["relative_strength_5"], "relative_strength_20": source["relative_strength_20"],
                    "median_return_1": source["median_return_1"], "up_breadth": source["advance_ratio"],
                    "up_breadth_5": source["advance_ratio_5"], "volume_activity": source["volume_activity"],
                    "above_ma20_ratio": source["above_ma20_ratio"], "above_ma60_ratio": source["above_ma60_ratio"],
                    "new_high_ratio": source["new_high_ratio"], "strong_stock_density": source["strong_stock_density"],
                    "daily_strong_density": source["daily_strong_density"], "daily_score": source["daily_score"],
                    "history_basis": source["history_basis"],
                }
                prior = history[(history.block_kind == row["block_type"]) & (history.block_name == row["block_name"])].head(5)
                row.update(market_regime.classify_sector_phase(row, prior))
                sectors.append(row)
            market_regime.save_block_metrics(state_conn, iso_date(stamp), sectors)
            persistent_mainlines = market_regime.continuous_core_mainlines(state_conn, iso_date(stamp), sectors)
            reclassify_market_state(report, sectors, persistent_mainlines)
            market_regime.confirm_market_state(state_conn, iso_date(stamp), report)
            preserved["llm"] += int(report["llm"] == old.get("llm", {}))
            preserved["daily_mainline"] += int(report["daily_mainline"] == old.get("daily_mainline", {}))
            paths["market"].mkdir(parents=True, exist_ok=True)
            (paths["market"] / f"market_regime_{stamp}.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            live_markdown = LIVE_MARKET / f"market_regime_{stamp}.md"
            if live_markdown.exists():
                refresh_markdown_state(live_markdown, paths["market"] / live_markdown.name, report)
            market_regime.write_csv(paths["market"] / f"sector_heat_{stamp}.csv", sectors)
            old_state = (old.get("state") or {}).get("current") or (old.get("state") or {}).get("confirmed_state")
            new_state = report["state"]["confirmed_state"]
            if old_state != new_state:
                state_changes.append({"date": stamp, "before": old_state, "after": new_state})
            print(f"[{index}/{len(dates)}] {stamp} {new_state}", flush=True)
    finally:
        state_conn.close()
        live_conn.close()
    with sqlite3.connect(paths["state_db"]) as rebuilt:
        rebuilt.row_factory = sqlite3.Row
        all_sector_rows = {
            (row["trade_date"], row["block_kind"], row["block_name"]): dict(row)
            for row in rebuilt.execute("SELECT * FROM sector_daily_metrics")
        }
    for stamp in dates:
        report = json.loads((paths["market"] / f"market_regime_{stamp}.json").read_text(encoding="utf-8"))
        context_path = LIVE_MARKET / "data" / f"market_context_{stamp}.json"
        context = json.loads(context_path.read_text(encoding="utf-8"))
        context["market_state"] = market_regime.market_state_view(report)
        context["market_state_explainer"] = market_regime.market_state_explainer(report)
        for kind, rows in context.get("sector_rankings", {}).items():
            for row in rows:
                rebuilt_row = all_sector_rows.get((iso_date(stamp), kind, row.get("block_name")))
                if rebuilt_row:
                    _merge_sector_fields(row, rebuilt_row)
        for kind_rows in context.get("sector_history", {}).values():
            for item in kind_rows:
                for point in item.get("points", []):
                    rebuilt_row = all_sector_rows.get((point.get("trade_date"), point.get("block_kind"), point.get("block_name")))
                    if rebuilt_row:
                        _merge_sector_fields(point, rebuilt_row)
        paths["market_data"].mkdir(parents=True, exist_ok=True)
        (paths["market_data"] / f"market_context_{stamp}.json").write_text(
            json.dumps(context, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        month = paths["dashboard_data"] / f"20{stamp[:4]}"
        month.mkdir(parents=True, exist_ok=True)
        write_context_js(month / f"market_context_{stamp}.js", "QUANT_DASHBOARD_MARKET_CONTEXTS", context)
    (paths["market_data"] / "latest.json").write_text(
        json.dumps({"latest": dates[-1], "available": dates}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if protected_before != protected_hashes():
        raise RuntimeError("回放改动了受保护的 Quant/Bloom/Plan/持仓/估值文件")
    with sqlite3.connect(paths["state_db"]) as conn:
        status_rows = conn.execute("SELECT data_status,count(*) FROM sector_daily_metrics GROUP BY data_status").fetchall()
        health_ready = conn.execute("SELECT count(*) FROM sector_daily_metrics WHERE sector_health!='数据不足'").fetchone()[0]
    audit = {
        "dates": dates, "date_count": len(dates), "state_changes": state_changes,
        "state_change_count": len(state_changes), "preserved": preserved,
        "protected_file_count": len(protected_before), "protected_hashes_unchanged": True,
        "sector_data_status": dict(status_rows), "sector_health_ready_rows": health_ready,
        "workspace": str(workspace),
    }
    (workspace / "audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit


def _merge_sector_fields(target: dict, source: dict) -> None:
    target.update({
        "rank_pct_20": source["rank_pct_20"], "rank_pct_5": source["rank_pct_5"],
        "up_breadth_5": source["advance_ratio_5"], "sector_state": source["sector_state"],
        "sector_phase": source["sector_phase"], "sector_health": source["sector_health"],
        "sector_health_level": source["sector_health_level"], "sector_health_score": source["sector_health_score"],
        "short_pulse": source["short_pulse"], "sector_policy_tier": source["sector_policy_tier"],
        "data_status": source["data_status"], "history_basis": source["history_basis"],
    })


def backup_live(dates: list[str]) -> Path:
    backup = BACKUP_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    (backup / "market" / "data").mkdir(parents=True)
    (backup / "cache" / "market_regime").mkdir(parents=True)
    with sqlite3.connect(LIVE_STATE_DB) as source, sqlite3.connect(backup / "cache" / "market_regime" / "market_regime.sqlite") as target:
        source.backup(target)
    for stamp in dates:
        for name in (f"market_regime_{stamp}.json", f"sector_heat_{stamp}.csv", f"market_regime_{stamp}.md"):
            source = LIVE_MARKET / name
            if source.exists():
                atomic_copy(source, backup / "market" / name)
        source = LIVE_MARKET / "data" / f"market_context_{stamp}.json"
        if source.exists():
            atomic_copy(source, backup / "market" / "data" / source.name)
    return backup


def replace_state_tables(shadow_db: Path) -> None:
    with sqlite3.connect(LIVE_STATE_DB) as conn:
        conn.execute("ATTACH DATABASE ? AS shadow", (str(shadow_db),))
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("block_metrics", "sector_daily_metrics", "market_state_history"):
                columns = [row[1] for row in conn.execute(f"PRAGMA main.table_info({table})")]
                names = ",".join(columns)
                conn.execute(f"DELETE FROM main.{table}")
                conn.execute(f"INSERT INTO main.{table}({names}) SELECT {names} FROM shadow.{table}")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.execute("DETACH DATABASE shadow")


def refresh_vcp_environment(dates: set[str]) -> int:
    updated = 0
    for path in sorted(LIVE_DASHBOARD_DATA.glob("*/vcp_context_*.js")):
        stamp = path.stem.rsplit("_", 1)[-1]
        if stamp not in dates:
            continue
        payload = read_context_js(path)
        candidates = payload.get("candidates", [])
        codes = [str(row.get("code", "")).zfill(6) for row in candidates]
        run_date = (payload.get("meta") or {}).get("run_date") or iso_date(stamp)
        industries = dashboard_vcp.load_industry_context(codes, run_date)
        for row in candidates:
            context = industries.get(str(row.get("code", "")).zfill(6), {})
            row["sw_l2_name"] = context.get("sw_l2_name", "")
            row["sector"] = context.get("sector", {})
        write_context_js(path, "QUANT_DASHBOARD_VCP_CONTEXTS", payload)
        updated += 1
    return updated


def refresh_signal_environment(dates: set[str]) -> int:
    updated = 0
    for path in sorted(LIVE_DASHBOARD_DATA.glob("*/signals_context_*.js")):
        stamp = path.stem.rsplit("_", 1)[-1]
        if stamp not in dates:
            continue
        payload = read_context_js(path)
        market = dashboard_signals.market_notice(stamp)
        sectors = dashboard_signals.sector_notices(stamp)
        for row in payload.get("signals", []):
            code = str(row.get("code", "")).zfill(6)
            sector = dict(sectors.get(code, dashboard_signals.default_sector_notice()))
            row.update(sector)
            row.update(dashboard_signals.position_guidance(row, market, sector))
        payload["market_notice"] = market
        payload.setdefault("meta", {})["position_strategy_version"] = dashboard_signals.PLAN_CONFIG["strategy_version"]
        write_context_js(path, "QUANT_DASHBOARD_SIGNALS_CONTEXTS", payload)
        updated += 1
    return updated


def publish(dates: list[str], workspace: Path, protected_before: dict[str, str]) -> dict:
    shadow_market = workspace / "market"
    shadow_dashboard = workspace / "dashboard" / "data"
    backup = backup_live(dates)
    replace_state_tables(workspace / "cache" / "market_regime" / "market_regime.sqlite")
    for stamp in dates:
        for name in (f"market_regime_{stamp}.json", f"sector_heat_{stamp}.csv", f"market_regime_{stamp}.md"):
            source = shadow_market / name
            if source.exists():
                atomic_copy(source, LIVE_MARKET / name)
        source = shadow_market / "data" / f"market_context_{stamp}.json"
        if source.exists():
            atomic_copy(source, LIVE_MARKET / "data" / source.name)
        source = shadow_dashboard / f"20{stamp[:4]}" / f"market_context_{stamp}.js"
        if source.exists():
            atomic_copy(source, LIVE_DASHBOARD_DATA / f"20{stamp[:4]}" / source.name)
    latest_source = shadow_market / "data" / "latest.json"
    if latest_source.exists():
        atomic_copy(latest_source, LIVE_MARKET / "data" / "latest.json")
    vcp_count = refresh_vcp_environment(set(dates))
    signal_count = refresh_signal_environment(set(dates))
    market_regime.DASHBOARD_DATA_DIR = LIVE_DASHBOARD_DATA
    market_regime.write_dashboard_index(dates[-1], dates)
    if protected_before != protected_hashes():
        raise RuntimeError(f"发布后受保护文件哈希变化；备份位于 {backup}")
    result = {"backup": str(backup), "vcp_pages": vcp_count, "signal_pages": signal_count}
    (workspace / "publish.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="隔离回放并刷新历史市场/板块环境派生数据")
    parser.add_argument("--from-date")
    parser.add_argument("--to-date")
    parser.add_argument("--workspace", type=Path, default=DEFAULT_WORKSPACE)
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    args.workspace = validate_workspace(args.workspace)
    dates = available_dates(args.from_date, args.to_date)
    if not dates:
        raise RuntimeError("指定范围没有市场报告")
    protected_before = protected_hashes()
    audit = replay(dates, args.workspace)
    result = {"audit": audit}
    if args.publish:
        result["publish"] = publish(dates, args.workspace, protected_before)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
