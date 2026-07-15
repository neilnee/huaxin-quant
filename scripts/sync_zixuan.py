#!/usr/bin/env python3
"""Refresh Huaxin-managed Eastmoney watchlist entries from Bloom.

Only stocks recorded in the local managed ledger may be deleted. The broader
Eastmoney all-watchlist group can contain user-maintained stocks and is never
queried by this workflow.
"""

import argparse
import csv
import os
import random
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT


MANAGE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/self-select/manage"

ROOT = Path(PROJECT_ROOT)
BLOOM_STATE_PATH = ROOT / "bloom" / "state" / "bloom_state.csv"
CACHE_DIR = ROOT / "cache" / "zixuan"
MANAGED_PATH = CACHE_DIR / "target_watchlist.csv"
LEGACY_MANAGED_PATH = CACHE_DIR / "managed_watchlist.csv"

BUY_SIGNALS = {"PULLBACK_BUY", "BREAKOUT_BUY", "RETEST_BUY"}
MATURE_STAGES = {"VCP_TIGHT", "VCP_MATURE"}
SCORE_STAGES = {"VCP_FORMING", "VCP_EARLY"}
FOCUS_MIN_SCORE = 60.0

MANAGE_INTERVAL_SECONDS = 1.2
BATCH_SIZE = 5
BATCH_PAUSE_SECONDS = 8.0
RETRY_PAUSE_SECONDS = 15.0

MANAGED_FIELDS = [
    "code", "name", "date", "selection", "model2_stage",
    "structure_score", "model2_setup_signal", "bloom_status",
]


class SyncError(RuntimeError):
    """Expected synchronization failure with a user-facing message."""


def normalize_code(value):
    text = str(value or "").replace('="', "").replace('"', "").strip()
    match = re.search(r"(?<!\d)(\d{6})(?!\d)", text)
    return match.group(1) if match else ""


def safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def normalize_date_arg(value):
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{6}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m-%d").strftime("%y%m%d")
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def date_to_iso(date_yy):
    return datetime.strptime(date_yy, "%y%m%d").strftime("%Y-%m-%d")


def latest_bloom_date(path=None):
    path = Path(path) if path is not None else BLOOM_STATE_PATH
    if not path.exists():
        raise SyncError(f"Bloom 状态文件不存在: {path}")
    latest = ""
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            value = str(row.get("last_seen") or "")
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                latest = max(latest, value)
    if not latest:
        raise SyncError("Bloom 状态中没有可用的 last_seen 日期")
    return datetime.strptime(latest, "%Y-%m-%d").strftime("%y%m%d")


def target_row(row, date_iso):
    """Apply the exact Bloom markdown focus rule plus active buy signals."""
    if str(row.get("last_seen") or "") != date_iso:
        return None
    code = normalize_code(row.get("code"))
    if not code:
        return None

    stage = str(row.get("model2_stage") or "").upper()
    setup = str(row.get("model2_setup_signal") or "").upper()
    bloom_status = str(row.get("bloom_status") or "").upper()
    score = safe_float(row.get("structure_score"))
    triggered = setup in BUY_SIGNALS or bloom_status == "TRIGGERED"
    focus = stage in MATURE_STAGES or (stage in SCORE_STAGES and score >= FOCUS_MIN_SCORE)
    if not focus and not triggered:
        return None

    selection = (
        "FOCUS_AND_SETUP_TRIGGER" if focus and triggered
        else "SETUP_TRIGGER" if triggered
        else "FOCUS"
    )
    return {
        "code": code,
        "name": str(row.get("name") or "").strip(),
        "selection": selection,
        "model2_stage": stage,
        "structure_score": row.get("structure_score", ""),
        "model2_setup_signal": setup,
        "bloom_status": bloom_status,
    }


def read_bloom_targets(date_yy, path=None):
    path = Path(path) if path is not None else BLOOM_STATE_PATH
    if not path.exists():
        raise SyncError(f"Bloom 状态文件不存在: {path}")
    date_iso = date_to_iso(date_yy)
    required = {
        "code", "name", "last_seen", "model2_stage", "structure_score",
        "model2_setup_signal", "bloom_status",
    }
    targets = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise SyncError(f"Bloom 状态缺少字段: {', '.join(sorted(missing))}")
        for row in reader:
            selected = target_row(row, date_iso)
            if selected:
                targets[selected["code"]] = selected
    if not targets:
        raise SyncError(
            f"{date_iso} 没有 Bloom 重点观察或买点标的，拒绝执行自选刷新"
        )
    return targets


def read_managed(path=None):
    """Read only the local ledger; never infer deletion candidates remotely."""
    path = Path(path) if path is not None else MANAGED_PATH
    source = path if path.exists() else LEGACY_MANAGED_PATH
    if not source.exists():
        return {}
    managed = {}
    with source.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = normalize_code(row.get("code"))
            if not code:
                continue
            managed[code] = {
                "code": code,
                "name": str(row.get("name") or "").strip(),
                "date": str(row.get("date") or row.get("added_at") or ""),
                "selection": str(row.get("selection") or "LEGACY_MANAGED"),
                "model2_stage": str(row.get("model2_stage") or ""),
                "structure_score": str(row.get("structure_score") or ""),
                "model2_setup_signal": str(row.get("model2_setup_signal") or ""),
                "bloom_status": str(row.get("bloom_status") or ""),
            }
    return managed


