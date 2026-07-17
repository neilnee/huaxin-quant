#!/usr/bin/env python3
"""
Position bookkeeping layer.

The trade ledger and position plan are the factual inputs. Current lots and
daily position states are derived outputs that can be rebuilt at any time.
This script intentionally does not generate trading advice; strategy and
market monitoring are a later layer.
"""

import argparse
import csv
import os
import shutil
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT
from scripts.data.market_data_service import MarketDataService
from scripts.strategy_config import load_strategy_config


POSITION_STRATEGY_FILE = "04-position.json"
CONFIG, STRATEGY_PATH = load_strategy_config(POSITION_STRATEGY_FILE)
MARKET_DATA_CONFIG, _ = load_strategy_config("market-regime.json")
STRATEGY_VERSION = CONFIG["strategy_version"]

ROOT = Path(PROJECT_ROOT)
POSITION_ROOT = ROOT / CONFIG["root_dir"]
TRADES_DIR = POSITION_ROOT / CONFIG["dirs"]["trades"]
STATES_DIR = POSITION_ROOT / CONFIG["dirs"]["states"]
IMPORTS_DIR = POSITION_ROOT / CONFIG["dirs"]["imports"]
PERFORMANCE_DIR = POSITION_ROOT / CONFIG["dirs"].get("performance", "performance")
PLAN_PATH = POSITION_ROOT / CONFIG["files"]["plan"]
LOTS_PATH = POSITION_ROOT / CONFIG["files"]["lots_current"]

PLAN_FIELDS = CONFIG["schemas"]["position_plan"]
TRADE_FIELDS = CONFIG["schemas"]["trade_ledger"]
LOT_FIELDS = CONFIG["schemas"]["lots_current"]
STATE_FIELDS = CONFIG["schemas"]["position_state"]
PERFORMANCE_FIELDS = CONFIG["schemas"].get("trade_performance", [])


def clean_cell(value):
    text = str(value or "").strip()
    if text.startswith("="):
        text = text[1:].strip()
    if len(text) >= 2 and text[0] == text[-1] == '"':
        text = text[1:-1]
    return text.strip()


def normalize_code(value):
    text = clean_cell(value)
    if text and len(text) <= 6 and text.isdigit():
        text = text.zfill(6)
    return text


def safe_float(value, default=0.0):
    try:
        value = clean_cell(value)
        if value in (None, ""):
            return default
        value = str(value).replace(",", "").replace("%", "")
        return float(value)
    except (TypeError, ValueError):
        return default


def safe_int(value, default=0):
    try:
        value = clean_cell(value)
        if value in (None, ""):
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def fmt_num(value, digits=4):
    value = safe_float(value)
    text = f"{value:.{digits}f}".rstrip("0").rstrip(".")
    return text if text else "0"


def parse_date(value):
    return datetime.strptime(clean_cell(value), "%Y-%m-%d").date()


def today_iso():
    return date.today().isoformat()


def month_key(trade_date):
    return parse_date(trade_date).strftime("%Y-%m")


def trade_ledger_path(trade_date):
    return TRADES_DIR / f"trade_ledger_{month_key(trade_date)}.csv"


def state_path(as_of):
    return STATES_DIR / f"position_state_{as_of}.csv"


def ensure_dirs():
    POSITION_ROOT.mkdir(parents=True, exist_ok=True)
    TRADES_DIR.mkdir(parents=True, exist_ok=True)
    STATES_DIR.mkdir(parents=True, exist_ok=True)
    IMPORTS_DIR.mkdir(parents=True, exist_ok=True)
    PERFORMANCE_DIR.mkdir(parents=True, exist_ok=True)


def ensure_csv(path, fieldnames):
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
    return True


def read_csv_rows(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: clean_cell(v) for k, v in row.items()} for row in csv.DictReader(f)]


def write_csv(path, fieldnames, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})


def append_rows(path, fieldnames, rows):
    created = ensure_csv(path, fieldnames)
    existing_ids = {row.get("trade_id") for row in read_csv_rows(path)}
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        if created:
            f.seek(0, os.SEEK_END)
        for row in rows:
            if row["trade_id"] in existing_ids:
                raise ValueError(f"duplicate trade_id in {path}: {row['trade_id']}")
            writer.writerow({field: row.get(field, "") for field in fieldnames})
            existing_ids.add(row["trade_id"])


def normalize_asset_type(value):
    value = clean_cell(value).upper()
    if value in {"ETF", "FUND"}:
        return "ETF"
    if value in {"个股", "STOCK", "A股"}:
        return "STOCK"
    return value or "STOCK"


def infer_asset_type(code, name=""):
    code = normalize_code(code)
    name = clean_cell(name).upper()
    if "ETF" in name or code.startswith(("15", "16", "51", "58")):
        return "ETF"
    return "STOCK"


