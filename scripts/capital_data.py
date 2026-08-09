#!/usr/bin/env python3
"""Batch CLI for capital-data mapping and quota-bounded source updates."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))

from scripts.data.capital_data_service import CapitalDataService
from scripts.data.capital_data_sources import MiaoxiangCapitalSource, RequestBudget
from scripts.shared import expected_trade_date, normalize_date_arg
from scripts.strategy_config import load_strategy_config


def csv_values(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def levels(value: str) -> list[int]:
    result = [int(item) for item in csv_values(value)]
    if not result or any(item not in {1, 2} for item in result):
        raise argparse.ArgumentTypeError("levels 只支持 1,2")
    return list(dict.fromkeys(result))


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def service_with_budget(config: dict, requested: int | None):
    mx_config = config["miaoxiang"]
    maximum = int(mx_config["hard_maximum_requests_per_run"])
    budget_value = int(requested if requested is not None else mx_config["default_maximum_requests_per_run"])
    if budget_value < 0 or budget_value > maximum:
        raise ValueError(f"max-mx-requests 必须在 0 到 {maximum} 之间")
    budget = RequestBudget(budget_value)
    source = MiaoxiangCapitalSource(request_budget=budget)
    return CapitalDataService(config=config, capital_source=source), budget


def command_mapping_build(args, config):
    service = CapitalDataService(config=config)
    results = []
    for level in args.levels:
        results.append(service.ensure_sector_mapping(args.as_of, sw_level=level, force_rebuild=args.force))
    print_json({"miaoxiang_requests": 0, "results": results})


def command_mapping_status(args, config):
    service = CapitalDataService(config=config)
    print_json({"results": [service.mapping_status(args.as_of, level) for level in args.levels]})


def command_fetch(args, config):
    service, budget = service_with_budget(config, args.max_mx_requests)
    target = normalize_date_arg(args.date) if args.date else expected_trade_date()
    sw_codes, stock_codes = csv_values(args.sw_codes), csv_values(args.stock_codes)
    estimate = service.estimate_requests(sw_codes, stock_codes, target, target)
    if args.dry_run:
        print_json({"dry_run": True, "request_budget": budget.maximum_requests, "estimate": estimate})
        return
    if estimate["missing_mappings"]:
        raise RuntimeError("缺少行业映射: " + ",".join(estimate["missing_mappings"]))
    sectors = []
    for sw_code in sw_codes:
        result = service.fetch_sector_capital(sw_code, target, target)
        sectors.append({
            "sw_code": sw_code,
            "status": result["status"],
            "row_count": len(result.get("rows", [])),
            "errors": result.get("errors", []),
        })
        if budget.used >= budget.maximum_requests:
            break
    stocks = None
    if stock_codes and budget.used < budget.maximum_requests:
        result = service.fetch_stock_capital(stock_codes, target, target)
        stocks = {
            "status": result["status"],
            "available": sum(bool(item["rows"]) for item in result["stocks"].values()),
            "requested": len(result["stocks"]),
            "errors": result.get("errors", []),
        }
    print_json({
        "date": target,
        "request_budget": budget.maximum_requests,
        "requests_used": budget.used,
        "estimate": estimate,
        "sectors": sectors,
        "stocks": stocks,
    })


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="资金数据层批处理")
    sub = root.add_subparsers(dest="command", required=True)

    build = sub.add_parser("mapping-build", help="建立或复用申万—财富通行业映射，不调用妙想")
    build.add_argument("--levels", type=levels, default=[1, 2])
    build.add_argument("--as-of")
    build.add_argument("--force", action="store_true")

    status = sub.add_parser("mapping-status", help="查看映射缓存状态")
    status.add_argument("--levels", type=levels, default=[1, 2])
    status.add_argument("--as-of")

    fetch = sub.add_parser("fetch", help="按请求预算补取板块与个股资金")
    fetch.add_argument("--date")
    fetch.add_argument("--sw-codes", default="")
    fetch.add_argument("--stock-codes", default="")
    fetch.add_argument("--max-mx-requests", type=int)
    fetch.add_argument("--dry-run", action="store_true")
    return root


def main() -> int:
    args = parser().parse_args()
    config = load_strategy_config("capital-data.json")[0]
    try:
        if args.command == "mapping-build":
            command_mapping_build(args, config)
        elif args.command == "mapping-status":
            command_mapping_status(args, config)
        else:
            command_fetch(args, config)
    except Exception as exc:
        print_json({"status": "error", "error": str(exc)})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