def api_headers(api_key):
    return {"Content-Type": "application/json", "apikey": api_key}


def manage_watchlist(api_key, code, action, session=requests):
    query = (
        f"把{code}添加到我的自选股列表" if action == "add"
        else f"把{code}从我的自选股列表删除"
    )
    for attempt in range(3):
        try:
            response = session.post(
                MANAGE_URL, headers=api_headers(api_key), json={"query": query}, timeout=30
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            message = str(exc)
        else:
            if payload.get("status") == 0 or payload.get("code") == 0:
                return True, payload.get("message") or "OK"
            message = payload.get("message") or f"code={payload.get('code')}"
            if payload.get("code") not in {112, 113}:
                return False, message
        if attempt < 2:
            time.sleep(RETRY_PAUSE_SECONDS * (attempt + 1) + random.uniform(0, 1))
    return False, message


def atomic_write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    with tmp_path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
    os.replace(tmp_path, path)


def sort_key(row):
    stage_rank = {"VCP_TIGHT": 4, "VCP_MATURE": 3, "VCP_FORMING": 2, "VCP_EARLY": 1}
    return (
        0 if "SETUP_TRIGGER" in row["selection"] else 1,
        -stage_rank.get(row["model2_stage"], 0),
        -safe_float(row["structure_score"]),
        row["code"],
    )


def print_stocks(title, rows, marker):
    if not rows:
        return
    print(f"\n{title} ({len(rows)} 只):")
    for row in sorted(rows, key=sort_key):
        print(f"  {marker} {row['code']} {row['name']} [{row['selection']}]")


def wait_between_operations(index, total):
    if index >= total:
        return
    time.sleep(MANAGE_INTERVAL_SECONDS + random.uniform(0, 0.2))
    if index % BATCH_SIZE == 0:
        time.sleep(BATCH_PAUSE_SECONDS + random.uniform(0, 1))


def write_managed(managed):
    atomic_write_csv(MANAGED_PATH, MANAGED_FIELDS, sorted(managed.values(), key=sort_key))


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="刷新工作流管理的东方财富自选（Bloom 重点观察 + 买点）")
    parser.add_argument("--date", help="Bloom 日期，YYMMDD 或 YYYY-MM-DD；默认使用状态表最新日期")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认，供 daily.py 调用")
    parser.add_argument("--dry-run", action="store_true", help="只展示本地账本差异，不操作远端或缓存")
    return parser.parse_args(argv)


def run(args, session=requests):
    api_key = os.environ.get("MX_APIKEY", "").strip()
    if not api_key:
        raise SyncError("环境变量 MX_APIKEY 未设置")
    date_yy = args.date or latest_bloom_date()
    targets = read_bloom_targets(date_yy)
    previous = read_managed()

    remove_codes = sorted(previous)
    # Eastmoney places later additions nearer the front. Add the least important
    # Bloom targets first so the final visual order is highest priority first.
    add_codes = sorted(targets, key=lambda code: sort_key(targets[code]), reverse=True)
    remove_rows = [previous[code] for code in remove_codes]
    add_rows = [targets[code] for code in add_codes]

    print(f"Bloom 日期: {date_yy}")
    print(f"本地系统受管: {len(previous)} 只")
    print_stocks("移出并删除", remove_rows, "-")
    print_stocks("新增并添加", add_rows, "+")
    print("每日刷新模式：上一日受管股票均会删除后重新添加。")

    if args.dry_run:
        print("\n--dry-run：未调用东方财富接口，未改写本地缓存。")
        return 0
    if not args.yes:
        try:
            answer = input("\n确认按以上本地账本刷新系统自选？(y/N): ").strip().lower()
        except EOFError:
            answer = ""
        if answer != "y":
            print("已取消。")
            return 0

    delete_results = {}
    for index, code in enumerate(remove_codes, 1):
        ok, message = manage_watchlist(api_key, code, "delete", session=session)
        delete_results[code] = ok
        print(f"删除 [{index}/{len(remove_codes)}] {code}: {'成功' if ok else '失败'} {message}")
        wait_between_operations(index, len(remove_codes))

    if remove_codes and add_codes:
        time.sleep(2)
    add_results = {}
    for index, code in enumerate(add_codes, 1):
        ok, message = manage_watchlist(api_key, code, "add", session=session)
        add_results[code] = ok
        print(f"添加 [{index}/{len(add_codes)}] {code}: {'成功' if ok else '失败'} {message}")
        wait_between_operations(index, len(add_codes))

    next_managed = {
        code: row for code, row in previous.items() if not delete_results.get(code)
    }
    next_managed.update({
        code: targets[code] for code, ok in add_results.items() if ok
    })
    write_managed(next_managed)

    failures = [
        ("delete", code) for code, ok in delete_results.items() if not ok
    ] + [
        ("add", code) for code, ok in add_results.items() if not ok
    ]
    if failures:
        raise SyncError(f"同步有 {len(failures)} 个接口调用失败；已写入可恢复的本地账本")

    print(f"\n同步完成: 本地系统受管 {len(next_managed)} 只，当前 Bloom 目标 {len(targets)} 只")
    print(f"受管账本: {MANAGED_PATH}")
    return 0


def main(argv=None):
    args = parse_args(argv)
    try:
        return run(args)
    except (SyncError, ValueError) as exc:
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