def normalize_side(value):
    value = clean_cell(value).upper()
    if value in {"BUY", "B", "买入", "证券买入", "OPENING"}:
        return "OPENING" if value == "OPENING" else "BUY"
    if value in {"SELL", "S", "卖出", "证券卖出"}:
        return "SELL"
    if value in {"SPLIT", "ADJUST", "份额调整", "拆分"}:
        return "SPLIT"
    if value in {"DIVIDEND", "CASH_DIVIDEND", "分红", "红利"}:
        return "DIVIDEND"
    raise ValueError(f"unsupported trade side: {value}")


def pick(row, *keys):
    for key in keys:
        value = row.get(key)
        if clean_cell(value) != "":
            return clean_cell(value)
    return ""


def normalize_trade(row, recorded_at=None):
    trade_date = pick(row, "trade_date", "交易日期", "成交日期", "date")
    if not trade_date:
        raise ValueError("trade_date is required")
    parse_date(trade_date)
    trade_time = pick(row, "trade_time", "成交时间", "time") or "00:00:00"
    code = normalize_code(pick(row, "code", "股票代码", "证券代码"))
    if not code:
        raise ValueError("code is required")
    name = pick(row, "name", "股票名称", "证券名称")
    shares = safe_int(pick(row, "shares", "数量", "成交数量"))
    price = safe_float(pick(row, "price", "成交价格", "成交均价"))
    amount = safe_float(pick(row, "amount", "成交金额"), shares * price)
    side = normalize_side(pick(row, "side", "方向", "委托方向"))
    broker_trade_id = pick(row, "成交编号", "委托编号")
    trade_id = pick(row, "trade_id") or broker_trade_id or build_trade_id(trade_date, trade_time, code, side, shares, price)
    fee = (
        safe_float(pick(row, "fee", "手续费", "佣金"))
        + safe_float(pick(row, "交易规费"))
        + safe_float(pick(row, "过户费"))
    )
    tax = safe_float(pick(row, "tax", "印花税"))
    asset_type = pick(row, "asset_type", "资产类型") or infer_asset_type(code, name)
    return {
        "trade_id": trade_id,
        "trade_date": trade_date,
        "trade_time": trade_time,
        "recorded_at": row.get("recorded_at") or recorded_at or today_iso(),
        "code": code,
        "name": name,
        "asset_type": normalize_asset_type(asset_type),
        "side": side,
        "shares": str(shares),
        "price": fmt_num(price),
        "amount": fmt_num(amount),
        "fee": fmt_num(fee),
        "tax": fmt_num(tax),
        "reason": pick(row, "reason", "原因") or "",
        "signal_ref": pick(row, "signal_ref") or "",
        "notes": pick(row, "notes", "备注") or "",
    }


def build_trade_id(trade_date, trade_time, code, side, shares, price):
    cents = int(round(float(price) * 10000))
    clock = clean_cell(trade_time).replace(":", "")
    return f"{trade_date.replace('-', '')}-{clock}-{code}-{side}-{shares}-{cents}"


def all_trade_paths():
    if not TRADES_DIR.exists():
        return []
    return sorted(TRADES_DIR.glob("trade_ledger_*.csv"))


def load_trades(as_of=None):
    cutoff = parse_date(as_of) if as_of else None
    rows = []
    for path in all_trade_paths():
        for row in read_csv_rows(path):
            trade_date = parse_date(row["trade_date"])
            if cutoff and trade_date > cutoff:
                continue
            rows.append(normalize_trade(row, recorded_at=row.get("recorded_at")))
    rows.sort(key=lambda r: (r["trade_date"], r.get("trade_time", "00:00:00"), r["trade_id"]))
    return rows


def load_plan():
    plan = {}
    for row in read_csv_rows(PLAN_PATH):
        code = normalize_code(row.get("code"))
        if code:
            plan[code] = row
    return plan


def lot_id(trade):
    return f"LOT-{trade['trade_id']}"


