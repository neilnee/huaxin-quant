"""Shared SQLite store for reusable A-share market data."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path

from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)
CACHE_DIR = ROOT / "cache" / "market_data"
DB_PATH = CACHE_DIR / "market_data.sqlite"
BLOCK_DIR = CACHE_DIR / "blocks"
LEGACY_CACHE_DIR = ROOT / "cache" / "market_regime"
LEGACY_DB_PATH = LEGACY_CACHE_DIR / "market_data.sqlite"


def _migrate_legacy_cache() -> None:
    """Copy the original market-regime data store once, preserving existing history."""
    if DB_PATH.exists() or not LEGACY_DB_PATH.exists():
        return
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(LEGACY_DB_PATH)
    target = sqlite3.connect(DB_PATH)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    legacy_blocks = LEGACY_CACHE_DIR / "blocks"
    if legacy_blocks.exists() and not BLOCK_DIR.exists():
        shutil.copytree(legacy_blocks, BLOCK_DIR)


def connect_db() -> sqlite3.Connection:
    _migrate_legacy_cache()
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS securities (
            code TEXT PRIMARY KEY, name TEXT NOT NULL, market INTEGER NOT NULL,
            is_st INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS universe_members (
            trade_date TEXT NOT NULL, code TEXT NOT NULL, eligible INTEGER NOT NULL,
            exclusion_reason TEXT, PRIMARY KEY (trade_date, code)
        );
        CREATE TABLE IF NOT EXISTS daily_bars (
            code TEXT NOT NULL, trade_date TEXT NOT NULL, open REAL NOT NULL, high REAL NOT NULL,
            low REAL NOT NULL, close REAL NOT NULL, volume REAL NOT NULL, amount REAL,
            source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY (code, trade_date)
        );
        CREATE INDEX IF NOT EXISTS idx_daily_bars_date ON daily_bars(trade_date);
        CREATE TABLE IF NOT EXISTS corporate_actions (
            code TEXT NOT NULL, ex_date TEXT NOT NULL,
            cash_dividend_per_10 REAL NOT NULL DEFAULT 0,
            bonus_shares_per_10 REAL NOT NULL DEFAULT 0,
            rights_shares_per_10 REAL NOT NULL DEFAULT 0,
            rights_price REAL NOT NULL DEFAULT 0,
            source TEXT NOT NULL, source_hash TEXT NOT NULL,
            first_seen_at TEXT NOT NULL, last_seen_at TEXT NOT NULL,
            PRIMARY KEY (code, ex_date, source)
        );
        CREATE INDEX IF NOT EXISTS idx_corporate_actions_date ON corporate_actions(ex_date);
        CREATE TABLE IF NOT EXISTS corporate_action_syncs (
            code TEXT NOT NULL, source TEXT NOT NULL, as_of_date TEXT NOT NULL,
            status TEXT NOT NULL, action_count INTEGER NOT NULL DEFAULT 0,
            source_hash TEXT, error_message TEXT, fetched_at TEXT NOT NULL,
            PRIMARY KEY (code, source, as_of_date)
        );
        CREATE TABLE IF NOT EXISTS adjustment_verifications (
            code TEXT NOT NULL, ex_date TEXT NOT NULL,
            primary_source TEXT NOT NULL, verification_source TEXT NOT NULL,
            status TEXT NOT NULL, sample_count INTEGER NOT NULL DEFAULT 0,
            max_factor_diff_pct REAL, max_raw_close_diff_pct REAL,
            detail_json TEXT NOT NULL, verified_at TEXT NOT NULL,
            PRIMARY KEY (code, ex_date, primary_source, verification_source)
        );
        CREATE TABLE IF NOT EXISTS block_snapshots (
            snapshot_date TEXT NOT NULL, block_kind TEXT NOT NULL, block_name TEXT NOT NULL,
            source_file TEXT NOT NULL, source_hash TEXT NOT NULL, member_count INTEGER NOT NULL,
            PRIMARY KEY (snapshot_date, block_kind, block_name)
        );
        CREATE TABLE IF NOT EXISTS block_members (
            snapshot_date TEXT NOT NULL, block_kind TEXT NOT NULL, block_name TEXT NOT NULL,
            code TEXT NOT NULL, PRIMARY KEY (snapshot_date, block_kind, block_name, code)
        );
        CREATE INDEX IF NOT EXISTS idx_block_members_date_code ON block_members(snapshot_date, code);
        CREATE TABLE IF NOT EXISTS industry_definitions (
            snapshot_date TEXT NOT NULL, industry_system TEXT NOT NULL, industry_code TEXT NOT NULL,
            industry_name TEXT NOT NULL, source_section TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, industry_system, industry_code)
        );
        CREATE TABLE IF NOT EXISTS stock_industries (
            snapshot_date TEXT NOT NULL, code TEXT NOT NULL, tdx_industry_code TEXT,
            sw_industry_code TEXT, PRIMARY KEY (snapshot_date, code)
        );
        CREATE INDEX IF NOT EXISTS idx_stock_industries_date_tdx ON stock_industries(snapshot_date, tdx_industry_code);
        CREATE TABLE IF NOT EXISTS ingestion_jobs (
            run_id TEXT NOT NULL, job_type TEXT NOT NULL, code TEXT NOT NULL, target_date TEXT NOT NULL,
            status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, error_message TEXT,
            updated_at TEXT NOT NULL, PRIMARY KEY (run_id, job_type, code, target_date)
        );
        CREATE TABLE IF NOT EXISTS run_log (
            run_id TEXT PRIMARY KEY, run_type TEXT NOT NULL, target_date TEXT NOT NULL, status TEXT NOT NULL,
            coverage_ratio REAL, detail_json TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT
        );
        """
    )
    conn.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', '3')")
    conn.commit()
