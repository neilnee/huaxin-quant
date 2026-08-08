#!/usr/bin/env python3
"""Fetch and cache point-in-time financial hints for stocks on the signal page."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import date, datetime
from pathlib import Path

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config

ROOT = Path(PROJECT_ROOT)
RUNS = ROOT / "cache" / "quant_runs"
PLANS = ROOT / "signal_plan"
POOLS = ROOT / "pool"
CACHE_DIR = ROOT / "cache" / "signal_fundamentals"
MX_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
CONFIG, CONFIG_PATH = load_strategy_config("signal-fundamentals.json")
QUERY_CONFIG = CONFIG["query"]
THRESHOLDS = CONFIG["risk_thresholds"]

FIELD_ALIASES = {
    "net_profit": ["归属于母公司股东的净利润"],
    "operating_cashflow": ["经营活动产生的现金流量净额"],
    "debt_ratio": ["资产负债率"],
    "revenue_growth": ["营业收入同比增长率", "营业总收入同比增长率"],
    "profit_growth": ["归属母公司股东的净利润同比增长率"],
    "gross_margin": ["销售毛利率"],
    "roe": ["净资产收益率ROE(加权)", "净资产收益率ROE"],
}


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() and key.strip() not in os.environ:
            os.environ[key.strip()] = value.strip().strip('"').strip("'")


def normalize_date(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 6:
        return datetime.strptime(digits, "%y%m%d").strftime("%Y-%m-%d")
    if len(digits) == 8:
        return datetime.strptime(digits, "%Y%m%d").strftime("%Y-%m-%d")
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def date_yy(iso: str) -> str:
    return datetime.strptime(iso, "%Y-%m-%d").strftime("%y%m%d")


def parse_period_label(value: object) -> dict | None:
    text = str(value or "").strip()
    match = re.search(r"(20\d{2})\s*(一季报|中报|三季报|年报)", text)
    if match:
        year = int(match.group(1))
        kind = match.group(2)
        month_day = {"一季报": (3, 31), "中报": (6, 30), "三季报": (9, 30), "年报": (12, 31)}[kind]
        available = date(year + 1, 4, 30) if kind == "年报" else {
            "一季报": date(year, 4, 30),
            "中报": date(year, 8, 31),
            "三季报": date(year, 10, 31),
        }[kind]
        return {
            "label": f"{year}{kind}",
            "report_end": date(year, *month_day).isoformat(),
            "available_from": available.isoformat(),
            "is_annual": kind == "年报",
        }
    match = re.search(r"(20\d{2})[-./](\d{1,2})[-./](\d{1,2})", text)
    if not match:
        return None
    end = date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
    kind = {3: "一季报", 6: "中报", 9: "三季报", 12: "年报"}.get(end.month)
    if not kind:
        return None
    return parse_period_label(f"{end.year}{kind}")


def expected_report_end(as_of: str) -> str:
    point = date.fromisoformat(as_of)
    year = point.year
    if point >= date(year, 10, 31):
        return date(year, 9, 30).isoformat()
    if point >= date(year, 8, 31):
        return date(year, 6, 30).isoformat()
    if point >= date(year, 4, 30):
        return date(year, 3, 31).isoformat()
    return date(year - 1, 9, 30).isoformat()


def number(value: object) -> float | None:
    if value in (None, "", "-"):
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def metric_key(name_map: dict, aliases: list[str]) -> str | None:
    candidates = []
    for key, raw_name in name_map.items():
        name = str(raw_name)
        if key == "headNameSub" or "单季度" in name or "扣除非经常" in name:
            continue
        for rank, alias in enumerate(aliases):
            if name == alias:
                candidates.append((0, rank, len(name), key))
            elif alias in name:
                candidates.append((1, rank, len(name), key))
    return min(candidates)[-1] if candidates else None


def response_tables(payload: dict) -> list[dict]:
    try:
        tables = payload["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
    except (KeyError, TypeError):
        return []
    return tables if isinstance(tables, list) else []


def extract_snapshot(payload: dict, code: str, as_of: str) -> dict | None:
    periods: dict[str, dict] = {}
    for table in response_tables(payload):
        tag = table.get("entityTagDTO") or {}
        table_code = str(tag.get("secuCode") or table.get("code") or "").split(".", 1)[0]
        if table_code and table_code != code:
            continue
        raw = table.get("rawTable") or {}
        heads = raw.get("headName") or []
        if not isinstance(raw, dict) or not isinstance(heads, list):
            continue
        keys = {metric: metric_key(table.get("nameMap") or {}, aliases) for metric, aliases in FIELD_ALIASES.items()}
        for index, head in enumerate(heads):
            period = parse_period_label(head)
            if not period or period["available_from"] > as_of:
                continue
            row = periods.setdefault(period["report_end"], {**period, "values": {}})
            for metric, key in keys.items():
                values = raw.get(key) if key else None
                value = number(values[index]) if isinstance(values, list) and index < len(values) else None
                if value is not None:
                    row["values"][metric] = value
    if not periods:
        return None
    latest = periods[max(periods)]
    annuals = [row for row in periods.values() if row["is_annual"] and row["values"].get("net_profit") is not None]
    latest["values"]["annual_net_profit"] = max(annuals, key=lambda row: row["report_end"])["values"]["net_profit"] if annuals else None
    if not latest["values"]:
        return None
    return latest


def risk_tags(values: dict) -> list[str]:
    tags = []
    rules = [
        (values.get("net_profit"), lambda value: value < 0, "最新期亏损"),
        (values.get("annual_net_profit"), lambda value: value < 0, "年度亏损"),
        (values.get("debt_ratio"), lambda value: value >= THRESHOLDS["high_debt_pct"], "高负债"),
        (values.get("operating_cashflow"), lambda value: value < 0, "经营现金流为负"),
        (values.get("revenue_growth"), lambda value: value <= THRESHOLDS["revenue_decline_pct"], "营收明显下滑"),
        (values.get("profit_growth"), lambda value: value <= THRESHOLDS["profit_decline_pct"], "利润明显下滑"),
        (values.get("gross_margin"), lambda value: value <= 0, "毛利率为负"),
        (values.get("gross_margin"), lambda value: 0 < value < THRESHOLDS["low_gross_margin_pct"], "毛利率偏低"),
    ]
    for value, predicate, label in rules:
        if value is not None and predicate(value) and label not in tags:
            tags.append(label)
    return tags


def read_json(path: Path, fallback: dict) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def cached_snapshot(payload: dict, as_of: str) -> dict | None:
    eligible = [item for item in payload.get("snapshots", []) if item.get("available_from", "9999") <= as_of]
    return max(eligible, key=lambda item: item.get("report_end", "")) if eligible else None


def save_attempt(code: str, name: str, as_of: str, status: str, reason: str = "", snapshot: dict | None = None):
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{code}.json"
    payload = read_json(path, {"code": code, "name": name, "snapshots": []})
    payload.update({"code": code, "name": name, "strategy_version": CONFIG["strategy_version"]})
    payload["last_attempt"] = {"as_of_date": as_of, "status": status, "reason": reason, "queried_at": datetime.now().isoformat(timespec="seconds")}
    if snapshot:
        item = {**snapshot, "as_of_date": as_of, "risk_tags": risk_tags(snapshot["values"]), "source": "东方财富妙想"}
        payload["snapshots"] = [old for old in payload.get("snapshots", []) if old.get("report_end") != item["report_end"]]
        payload["snapshots"].append(item)
        payload["snapshots"].sort(key=lambda old: old.get("report_end", ""))
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def pool_statuses(date_value: str) -> dict[str, str]:
    path = POOLS / f"pool_{date_yy(date_value)}.csv"
    if not path.exists():
        return {}
    result = {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            match = re.search(r"\d{6}", str(row.get("股票代码") or ""))
            if match:
                result[match.group(0)] = str(row.get("fundamental_status") or "")
    return result


def signal_candidates(date_value: str) -> dict[str, str]:
    stamp = date_yy(date_value)
    quant = read_json(RUNS / f"quant_{stamp}.json", {})
    plans = read_json(PLANS / f"signal_plan_{stamp}.json", {})
    result = {}
    for item in quant.get("results", []):
        if item.get("setup_signal") not in (None, "", "NONE"):
            result[str(item.get("code", "")).zfill(6)] = str(item.get("name") or "")
    for item in plans.get("plans", []):
        result[str(item.get("code", "")).zfill(6)] = str(item.get("name") or "")
    return result


def query_text(code: str, name: str, as_of: str) -> str:
    count = int(QUERY_CONFIG["recent_periods"])
    return (
        f"{name} {code} 截至{as_of}已经披露的最近{count}个报告期，"
        "资产负债率、净资产收益率ROE(加权)、营业收入同比增长率、"
        "归属母公司股东的净利润同比增长率、归属于母公司股东的净利润、"
        "经营活动产生的现金流量净额、销售毛利率"
    )


def request_financial(code: str, name: str, as_of: str) -> tuple[dict | None, str]:
    api_key = os.environ.get("MX_APIKEY", "").strip()
    if not api_key:
        return None, "MX_APIKEY 未设置"
    headers = {"Content-Type": "application/json", "apikey": api_key}
    body = {"toolQuery": query_text(code, name, as_of), "toolType": "query_tool"}
    last_error = "未知错误"
    for attempt in range(int(QUERY_CONFIG["max_retries"])):
        try:
            response = requests.post(MX_URL, headers=headers, json=body, timeout=float(QUERY_CONFIG["timeout_seconds"]))
            response.raise_for_status()
            payload = response.json()
            api_code = payload.get("code", payload.get("status", -1))
            if api_code == 0:
                return payload, ""
            last_error = f"API {api_code}: {payload.get('message') or '请求失败'}"
            if api_code in (113, 114, 115):
                break
        except (requests.RequestException, ValueError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt + 1 < int(QUERY_CONFIG["max_retries"]):
            time.sleep(float(QUERY_CONFIG["retry_delay_seconds"]) * (attempt + 1))
    return None, last_error


def run(date_value: str) -> dict:
    as_of = normalize_date(date_value)
    candidates = signal_candidates(as_of)
    statuses = pool_statuses(as_of)
    summary = {"date": as_of, "candidates": len(candidates), "queried": 0, "cache_hits": 0, "skipped_core": 0, "success": 0, "failed": 0, "details": []}
    expected = expected_report_end(as_of)
    for code, name in sorted(candidates.items()):
        if statuses.get(code) == "CORE_VERIFIED":
            summary["skipped_core"] += 1
            continue
        path = CACHE_DIR / f"{code}.json"
        cache = read_json(path, {})
        existing = cached_snapshot(cache, as_of)
        if existing and existing.get("report_end", "") >= expected:
            summary["cache_hits"] += 1
            summary["details"].append({"code": code, "status": "cache", "report": existing.get("label")})
            continue
        summary["queried"] += 1
        payload, error = request_financial(code, name, as_of)
        if payload is None:
            save_attempt(code, name, as_of, "failed", error)
            summary["failed"] += 1
            summary["details"].append({"code": code, "status": "failed", "reason": error})
            continue
        snapshot = extract_snapshot(payload, code, as_of)
        if snapshot is None:
            save_attempt(code, name, as_of, "insufficient", "未找到点时可用的报告期财务表")
            summary["failed"] += 1
            summary["details"].append({"code": code, "status": "insufficient"})
            continue
        save_attempt(code, name, as_of, "success", snapshot=snapshot)
        summary["success"] += 1
        summary["details"].append({"code": code, "status": "success", "report": snapshot["label"], "risk_tags": risk_tags(snapshot["values"])})
        time.sleep(float(QUERY_CONFIG["success_delay_seconds"]))
    return summary


def main():
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", required=True)
    args = parser.parse_args()
    summary = run(args.date)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