def replay_trades(trades):
    lots = []
    realized = defaultdict(float)
    last_trade_date = {}
    names = {}
    asset_types = {}

    for trade in trades:
        code = trade["code"]
        side = trade["side"]
        shares = safe_int(trade["shares"])
        price = safe_float(trade["price"])
        fee = safe_float(trade["fee"])
        tax = safe_float(trade["tax"])
        names[code] = trade.get("name") or names.get(code, "")
        asset_types[code] = trade.get("asset_type") or asset_types.get(code, "STOCK")
        last_trade_date[code] = trade["trade_date"]

        if side == "SPLIT":
            ratio = price
            if ratio <= 0:
                raise ValueError(f"invalid split ratio for {code}: {ratio}")
            for lot in lots:
                if lot["code"] != code or lot["shares_remaining"] <= 0:
                    continue
                lot["shares_remaining"] = int(round(lot["shares_remaining"] * ratio))
                lot["cost_price"] = lot["cost_amount"] / lot["shares_remaining"] if lot["shares_remaining"] else 0
                lot["price_ref_date"] = trade.get("signal_ref", "")
                lot["price_ref_source"] = trade.get("notes", "")
            continue

        if side == "DIVIDEND":
            realized[code] += safe_float(trade.get("amount")) - fee - tax
            continue

        if side in {"BUY", "OPENING"}:
            amount = safe_float(trade.get("amount"), shares * price) + fee + tax
            lots.append({
                "lot_id": lot_id(trade),
                "code": code,
                "name": names[code],
                "asset_type": asset_types[code],
                "open_date": trade["trade_date"],
                "source_trade_id": trade["trade_id"],
                "open_reason": trade.get("reason", ""),
                "shares_remaining": shares,
                "cost_price": amount / shares if shares else 0,
                "cost_amount": amount,
                "realized_shares": 0,
                "cost_basis_type": "YEAR_OPENING" if side == "OPENING" else "TRADE_BUY",
                "price_ref_date": trade.get("signal_ref", ""),
                "price_ref_source": trade.get("notes", ""),
                "status": "OPEN",
            })
            continue

        if side == "SELL":
            remaining = shares
            proceeds_per_share = (shares * price - fee - tax) / shares if shares else price
            for lot in lots:
                if remaining <= 0:
                    break
                if lot["code"] != code or lot["shares_remaining"] <= 0:
                    continue
                matched = min(remaining, lot["shares_remaining"])
                realized[code] += matched * (proceeds_per_share - lot["cost_price"])
                lot["shares_remaining"] -= matched
                lot["cost_amount"] = lot["shares_remaining"] * lot["cost_price"]
                lot["realized_shares"] += matched
                if lot["shares_remaining"] <= 0:
                    lot["status"] = "CLOSED"
                remaining -= matched
            if remaining > 0 and not CONFIG["rebuild"]["allow_short_position"]:
                raise ValueError(f"sell exceeds open lots for {code}: remaining {remaining}")

    open_lots = [lot for lot in lots if lot["shares_remaining"] > 0]
    return open_lots, realized, last_trade_date, names, asset_types


def build_state(as_of, lots, realized, last_trade_date, names, asset_types, plan, account_refs=None):
    account_refs = account_refs or {}
    by_code = defaultdict(list)
    for lot in lots:
        by_code[lot["code"]].append(lot)

    codes = sorted(set(by_code) | set(realized) | set(plan))
    rows = []
    for code in codes:
        plan_row = plan.get(code, {})
        code_lots = by_code.get(code, [])
        shares = sum(safe_int(lot["shares_remaining"]) for lot in code_lots)
        cost_amount = sum(safe_float(lot["cost_amount"]) for lot in code_lots)
        if shares <= 0 and not CONFIG["rebuild"]["write_empty_state"]:
            continue
        name = plan_row.get("name") or names.get(code, "")
        asset_type = plan_row.get("asset_type") or asset_types.get(code, "")
        rows.append({
            "as_of": as_of,
            "code": code,
            "name": name,
            "asset_type": normalize_asset_type(asset_type),
            "strategy_tag": plan_row.get("strategy_tag", ""),
            "target_type": plan_row.get("target_type", ""),
            "target_value": plan_row.get("target_value", ""),
            "actual_shares": str(shares),
            "cost_price": fmt_num(cost_amount / shares if shares else 0),
            "cost_amount": fmt_num(cost_amount),
            "realized_pnl": fmt_num(realized.get(code, 0)),
            "last_trade_date": last_trade_date.get(code, ""),
            "position_status": "OPEN" if shares > 0 else "CLOSED",
            "manual_tag": plan_row.get("manual_tag", ""),
            "account_cost_price": account_refs.get(code, {}).get("cost_price", ""),
            "account_cost_amount": account_refs.get(code, {}).get("cost_amount", ""),
            "latest_price": account_refs.get(code, {}).get("latest_price", ""),
            "account_unrealized_pnl": account_refs.get(code, {}).get("unrealized_pnl", ""),
        })
    return rows


def cmd_init(_args):
    ensure_dirs()
    created = []
    if ensure_csv(PLAN_PATH, PLAN_FIELDS):
        created.append(str(PLAN_PATH))
    if ensure_csv(LOTS_PATH, LOT_FIELDS):
        created.append(str(LOTS_PATH))
    print(f"position root: {POSITION_ROOT}")
    print(f"created files: {len(created)}")
    for path in created:
        print(f"  + {path}")


def legacy_asset_type(value):
    return "ETF" if str(value).upper() == "ETF" else "STOCK"


