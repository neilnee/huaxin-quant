#!/usr/bin/env python3
"""Regenerate compatibility artifacts from strategy_data.sqlite."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.data.strategy_data_store import (
    DB_PATH,
    connect,
    load_all_bloom_events,
    load_bloom_state,
    load_document,
)
from scripts.io_utils import atomic_write_csv, atomic_write_json, atomic_write_text
from scripts.shared import PROJECT_ROOT


ROOT = Path(PROJECT_ROOT)


def normalize_date(value: str) -> tuple[str, str]:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 6:
        return f"20{digits[:2]}-{digits[2:4]}-{digits[4:]}", digits
    if len(digits) == 8:
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}", digits[2:]
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def ordered_bloom_state(state: dict[str, dict]) -> list[dict]:
    from scripts.bloom import is_active_status, safe_float

    rows = [row for row in state.values() if row.get("bloom_status") != "EXIT"]
    rows.sort(key=lambda row: (
        0 if is_active_status(row.get("bloom_status")) else 1,
        -safe_float(row.get("structure_score")),
        row.get("code", ""),
    ))
    return rows


def publish_quant(conn, date_iso: str, date_yy: str) -> list[Path]:
    from scripts.quant_filter import should_write_to_quant, write_csv

    payload = load_document(conn, "quant", date_iso)
    if payload is None:
        raise FileNotFoundError(f"quant document missing: {date_iso}")
    json_path = ROOT / "cache" / "quant_runs" / f"quant_{date_yy}.json"
    csv_path = ROOT / "quant" / f"quant_{date_yy}.csv"
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    rows = [row for row in payload.get("results", []) if should_write_to_quant(row)]
    rows.sort(key=lambda row: row.get("structure_score") or 0, reverse=True)
    if rows:
        write_csv(rows, str(csv_path))
    return [json_path, csv_path]


def publish_signal_plan(conn, date_iso: str, date_yy: str) -> list[Path]:
    from scripts.signal_plan import build_markdown

    payload = load_document(conn, "signal_plan", date_iso)
    if payload is None:
        raise FileNotFoundError(f"signal plan document missing: {date_iso}")
    json_path = ROOT / "signal_plan" / f"signal_plan_{date_yy}.json"
    md_path = ROOT / "signal_plan" / f"signal_plan_{date_yy}.md"
    atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2))
    atomic_write_text(md_path, build_markdown(payload))
    return [json_path, md_path]


def publish_bloom(conn, date_iso: str, date_yy: str) -> list[Path]:
    from scripts.bloom import STATE_FIELDS, build_markdown

    payload = load_document(conn, "bloom", date_iso)
    if payload is None:
        raise FileNotFoundError(f"bloom document missing: {date_iso}")
    input_path = ROOT / "bloom" / "state" / f"bloom_input_{date_yy}.json"
    report_path = ROOT / "bloom" / f"bloom_{date_yy}.md"
    state_path = ROOT / "bloom" / "state" / "bloom_state.csv"
    event_path = ROOT / "bloom" / "state" / "bloom_events.jsonl"
    atomic_write_json(input_path, payload)
    atomic_write_text(report_path, build_markdown(payload))
    state = load_bloom_state(conn, date_iso)
    active = ordered_bloom_state(state)
    atomic_write_csv(state_path, STATE_FIELDS, active)
    content = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                      for row in load_all_bloom_events(conn))
    atomic_write_text(event_path, content)
    return [input_path, report_path, state_path, event_path]


def main() -> int:
    parser = argparse.ArgumentParser(description="Publish strategy compatibility files from SQLite")
    parser.add_argument("module", choices=("quant", "signal-plan", "bloom", "all"))
    parser.add_argument("--date", required=True)
    parser.add_argument("--database", default=str(DB_PATH))
    args = parser.parse_args()
    date_iso, date_yy = normalize_date(args.date)
    with connect(args.database) as conn:
        outputs = []
        if args.module in ("quant", "all"):
            outputs.extend(publish_quant(conn, date_iso, date_yy))
        if args.module in ("signal-plan", "all"):
            outputs.extend(publish_signal_plan(conn, date_iso, date_yy))
        if args.module in ("bloom", "all"):
            outputs.extend(publish_bloom(conn, date_iso, date_yy))
    for path in outputs:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
