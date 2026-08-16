#!/usr/bin/env python3
"""Import existing strategy artifacts without rerunning strategy calculations."""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.data.strategy_data_store import (
    DB_PATH,
    connect,
    replace_bloom_events,
    save_bloom,
    save_buy_point_events,
    save_quant,
    save_signal_plan,
)
from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)
DATE_RE = re.compile(r"(\d{6})")


def date_from_path(path: Path) -> str:
    match = DATE_RE.search(path.stem)
    if not match:
        raise ValueError(f"date missing from {path}")
    value = match.group(1)
    return f"20{value[:2]}-{value[2:4]}-{value[4:]}"


def selected(path: Path, start: str | None, end: str | None) -> bool:
    value = date_from_path(path)
    return (not start or value >= start) and (not end or value <= end)


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_state(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def migrate(conn: sqlite3.Connection, start: str | None = None, end: str | None = None) -> dict:
    counts = defaultdict(int)
    quant_files = sorted((ROOT / "cache" / "quant_runs").glob("quant_[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    for path in quant_files:
        if selected(path, start, end):
            payload = load_json(path)
            save_quant(conn, payload, str(path.relative_to(ROOT)))
            counts["quant_documents"] += 1
            counts["quant_rows"] += len(payload.get("results", []))

    plan_files = sorted((ROOT / "signal_plan").glob("signal_plan_[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    for path in plan_files:
        if selected(path, start, end):
            payload = load_json(path)
            save_signal_plan(conn, payload, str(path.relative_to(ROOT)))
            counts["plan_documents"] += 1
            counts["plan_rows"] += len(payload.get("plans", []))

    bloom_files = sorted((ROOT / "bloom" / "state").glob("bloom_input_[0-9][0-9][0-9][0-9][0-9][0-9].json"))
    latest_bloom_date = date_from_path(bloom_files[-1]) if bloom_files else None
    current_state = read_state(ROOT / "bloom" / "state" / "bloom_state.csv")
    for path in bloom_files:
        if selected(path, start, end):
            payload = load_json(path)
            rows = current_state if date_from_path(path) == latest_bloom_date else None
            save_bloom(conn, payload, rows, source_path=str(path.relative_to(ROOT)))
            counts["bloom_documents"] += 1

    events_by_date = defaultdict(list)
    event_path = ROOT / "bloom" / "state" / "bloom_events.jsonl"
    if event_path.exists():
        for line in event_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                event = json.loads(line)
                value = str(event.get("date") or "")
                if (not start or value >= start) and (not end or value <= end):
                    events_by_date[value].append(event)
    for value, events in events_by_date.items():
        replace_bloom_events(conn, value, events)
        counts["bloom_events"] += len(events)
    conn.commit()

    if quant_files:
        from scripts import backtest

        last = end or date_from_path(quant_files[-1])
        market = sqlite3.connect(backtest.MARKET_DB)
        try:
            calendar = backtest.trading_calendar(market, last)
        finally:
            market.close()
        events = backtest.discover_events(last.replace("-", "")[2:], calendar)
        if start:
            events = [event for event in events if event.get("entry_date", "") >= start]
        save_buy_point_events(conn, events)
        counts["buy_point_events"] = len(events)
    return dict(counts)


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate existing strategy files into SQLite")
    parser.add_argument("--from-date", dest="start")
    parser.add_argument("--to-date", dest="end")
    parser.add_argument("--database", default=str(DB_PATH))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    normalize = lambda value: None if not value else (
        value if "-" in value else f"20{value[:2]}-{value[2:4]}-{value[4:]}"
    )
    if args.dry_run:
        target = Path(args.database)
        uri = ":memory:"
    else:
        target = Path(args.database)
        uri = target
    with connect(uri) as conn:
        counts = migrate(conn, normalize(args.start), normalize(args.end))
    print(json.dumps({"database": str(target), "dry_run": args.dry_run, **counts}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