def cmd_bootstrap(args):
    ensure_dirs()
    source = Path(args.source)
    rows = read_csv_rows(source)
    if not rows:
        raise SystemExit(f"no rows found: {source}")

    plan_rows = []
    trades = []
    for idx, row in enumerate(rows, 1):
        code = normalize_code(row.get("code") or row.get("股票代码"))
        shares = safe_int(row.get("actual_shares"))
        if not code or shares <= 0:
            continue
        asset_type = legacy_asset_type(row.get("asset_type"))
        name = row.get("name") or row.get("股票名称") or ""
        target_type = row.get("target_type") or ("amount" if asset_type == "ETF" else "shares")
        target_value = row.get("target_value") or ""
        cost_price = safe_float(row.get("cost_price"))
        trade_id = f"{CONFIG['bootstrap']['default_trade_id_prefix']}-{args.as_of.replace('-', '')}-{idx:04d}-{code}"
        trades.append(normalize_trade({
            "trade_id": trade_id,
            "trade_date": args.as_of,
            "recorded_at": today_iso(),
            "code": code,
            "name": name,
            "asset_type": asset_type,
            "side": "OPENING",
            "shares": shares,
            "price": cost_price,
            "amount": shares * cost_price,
            "reason": CONFIG["bootstrap"]["default_reason"],
            "notes": f"bootstrap from {source}",
        }))
        plan_rows.append({
            "code": code,
            "name": name,
            "asset_type": asset_type,
            "target_type": target_type,
            "target_value": target_value,
            "strategy_tag": row.get("strategy_tag", ""),
            "max_position_pct": "1.0",
            "status": "ACTIVE",
            "manual_tag": row.get("manual_tag", ""),
            "notes": "bootstrap",
        })

    if not PLAN_PATH.exists() or args.overwrite_plan:
        write_csv(PLAN_PATH, PLAN_FIELDS, plan_rows)
    else:
        existing = load_plan()
        merged = list(existing.values())
        seen = set(existing)
        for row in plan_rows:
            if row["code"] not in seen:
                merged.append(row)
        write_csv(PLAN_PATH, PLAN_FIELDS, merged)

    by_month = defaultdict(list)
    for trade in trades:
        by_month[month_key(trade["trade_date"])].append(trade)
    for month, month_rows in by_month.items():
        append_rows(TRADES_DIR / f"trade_ledger_{month}.csv", TRADE_FIELDS, month_rows)

    print(f"bootstrapped positions: {len(trades)}")
    print(f"plan: {PLAN_PATH}")
    print(f"trade months: {', '.join(sorted(by_month))}")


def cmd_add_trade(args):
    ensure_dirs()
    trade = normalize_trade({
        "trade_date": args.trade_date,
        "trade_time": args.trade_time,
        "recorded_at": today_iso(),
        "code": args.code,
        "name": args.name,
        "asset_type": args.asset_type,
        "side": args.side,
        "shares": args.shares,
        "price": args.price,
        "amount": args.amount,
        "fee": args.fee,
        "tax": args.tax,
        "reason": args.reason,
        "signal_ref": args.signal_ref,
        "notes": args.notes,
    })
    append_rows(trade_ledger_path(trade["trade_date"]), TRADE_FIELDS, [trade])
    print(f"added trade: {trade['trade_id']}")
    print(f"ledger: {trade_ledger_path(trade['trade_date'])}")


def cmd_import(args):
    ensure_dirs()
    source = Path(args.file)
    rows, skipped = normalize_trade_rows(read_csv_rows(source))
    by_month = defaultdict(list)
    for row in rows:
        by_month[month_key(row["trade_date"])].append(row)
    for month, month_rows in by_month.items():
        append_rows(TRADES_DIR / f"trade_ledger_{month}.csv", TRADE_FIELDS, month_rows)
    print(f"imported trades: {len(rows)}")
    print(f"skipped rows: {skipped}")
    print(f"months: {', '.join(sorted(by_month))}")


def normalize_trade_rows(rows, recorded_at=None):
    trades = []
    skipped = 0
    for row in rows:
        try:
            trades.append(normalize_trade(row, recorded_at=recorded_at or today_iso()))
        except ValueError as exc:
            if "unsupported trade side" in str(exc):
                skipped += 1
                continue
            raise
    return trades, skipped


def load_holdings(path):
    holdings = {}
    for row in read_csv_rows(Path(path)):
        code = normalize_code(pick(row, "code", "证券代码", "股票代码"))
        if not code:
            continue
        name = pick(row, "name", "证券名称", "股票名称")
        shares = safe_int(pick(row, "shares", "持仓数量", "actual_shares"))
        cost_price = safe_float(pick(row, "cost_price", "成本价"))
        latest_price = safe_float(pick(row, "latest_price", "最新价"))
        unrealized_pnl = safe_float(pick(row, "unrealized_pnl", "持仓盈亏"))
        holdings[code] = {
            "code": code,
            "name": name,
            "asset_type": normalize_asset_type(pick(row, "asset_type", "资产类型") or infer_asset_type(code, name)),
            "shares": shares,
            "cost_price": fmt_num(cost_price),
            "cost_amount": fmt_num(shares * cost_price),
            "latest_price": fmt_num(latest_price),
            "unrealized_pnl": fmt_num(unrealized_pnl),
        }
    return holdings


