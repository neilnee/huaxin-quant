"""SQLite persistence for capital-flow source data and industry mappings."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)
CACHE_DIR = ROOT / "cache" / "capital_flow"
DB_PATH = CACHE_DIR / "capital_data.sqlite"


def connect_capital_db(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    create_capital_schema(conn)
    return conn


def create_capital_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS board_snapshot_runs (
            snapshot_date TEXT NOT NULL,
            fortune_level INTEGER NOT NULL,
            status TEXT NOT NULL,
            board_count INTEGER NOT NULL,
            member_count INTEGER NOT NULL,
            source_hash TEXT,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, fortune_level)
        );
        CREATE TABLE IF NOT EXISTS fortune_industry_boards (
            snapshot_date TEXT NOT NULL,
            fortune_level INTEGER NOT NULL,
            bk_code TEXT NOT NULL,
            bk_name TEXT NOT NULL,
            member_count INTEGER NOT NULL,
            source_hash TEXT NOT NULL,
            PRIMARY KEY (snapshot_date, fortune_level, bk_code)
        );
        CREATE TABLE IF NOT EXISTS fortune_industry_members (
            snapshot_date TEXT NOT NULL,
            fortune_level INTEGER NOT NULL,
            bk_code TEXT NOT NULL,
            stock_code TEXT NOT NULL,
            stock_name TEXT,
            PRIMARY KEY (snapshot_date, fortune_level, bk_code, stock_code)
        );
        CREATE INDEX IF NOT EXISTS idx_fortune_members_code
            ON fortune_industry_members(snapshot_date, fortune_level, stock_code);
        CREATE TABLE IF NOT EXISTS mapping_versions (
            mapping_version TEXT PRIMARY KEY,
            sw_level INTEGER NOT NULL,
            fortune_level INTEGER NOT NULL,
            sw_snapshot_date TEXT NOT NULL,
            fortune_snapshot_date TEXT NOT NULL,
            as_of TEXT NOT NULL,
            valid_from TEXT NOT NULL,
            valid_to TEXT,
            status TEXT NOT NULL,
            detail_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sector_mappings (
            mapping_version TEXT NOT NULL,
            sw_level INTEGER NOT NULL,
            fortune_level INTEGER NOT NULL,
            sw_code TEXT NOT NULL,
            sw_name TEXT NOT NULL,
            bk_code TEXT,
            bk_name TEXT,
            mapping_rank INTEGER NOT NULL,
            coverage REAL NOT NULL,
            incremental_coverage REAL NOT NULL,
            combined_coverage REAL NOT NULL,
            purity REAL NOT NULL,
            core_coverage REAL NOT NULL,
            jaccard REAL NOT NULL,
            mapping_score REAL NOT NULL,
            mapping_weight REAL NOT NULL,
            mapping_quality TEXT NOT NULL,
            PRIMARY KEY (mapping_version, sw_code, mapping_rank),
            FOREIGN KEY (mapping_version) REFERENCES mapping_versions(mapping_version)
        );
        CREATE INDEX IF NOT EXISTS idx_sector_mapping_sw
            ON sector_mappings(sw_level, sw_code, mapping_version);
        CREATE TABLE IF NOT EXISTS source_contracts (
            entity_type TEXT NOT NULL,
            entity_code TEXT NOT NULL,
            metric_name TEXT NOT NULL,
            source_field_code TEXT NOT NULL,
            source_field_name TEXT NOT NULL,
            last_seen_at TEXT NOT NULL,
            PRIMARY KEY (entity_type, entity_code, metric_name)
        );
        CREATE TABLE IF NOT EXISTS sector_capital (
            bk_code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            source_query TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (bk_code, trade_date)
        );
        CREATE TABLE IF NOT EXISTS stock_capital (
            code TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            metrics_json TEXT NOT NULL,
            source_query TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            PRIMARY KEY (code, trade_date)
        );
        """
    )
    conn.execute("INSERT OR REPLACE INTO schema_meta(key,value) VALUES('schema_version','1')")
    conn.commit()
