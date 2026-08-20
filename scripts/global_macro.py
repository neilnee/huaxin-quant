#!/usr/bin/env python3
"""CLI for Global Macro official-source health and P0 collection."""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))

from scripts.data.global_macro_sources import GlobalMacroClient, validate_source_ids
from scripts.data.global_macro_store import GlobalMacroStore
from scripts.io_utils import FileLock, atomic_write_json
from scripts.shared import PROJECT_ROOT, normalize_date_arg
from scripts.strategy_config import load_strategy_config


DEFAULT_DB = Path(PROJECT_ROOT) / "cache" / "global_macro" / "global_macro.sqlite"
OUTPUT_DIR = Path(PROJECT_ROOT) / "macro"


def parse_source_list(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def parse_as_of(value: str | None) -> date:
    return datetime.strptime(normalize_date_arg(value), "%Y-%m-%d").date() if value else date.today()


def emit(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def write_runtime_summary(name: str, as_of: date, payload: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    atomic_write_json(OUTPUT_DIR / f"{name}_{as_of.strftime('%y%m%d')}.json", payload)
    atomic_write_json(OUTPUT_DIR / f"{name}_latest.json", payload)


def run_sources(args: argparse.Namespace, config: dict, save_payload: bool) -> tuple[dict, int]:
    store = GlobalMacroStore(args.db)
    source_ids = validate_source_ids(config, parse_source_list(args.sources))
    client = GlobalMacroClient(config)
    attempts = 1 if save_payload else args.attempts
    source_rows = []
    for source_id in source_ids:
        attempt_rows = []
        for attempt in range(1, attempts + 1):
            result = client.fetch(source_id, args.as_of)
            store.save_health(result)
            observations = events = 0
            if save_payload and result.ok:
                observations, events = store.save_payload(result)
            attempt_rows.append({
                "attempt": attempt,
                "ok": result.ok,
                "http_status": result.http_status,
                "latency_ms": result.latency_ms,
                "latest_observation_date": result.latest_observation_date,
                "parsed_observations": len(result.observations),
                "parsed_events": len(result.events),
                "stored_observations": observations,
                "stored_events": events,
                "error": result.error,
            })
        source_rows.append({
            "source_id": source_id,
            "ok": any(row["ok"] for row in attempt_rows),
            "attempts": attempt_rows,
        })
    succeeded = sum(row["ok"] for row in source_rows)
    status = "complete" if succeeded == len(source_rows) else "partial" if succeeded else "failed"
    payload = {
        "schema": "global_macro_run_v1",
        "command": "fetch" if save_payload else "probe",
        "as_of": args.as_of.isoformat(),
        "status": status,
        "succeeded": succeeded,
        "requested": len(source_rows),
        "sources": source_rows,
        "database_counts": store.counts(),
    }
    write_runtime_summary(payload["command"], args.as_of, payload)
    return payload, 0 if succeeded else 1


def command_status(args: argparse.Namespace, config: dict) -> tuple[dict, int]:
    store = GlobalMacroStore(args.db)
    rows = store.health_summary(
        config["sources"], args.as_of, args.days, float(config["health"]["green_success_rate"])
    )
    payload = {
        "schema": "global_macro_health_v1",
        "as_of": args.as_of.isoformat(),
        "window_days": args.days,
        "summary": {grade: sum(row["grade"] == grade for row in rows) for grade in ("GREEN", "YELLOW", "RED", "UNKNOWN")},
        "sources": rows,
        "database_counts": store.counts(),
    }
    write_runtime_summary("health", args.as_of, payload)
    return payload, 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="全球宏观与流动性官方信源数据层")
    root.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite 缓存路径")
    root.add_argument("--as-of", dest="root_as_of", help="目标日期，默认今天")
    sub = root.add_subparsers(dest="command", required=True)

    probe = sub.add_parser("probe", help="重复探测信源健康，不写入业务数据")
    probe.add_argument("--sources", help="逗号分隔的信源 ID")
    probe.add_argument("--attempts", type=int, default=2, choices=range(1, 6))
    probe.add_argument("--as-of", dest="command_as_of", help="目标日期，默认今天")

    fetch = sub.add_parser("fetch", help="采集并缓存 P0 数据与官方事件")
    fetch.add_argument("--sources", help="逗号分隔的信源 ID")
    fetch.add_argument("--as-of", dest="command_as_of", help="目标日期，默认今天")

    status = sub.add_parser("status", help="汇总最近信源健康")
    status.add_argument("--days", type=int, default=None)
    status.add_argument("--as-of", dest="command_as_of", help="目标日期，默认今天")
    return root


def main() -> int:
    args = parser().parse_args()
    config = load_strategy_config("global-macro.json")[0]
    args.as_of = parse_as_of(args.command_as_of or args.root_as_of)
    if getattr(args, "days", None) is None:
        args.days = int(config["health"]["default_window_days"])
    if args.days < 1:
        emit({"status": "error", "error": "days 必须大于零"})
        return 1
    try:
        lock_path = Path(PROJECT_ROOT) / ".tmp" / "locks" / "global_macro.lock"
        with FileLock(lock_path, purpose=f"global_macro:{args.command}"):
            if args.command == "probe":
                payload, code = run_sources(args, config, save_payload=False)
            elif args.command == "fetch":
                payload, code = run_sources(args, config, save_payload=True)
            else:
                payload, code = command_status(args, config)
        emit(payload)
        return code
    except Exception as exc:
        emit({"status": "error", "error": str(exc)})
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