def first_trade_price(code, trades):
    code_trades = [t for t in trades if t["code"] == code and safe_float(t["price"]) > 0]
    if not code_trades:
        return 0, "", ""
    code_trades.sort(key=lambda r: (r["trade_date"], r.get("trade_time", "00:00:00"), r["trade_id"]))
    first = code_trades[0]
    return safe_float(first["price"]), first["trade_date"], "first_trade_fallback"


def infer_share_adjustments(holdings, raw_trade_rows):
    latest_balance = {}
    for row in raw_trade_rows:
        direction = pick(row, "委托方向", "side", "方向")
        if direction not in {"证券买入", "证券卖出", "BUY", "SELL", "买入", "卖出"}:
            continue
        code = normalize_code(pick(row, "证券代码", "股票代码", "code"))
        if code not in holdings:
            continue
        trade_date = pick(row, "成交日期", "交易日期", "trade_date")
        trade_time = pick(row, "成交时间", "trade_time") or "00:00:00"
        balance = safe_int(pick(row, "股份余额", "balance_after"))
        key = (trade_date, trade_time)
        if balance <= 0:
            continue
        if code not in latest_balance or key > latest_balance[code]["key"]:
            latest_balance[code] = {"key": key, "balance": balance}

    adjustments = {}
    for code, info in latest_balance.items():
        current = safe_int(holdings.get(code, {}).get("shares"))
        balance = info["balance"]
        if current <= 0 or balance <= 0 or current == balance:
            continue
        ratio = current / balance
        rounded = round(ratio, 6)
        common_ratio = round(ratio)
        if common_ratio > 1 and abs(ratio - common_ratio) < 0.0001:
            rounded = float(common_ratio)
        if rounded > 0:
            adjustments[code] = {
                "ratio": rounded,
                "last_trade_date": info["key"][0],
                "last_trade_time": info["key"][1],
                "balance_before": balance,
                "current_shares": current,
            }
    return adjustments


def year_start_price(code, name, year_start, as_of, fallback_price=0, trades=None):
    frames, status = MarketDataService(MARKET_DATA_CONFIG).get_daily_bars([(code, name)], as_of, 200)
    df = frames.get(code)
    source = status.get(code, {}).get("source", "market_db")
    if df is not None:
        start = parse_date(year_start)
        pre_year = df[df["date"].astype(str) < start.isoformat()]
        if not pre_year.empty:
            row = pre_year.iloc[-1]
            return safe_float(row["close"]), str(row["date"]), source
        post_year = df[df["date"].astype(str) >= start.isoformat()]
        if not post_year.empty:
            row = post_year.iloc[0]
            return safe_float(row["close"]), str(row["date"]), source
    if fallback_price:
        return fallback_price, year_start, "account_cost_fallback"
    price, ref_date, ref_source = first_trade_price(code, trades or [])
    return price, ref_date or year_start, ref_source or f"missing_quote:{source}"


def required_openings(holdings, trades, adjustments=None):
    adjustments = adjustments or {}
    by_code = defaultdict(list)
    for trade in trades:
        if trade["side"] == "SPLIT":
            continue
        by_code[trade["code"]].append(trade)
    codes = sorted(set(holdings) | set(by_code))
    openings = {}
    errors = []
    for code in codes:
        ordered = sorted(by_code.get(code, []), key=lambda r: (r["trade_date"], r.get("trade_time", "00:00:00"), r["trade_id"]))
        net = 0
        min_running = 0
        for trade in ordered:
            if trade["side"] not in {"BUY", "SELL"}:
                continue
            qty = safe_int(trade["shares"])
            net += qty if trade["side"] == "BUY" else -qty
            min_running = min(min_running, net)
        current = safe_int(holdings.get(code, {}).get("shares"))
        if code in adjustments:
            current = int(round(current / safe_float(adjustments[code]["ratio"], 1)))
        opening = current - net
        if opening < 0:
            errors.append(f"{code}: current shares {current} below year net buy {net}")
            continue
        if opening < -min_running:
            errors.append(f"{code}: opening shares {opening} cannot cover early sells {-min_running}")
            continue
        if opening > 0:
            openings[code] = opening
    return openings, errors


