"""SQLite persistence for normalized Global Macro observations and source health."""

from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path

from scripts.data.global_macro_sources import SourceResult


SCHEMA = """
CREATE TABLE IF NOT EXISTS source_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    ok INTEGER NOT NULL,
    http_status INTEGER,
    latency_ms INTEGER NOT NULL,
    content_type TEXT NOT NULL,
    content_bytes INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    latest_observation_date TEXT,
    source_url TEXT NOT NULL,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_source_health_source_checked
    ON source_health(source_id, checked_at);

CREATE TABLE IF NOT EXISTS observations (
    source_id TEXT NOT NULL,
    series_id TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    value REAL NOT NULL,
    unit TEXT NOT NULL,
    frequency TEXT NOT NULL,
    source_url TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(source_id, series_id, observed_at)
);
CREATE INDEX IF NOT EXISTS idx_observations_series_date
    ON observations(series_id, observed_at);

CREATE TABLE IF NOT EXISTS events (
    source_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    published_at TEXT,
    event_time TEXT,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    category TEXT NOT NULL,
    official_fact TEXT NOT NULL,
    raw_json TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY(source_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at);
"""


class GlobalMacroStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def save_health(self, result: SourceResult) -> None:
        with self.connect() as conn:
            conn.execute(
                """INSERT INTO source_health(
                   source_id,checked_at,ok,http_status,latency_ms,content_type,content_bytes,
                   content_hash,latest_observation_date,source_url,error
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    result.source_id, result.checked_at, int(result.ok), result.http_status,
                    result.latency_ms, result.content_type, result.content_bytes,
                    result.content_hash, result.latest_observation_date, result.url, result.error,
                ),
            )

    def save_payload(self, result: SourceResult) -> tuple[int, int]:
        if not result.ok:
            return 0, 0
        with self.connect() as conn:
            for row in result.observations:
                conn.execute(
                    """INSERT INTO observations VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id,series_id,observed_at) DO UPDATE SET
                       value=excluded.value,unit=excluded.unit,frequency=excluded.frequency,
                       source_url=excluded.source_url,raw_json=excluded.raw_json,fetched_at=excluded.fetched_at""",
                    (
                        row["source_id"], row["series_id"], row["observed_at"], row["value"],
                        row["unit"], row["frequency"], row["source_url"],
                        json.dumps(row["raw"], ensure_ascii=False, sort_keys=True), result.checked_at,
                    ),
                )
            for row in result.events:
                conn.execute(
                    """INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(source_id,event_id) DO UPDATE SET
                       published_at=excluded.published_at,event_time=excluded.event_time,
                       title=excluded.title,url=excluded.url,category=excluded.category,
                       official_fact=excluded.official_fact,raw_json=excluded.raw_json,fetched_at=excluded.fetched_at""",
                    (
                        row["source_id"], row["event_id"], row["published_at"], row["event_time"],
                        row["title"], row["url"], row["category"], row["official_fact"],
                        json.dumps(row["raw"], ensure_ascii=False, sort_keys=True), result.checked_at,
                    ),
                )
        return len(result.observations), len(result.events)

    def health_summary(self, source_configs: dict, as_of: date, days: int, green_rate: float) -> list[dict]:
        since = (as_of - timedelta(days=max(days - 1, 0))).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                """SELECT source_id,COUNT(*) attempts,SUM(ok) successes,
                          CAST(AVG(latency_ms) AS INTEGER) average_latency_ms,
                          MAX(CASE WHEN ok=1 THEN checked_at END) last_success_at,
                          MAX(CASE WHEN ok=1 THEN latest_observation_date END) latest_observation_date,
                          COUNT(DISTINCT CASE WHEN ok=1 THEN content_hash END) content_versions
                   FROM source_health WHERE substr(checked_at,1,10)>=? AND substr(checked_at,1,10)<=?
                   GROUP BY source_id ORDER BY source_id""",
                (since, as_of.isoformat()),
            ).fetchall()
            errors = {
                row["source_id"]: row["error"]
                for row in conn.execute(
                    """SELECT h.source_id,h.error FROM source_health h JOIN (
                       SELECT source_id,MAX(id) id FROM source_health GROUP BY source_id
                       ) x ON h.id=x.id"""
                )
            }
        indexed = {row["source_id"]: dict(row) for row in rows}
        result = []
        for source_id, config in source_configs.items():
            if not config.get("enabled"):
                continue
            row = indexed.get(source_id)
            if not row:
                result.append({"source_id": source_id, "grade": "UNKNOWN", "attempts": 0, "success_rate": None})
                continue
            attempts, successes = int(row["attempts"]), int(row["successes"] or 0)
            rate = successes / attempts
            stale = False
            freshness = config.get("freshness_days")
            latest = row.get("latest_observation_date")
            if config.get("kind") == "data" and freshness is not None:
                stale = not latest or (as_of - date.fromisoformat(latest)).days > int(freshness)
            grade = "GREEN" if rate >= green_rate and successes and not stale else "YELLOW" if successes else "RED"
            result.append({
                **row,
                "source_id": source_id,
                "success_rate": round(rate, 4),
                "stale": stale,
                "grade": grade,
                "last_error": errors.get(source_id),
            })
        return result

    def counts(self) -> dict:
        with self.connect() as conn:
            return {
                "health": conn.execute("SELECT COUNT(*) FROM source_health").fetchone()[0],
                "observations": conn.execute("SELECT COUNT(*) FROM observations").fetchone()[0],
                "events": conn.execute("SELECT COUNT(*) FROM events").fetchone()[0],
            }