def write_ledgers(trades, overwrite=False):
    by_month = defaultdict(list)
    for trade in trades:
        by_month[month_key(trade["trade_date"])].append(trade)
    for month, month_rows in by_month.items():
        path = TRADES_DIR / f"trade_ledger_{month}.csv"
        month_rows.sort(key=lambda r: (r["trade_date"], r.get("trade_time", "00:00:00"), r["trade_id"]))
        if overwrite:
            write_csv(path, TRADE_FIELDS, month_rows)
        else:
            append_rows(path, TRADE_FIELDS, month_rows)
    return by_month


def build_plan_from_holdings(holdings):
    rows = []
    for code in sorted(holdings):
        row = holdings[code]
        rows.append({
            "code": code,
            "name": row["name"],
            "asset_type": row["asset_type"],
            "target_type": "shares",
            "target_value": "",
            "strategy_tag": "",
            "max_position_pct": "1.0",
            "status": "ACTIVE",
            "manual_tag": "",
            "notes": "current holding snapshot import",
        })
    return rows


def performance_path(year):
    return PERFORMANCE_DIR / f"trade_performance_{year}.csv"


def build_performance_rows(year, as_of, holdings, trades, lots, realized, openings):
    by_code = defaultdict(list)
    for trade in trades:
        by_code[trade["code"]].append(trade)
    open_lots = defaultdict(list)
    for lot in lots:
        open_lots[lot["code"]].append(lot)
    codes = sorted(set(by_code) | set(holdings) | set(realized) | set(openings))
    rows = []
    for code in codes:
        trades_for_code = by_code.get(code, [])
        name = holdings.get(code, {}).get("name") or next((t["name"] for t in trades_for_code if t.get("name")), "")
        asset_type = holdings.get(code, {}).get("asset_type") or next((t["asset_type"] for t in trades_for_code if t.get("asset_type")), "")
        buy_shares = sum(safe_int(t["shares"]) for t in trades_for_code if t["side"] == "BUY")
        sell_shares = sum(safe_int(t["shares"]) for t in trades_for_code if t["side"] == "SELL")
        buy_amount = sum(safe_float(t["amount"]) + safe_float(t["fee"]) + safe_float(t["tax"]) for t in trades_for_code if t["side"] == "BUY")
        sell_amount = sum(safe_float(t["amount"]) - safe_float(t["fee"]) - safe_float(t["tax"]) for t in trades_for_code if t["side"] == "SELL")
        shares = safe_int(holdings.get(code, {}).get("shares"))
        latest_price = safe_float(holdings.get(code, {}).get("latest_price"))
        ending_cost = sum(safe_float(lot["cost_amount"]) for lot in open_lots.get(code, []))
        market_value = shares * latest_price
        unrealized = market_value - ending_cost if shares > 0 and latest_price else 0
        realized_pnl = realized.get(code, 0)
        opening_lot = next((t for t in trades_for_code if t["side"] == "OPENING"), {})
        rows.append({
            "year": str(year),
            "as_of": as_of,
            "code": code,
            "name": name,
            "asset_type": normalize_asset_type(asset_type),
            "opening_shares": str(openings.get(code, 0)),
            "opening_price": opening_lot.get("price", ""),
            "opening_ref": opening_lot.get("signal_ref", ""),
            "buy_shares": str(buy_shares),
            "sell_shares": str(sell_shares),
            "buy_amount": fmt_num(buy_amount),
            "sell_amount": fmt_num(sell_amount),
            "current_shares": str(shares),
            "latest_price": fmt_num(latest_price),
            "ending_cost_amount": fmt_num(ending_cost),
            "market_value": fmt_num(market_value),
            "realized_pnl": fmt_num(realized_pnl),
            "year_unrealized_pnl": fmt_num(unrealized),
            "year_total_pnl": fmt_num(realized_pnl + unrealized),
            "account_cost_price": holdings.get(code, {}).get("cost_price", ""),
            "account_unrealized_pnl": holdings.get(code, {}).get("unrealized_pnl", ""),
            "trade_count": str(len([t for t in trades_for_code if t["side"] not in {"OPENING", "SPLIT"}])),
            "notes": opening_lot.get("notes", ""),
        })
    return rows


def cmd_reconstruct_year(args):
    ensure_dirs()
    year = parse_date(args.year_start).year
    holdings = load_holdings(args.holdings)
    raw_trade_rows = read_csv_rows(Path(args.trades))
    imported, skipped = normalize_trade_rows(raw_trade_rows, recorded_at=today_iso())
    trades = [t for t in imported if parse_date(args.year_start) <= parse_date(t["trade_date"]) <= parse_date(args.as_of)]
    cash_event_path = Path(args.cash_events) if args.cash_events else IMPORTS_DIR / f"cash_events_{year}.csv"
    cash_events = []
    if cash_event_path.exists():
        imported_cash, _cash_skipped = normalize_trade_rows(read_csv_rows(cash_event_path), recorded_at=today_iso())
        cash_events = [
            t for t in imported_cash
            if parse_date(args.year_start) <= parse_date(t["trade_date"]) <= parse_date(args.as_of)
        ]
    adjustments = infer_share_adjustments(holdings, raw_trade_rows)
    openings, errors = required_openings(holdings, trades, adjustments=adjustments)
    if errors:
        raise SystemExit("cannot reconstruct opening lots:\n  " + "\n  ".join(errors))

    opening_trades = []
    for idx, code in enumerate(sorted(openings), 1):
        h = holdings.get(code, {})
        name = h.get("name") or next((t["name"] for t in trades if t["code"] == code and t.get("name")), "")
        asset_type = h.get("asset_type") or infer_asset_type(code, name)
        fallback = safe_float(h.get("cost_price"))
        price, ref_date, ref_source = year_start_price(code, name, args.year_start, args.as_of, fallback_price=fallback, trades=trades)
        opening_trades.append(normalize_trade({
            "trade_id": f"{CONFIG['bootstrap']['default_trade_id_prefix']}-{year}-{idx:04d}-{code}",
            "trade_date": args.year_start,
            "trade_time": "00:00:00",
            "recorded_at": today_iso(),
            "code": code,
            "name": name,
            "asset_type": asset_type,
            "side": "OPENING",
            "shares": openings[code],
            "price": price,
            "amount": openings[code] * price,
            "reason": "year_opening_reconstruct",
            "signal_ref": f"{ref_date}:{ref_source}",
            "notes": f"opening inferred from {Path(args.holdings).name} and {Path(args.trades).name}",
        }))

    adjustment_trades = []
    for idx, code in enumerate(sorted(adjustments), 1):
        adj = adjustments[code]
        h = holdings.get(code, {})
        name = h.get("name") or next((t["name"] for t in trades if t["code"] == code and t.get("name")), "")
        asset_type = h.get("asset_type") or infer_asset_type(code, name)
        adjustment_trades.append(normalize_trade({
            "trade_id": f"ADJ-{year}-{idx:04d}-{code}",
            "trade_date": args.as_of,
            "trade_time": "00:00:00",
            "recorded_at": today_iso(),
            "code": code,
            "name": name,
            "asset_type": asset_type,
            "side": "SPLIT",
            "shares": 0,
            "price": adj["ratio"],
            "amount": 0,
            "fee": 0,
            "tax": 0,
            "reason": "share_adjustment_inferred",
            "signal_ref": f"{adj['last_trade_date']} balance {adj['balance_before']} -> snapshot {adj['current_shares']}",
            "notes": f"inferred split ratio {adj['ratio']} from broker balance_after and current snapshot",
        }))

    all_trades = opening_trades + trades + adjustment_trades + cash_events
    write_ledgers(all_trades, overwrite=args.overwrite_ledgers)
    write_csv(PLAN_PATH, PLAN_FIELDS, build_plan_from_holdings(holdings))

    account_refs = {code: {
        "cost_price": row.get("cost_price", ""),
        "cost_amount": row.get("cost_amount", ""),
        "latest_price": row.get("latest_price", ""),
        "unrealized_pnl": row.get("unrealized_pnl", ""),
    } for code, row in holdings.items()}
    lots, realized, last_trade_date, names, asset_types = replay_trades(load_trades(args.as_of))
    state_rows = build_state(args.as_of, lots, realized, last_trade_date, names, asset_types, load_plan(), account_refs=account_refs)
    lot_rows = []
    for lot in lots:
        lot_rows.append({
            "lot_id": lot["lot_id"],
            "code": lot["code"],
            "name": lot["name"],
            "asset_type": lot["asset_type"],
            "open_date": lot["open_date"],
            "source_trade_id": lot["source_trade_id"],
            "open_reason": lot["open_reason"],
            "shares_remaining": str(lot["shares_remaining"]),
            "cost_price": fmt_num(lot["cost_price"]),
            "cost_amount": fmt_num(lot["cost_amount"]),
            "realized_shares": str(lot["realized_shares"]),
            "cost_basis_type": lot.get("cost_basis_type", ""),
            "price_ref_date": lot.get("price_ref_date", ""),
            "price_ref_source": lot.get("price_ref_source", ""),
            "status": lot["status"],
        })
    write_csv(LOTS_PATH, LOT_FIELDS, lot_rows)
    out_state = state_path(args.as_of)
    write_csv(out_state, STATE_FIELDS, state_rows)
    perf_rows = build_performance_rows(year, args.as_of, holdings, load_trades(args.as_of), lots, realized, openings)
    out_perf = performance_path(year)
    write_csv(out_perf, PERFORMANCE_FIELDS, perf_rows)

    for src in (Path(args.holdings), Path(args.trades), cash_event_path):
        if src.exists():
            dst = IMPORTS_DIR / src.name
            if src.resolve() != dst.resolve():
                shutil.copy2(src, dst)

    print(f"holdings imported: {len(holdings)}")
    print(f"broker trades imported: {len(trades)}")
    print(f"broker rows skipped: {skipped}")
    print(f"opening trades inferred: {len(opening_trades)}")
    print(f"share adjustments inferred: {len(adjustment_trades)}")
    print(f"cash events imported: {len(cash_events)}")
    print(f"open lots: {len(lot_rows)} -> {LOTS_PATH}")
    print(f"state rows: {len(state_rows)} -> {out_state}")
    print(f"performance rows: {len(perf_rows)} -> {out_perf}")


def cmd_rebuild(args):
    ensure_dirs()
    parse_date(args.as_of)
    trades = load_trades(args.as_of)
    plan = load_plan()
    lots, realized, last_trade_date, names, asset_types = replay_trades(trades)
    state_rows = build_state(args.as_of, lots, realized, last_trade_date, names, asset_types, plan)
    lot_rows = []
    for lot in lots:
        lot_rows.append({
            "lot_id": lot["lot_id"],
            "code": lot["code"],
            "name": lot["name"],
            "asset_type": lot["asset_type"],
            "open_date": lot["open_date"],
            "source_trade_id": lot["source_trade_id"],
            "open_reason": lot["open_reason"],
            "shares_remaining": str(lot["shares_remaining"]),
            "cost_price": fmt_num(lot["cost_price"]),
            "cost_amount": fmt_num(lot["cost_amount"]),
            "realized_shares": str(lot["realized_shares"]),
            "cost_basis_type": lot.get("cost_basis_type", ""),
            "price_ref_date": lot.get("price_ref_date", ""),
            "price_ref_source": lot.get("price_ref_source", ""),
            "status": lot["status"],
        })
    write_csv(LOTS_PATH, LOT_FIELDS, lot_rows)
    out_state = state_path(args.as_of)
    write_csv(out_state, STATE_FIELDS, state_rows)
    print(f"trades replayed: {len(trades)}")
    print(f"open lots: {len(lot_rows)} -> {LOTS_PATH}")
    print(f"state rows: {len(state_rows)} -> {out_state}")


def build_parser():
    parser = argparse.ArgumentParser(description="Huaxin position bookkeeping")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="创建 position 模块目录和模板")
    p_init.set_defaults(func=cmd_init)

    p_boot = sub.add_parser("bootstrap", help="从旧持仓快照初始化计划和 OPENING 流水")
    p_boot.add_argument("--from", dest="source", required=True, help="旧持仓 CSV，如 signals/positions.csv")
    p_boot.add_argument("--as-of", required=True, help="初始化交易日期 YYYY-MM-DD")
    p_boot.add_argument("--overwrite-plan", action="store_true", help="覆盖 position_plan.csv")
    p_boot.set_defaults(func=cmd_bootstrap)

    p_add = sub.add_parser("add-trade", help="补录一笔标准交易")
    p_add.add_argument("--trade-date", required=True)
    p_add.add_argument("--trade-time", default="00:00:00")
    p_add.add_argument("--code", required=True)
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--asset-type", default="STOCK")
    p_add.add_argument("--side", required=True, choices=["BUY", "SELL", "OPENING"])
    p_add.add_argument("--shares", required=True)
    p_add.add_argument("--price", required=True)
    p_add.add_argument("--amount", default="")
    p_add.add_argument("--fee", default="0")
    p_add.add_argument("--tax", default="0")
    p_add.add_argument("--reason", default="")
    p_add.add_argument("--signal-ref", default="")
    p_add.add_argument("--notes", default="")
    p_add.set_defaults(func=cmd_add_trade)

    p_import = sub.add_parser("import", help="导入标准交易 CSV")
    p_import.add_argument("--file", required=True)
    p_import.set_defaults(func=cmd_import)

    p_recon = sub.add_parser("reconstruct-year", help="用当前持仓和年内券商流水重建年度账本")
    p_recon.add_argument("--holdings", required=True, help="当前持仓 CSV")
    p_recon.add_argument("--trades", required=True, help="券商历史成交 CSV")
    p_recon.add_argument("--as-of", required=True, help="当前持仓日期 YYYY-MM-DD")
    p_recon.add_argument("--year-start", default="2026-01-01")
    p_recon.add_argument("--cash-events", default="", help="现金事件 CSV，如分红/红利税；默认读取 position/imports/cash_events_<year>.csv")
    p_recon.add_argument("--overwrite-ledgers", action="store_true", help="覆盖同月份流水文件")
    p_recon.set_defaults(func=cmd_reconstruct_year)

    p_rebuild = sub.add_parser("rebuild", help="按交易流水重建持仓状态")
    p_rebuild.add_argument("--as-of", required=True)
    p_rebuild.set_defaults(func=cmd_rebuild)

    return parser


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
