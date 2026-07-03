"""
模型二：量价精筛 — VCP/P2/P3 脚本驱动实现
对应指令: instructions/02-quant.md

用法:
  python3 scripts/quant_filter.py
  python3 scripts/quant_filter.py --pool pool/pool_260703.csv
  python3 scripts/quant_filter.py --code 300604 --name 长川科技
  python3 scripts/quant_filter.py --codes 300604,300442
  python3 scripts/quant_filter.py --code 300604 --with-llm

核心原则:
  脚本负责数据、指标、形态、评分、输出；LLM 只做可选解释，不参与 P1/P2/P3 判定。
"""
import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import DailyCache, PROJECT_ROOT, RateLimiter


# ===================== 配置 =====================

BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
POOL_DIR = os.path.join(PROJECT_ROOT, "pool")
QUANT_DIR = os.path.join(PROJECT_ROOT, "quant")
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
QUANT_RUNS_DIR = os.path.join(PROJECT_ROOT, "cache", "quant_runs")
cache = DailyCache()
_rate_limiter = RateLimiter()

TEST_CODES = [
    "688256", "301329", "300394", "300308", "002028",
    "300750", "002008", "688285", "002460", "300127",
    "688235", "920946", "688295", "002054", "601777",
    "603409", "600176", "301526", "300442",
]

FIELD_ALIASES = {
    "成交量": ["成交量", "成交数量", "成交股数"],
    "换手率": ["换手率", "换手"],
    "开盘价": ["开盘价", "开盘"],
    "最高价": ["最高价", "最高"],
    "最低价": ["最低价", "最低"],
    "收盘价": ["收盘价", "收盘"],
}


# ===================== 通用工具 =====================

def safe_float(value, default=None):
    try:
        if value is None or pd.isna(value):
            return default
        return float(value)
    except Exception:
        return default


def round_or_none(value, digits=2):
    value = safe_float(value)
    if value is None:
        return None
    return round(value, digits)


def pct(numer, denom):
    if denom is None or pd.isna(denom) or denom == 0:
        return np.nan
    return numer / denom * 100


def regression_slope_pct(series, window):
    """最近 window 个点线性回归斜率，转为相对末值的每日百分比。"""
    if len(series) < window:
        return np.nan
    y = pd.Series(series).dropna().tail(window)
    if len(y) < window or y.iloc[-1] == 0:
        return np.nan
    x = np.arange(len(y), dtype=float)
    slope = np.polyfit(x, y.astype(float), 1)[0]
    return slope / y.iloc[-1] * 100


def rolling_slope_pct(series, window):
    return series.rolling(window).apply(lambda s: regression_slope_pct(s, window), raw=False)


def latest_file(directory, pattern):
    files = sorted(Path(directory).glob(pattern))
    return files[-1] if files else None


def load_local_env():
    """加载本地 .env 配置。只填充当前进程缺失的环境变量，不覆盖外部环境。"""
    candidates = [
        Path(os.getcwd()) / ".env",
        Path(PROJECT_ROOT) / ".env",
        Path(__file__).resolve().parents[1] / ".env",
    ]
    seen = set()
    for path in candidates:
        if path in seen or not path.exists():
            continue
        seen.add(path)
        try:
            for raw_line in path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
        except OSError:
            continue


def normalize_code(raw):
    return str(raw).strip().strip('="').zfill(6)


# ===================== 股票名称解析 =====================

def read_code_name_csv(path):
    items = {}
    if not path or not os.path.exists(path):
        return items
    try:
        with open(path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code_key = "股票代码" if "股票代码" in row else "code"
                name_key = "股票名称" if "股票名称" in row else "name"
                if code_key in row and name_key in row:
                    code = normalize_code(row[code_key])
                    name = row[name_key].strip()
                    if code and name:
                        items[code] = name
    except Exception:
        return items
    return items


def load_known_names():
    names = {}
    for pattern_dir, pattern in [(POOL_DIR, "pool_*.csv"), (QUANT_DIR, "quant_*.csv")]:
        for path in sorted(Path(pattern_dir).glob(pattern)):
            names.update(read_code_name_csv(path))
    names.update(read_code_name_csv(os.path.join(REPORTS_DIR, "_index.csv")))
    return names


def load_pool_codes(pool_path):
    codes = []
    with open(pool_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = normalize_code(row["股票代码"])
            name = row["股票名称"].strip()
            codes.append((code, name))
    return codes


def resolve_input_codes(args, today_yy):
    known_names = load_known_names()
    if args.code:
        code = normalize_code(args.code)
        name = args.name or known_names.get(code, code)
        return [(code, name)], "单股"

    if args.codes:
        codes = []
        for raw_code in args.codes.split(","):
            code = normalize_code(raw_code)
            codes.append((code, known_names.get(code, code)))
        return codes, "多股"

    if args.test:
        return [(c, known_names.get(c, c)) for c in TEST_CODES], "测试"

    pool_path = args.pool or f"{POOL_DIR}/pool_{today_yy}.csv"
    if not os.path.exists(pool_path):
        raise FileNotFoundError(f"池文件不存在: {pool_path}")
    return load_pool_codes(pool_path), "全量"


# ===================== 数据拉取 =====================

def _find_field(ind_map, field_key):
    aliases = FIELD_ALIASES.get(field_key, [field_key])
    for alias in aliases:
        if alias in ind_map:
            return ind_map[alias]
    return None


def _resolve_fields(ind_map, required_fields):
    resolved = {}
    for field in required_fields:
        idx = _find_field(ind_map, field)
        if idx is None:
            return None, f"nameMap缺少字段'{field}'(可用: {list(ind_map.keys())})"
        resolved[field] = idx
    return resolved, None


def _select_history_table(tables, required_fields):
    """Select the table that contains OHLCV history fields.

    The MX query API can return multiple tables. Some stocks put the full
    historical table at index 0 while others put it at index 1, so fixed index
    access is brittle.
    """
    errors = []
    for idx, table in enumerate(tables or []):
        try:
            raw = table["rawTable"]
            name_map = table["nameMap"]
        except (KeyError, TypeError):
            errors.append(f"table[{idx}]缺rawTable/nameMap")
            continue

        ind_map = {v: k for k, v in name_map.items() if k != "headNameSub"}
        fields, err = _resolve_fields(ind_map, required_fields)
        if err:
            errors.append(f"table[{idx}]: {err}")
            continue
        if not raw.get("headName"):
            errors.append(f"table[{idx}]缺headName")
            continue
        return raw, fields, None

    return None, None, "未找到完整历史行情表(" + "；".join(errors[:3]) + ")"


def fetch_daily(code, name, datestr, use_cache=True):
    """拉取近 200 个交易日日线。返回 (DataFrame, source) 或 (None, error)。"""
    if use_cache:
        cached = cache.load(code, datestr)
        if cached is not None:
            return cached, "cache"

    api_key = os.environ.get("MX_APIKEY")
    if not api_key:
        return None, "FATAL:环境变量 MX_APIKEY 未设置，且未命中缓存"

    query = f"{name}近200个交易日每日开盘价、最高价、最低价、收盘价、成交量、换手率"
    headers = {"Content-Type": "application/json", "apikey": api_key}
    payload = {"toolQuery": query, "toolType": "query_tool"}

    for attempt in range(3):
        try:
            resp = requests.post(BASE_URL, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
        except requests.exceptions.Timeout:
            if attempt < 1:
                _rate_limiter.wait(is_fail=True)
                continue
            return None, "网络超时(重试仍失败)"
        except requests.exceptions.ConnectionError:
            if attempt < 1:
                _rate_limiter.wait(is_fail=True)
                continue
            return None, "连接错误(重试仍失败)"
        except Exception as exc:
            return None, f"网络异常: {exc}"

        code_val = result.get("code", -1)
        if code_val == 0:
            _rate_limiter.reset_fails()
            break
        if code_val == 112:
            if attempt < 2:
                _rate_limiter.wait(is_fail=True)
                time.sleep(5)
                continue
            return None, "code=112(频率限制，重试3次仍失败)"
        if code_val == 113:
            return None, "FATAL:API调用次数达上限(113)，请次日再跑"
        if code_val == 114:
            return None, "FATAL:API Key失效(114)"
        if code_val == 115:
            return None, "code=115(查询无结果)"
        if attempt == 0:
            _rate_limiter.wait(is_fail=True)
            continue
        return None, f"API返回code={code_val}"

    required_fields = ["成交量", "换手率", "开盘价", "最高价", "最低价", "收盘价"]
    try:
        tables = result["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
    except (KeyError, TypeError):
        return None, "数据结构异常(缺dataTableDTOList)"

    raw, fields, err = _select_history_table(tables, required_fields)
    if err:
        return None, err

    rows = []
    for i, d in enumerate(raw["headName"]):
        values = {
            "volume": raw[fields["成交量"]][i],
            "turnover": raw[fields["换手率"]][i],
            "open": raw[fields["开盘价"]][i],
            "high": raw[fields["最高价"]][i],
            "low": raw[fields["最低价"]][i],
            "close": raw[fields["收盘价"]][i],
        }
        if "-" in tuple(values.values()):
            continue
        rows.append({
            "date": d,
            "open": float(values["open"]),
            "high": float(values["high"]),
            "low": float(values["low"]),
            "close": float(values["close"]),
            "volume": float(values["volume"]),
            "turnover": float(values["turnover"]),
        })

    if not rows:
        return None, "无有效交易日数据(可能长期停牌)"

    df = pd.DataFrame(rows).sort_values("date").reset_index(drop=True)
    cache.save(code, datestr, df)
    _rate_limiter.wait(is_fail=False)
    return df, "api"


# ===================== 指标计算 =====================

def calc_atr(df, window=14):
    high_low = df["high"] - df["low"]
    high_close = (df["high"] - df["close"].shift(1)).abs()
    low_close = (df["low"] - df["close"].shift(1)).abs()
    true_range = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    return true_range.rolling(window).mean()


def calc_indicators(df):
    df = df.copy()

    for window in [5, 10, 20, 60, 120]:
        df[f"MA{window}"] = df["close"].rolling(window).mean()

    for window in [5, 10, 20, 60]:
        df[f"vol_ma{window}"] = df["volume"].rolling(window).mean()

    df["amount"] = df["volume"] * df["close"]
    df["amount_ma20"] = df["amount"].rolling(20).mean()
    df["量比"] = df["volume"] / df["vol_ma5"].shift(1)
    df["volume_dry_up"] = df["vol_ma5"] / df["vol_ma20"]
    df["volume_dry_up_60"] = df["vol_ma10"] / df["vol_ma60"]

    df["distance_ma20"] = (df["close"] - df["MA20"]) / df["MA20"] * 100
    df["distance_ma60"] = (df["close"] - df["MA60"]) / df["MA60"] * 100
    df["distance_ma120"] = (df["close"] - df["MA120"]) / df["MA120"] * 100

    for window in [10, 20, 60, 120]:
        df[f"high_{window}"] = df["high"].rolling(window).max()
        df[f"low_{window}"] = df["low"].rolling(window).min()
        df[f"range_{window}"] = (df[f"high_{window}"] - df[f"low_{window}"]) / df[f"low_{window}"] * 100

    df["distance_high_60"] = (df["close"] - df["high_60"]) / df["high_60"] * 100
    df["distance_low_60"] = (df["close"] - df["low_60"]) / df["low_60"] * 100
    df["drawdown_high_120"] = (df["close"] - df["high_120"]) / df["high_120"] * 100

    df["chg_1"] = df["close"].pct_change(1) * 100
    df["chg_5"] = df["close"].pct_change(5) * 100
    df["chg_20"] = df["close"].pct_change(20) * 100
    df["chg_60"] = df["close"].pct_change(60) * 100

    df["MA20_slope"] = rolling_slope_pct(df["MA20"], 5)
    df["MA60_slope"] = rolling_slope_pct(df["MA60"], 10)
    df["MA120_slope"] = rolling_slope_pct(df["MA120"], 20)

    df["ATR14"] = calc_atr(df, 14)
    df["ATR14_pct"] = df["ATR14"] / df["close"] * 100

    # 兼容旧字段名，便于模型四和旧报表逐步迁移。
    df["缩量程度"] = df["volume_dry_up"]
    df["距MA20"] = df["distance_ma20"]
    df["距MA60"] = df["distance_ma60"]
    df["近5日涨幅"] = df["chg_5"]
    df["MA20_MA60差"] = (df["MA20"] - df["MA60"]) / df["MA60"] * 100
    return df


# ===================== 形态识别 =====================

def is_long_upper_shadow(row):
    day_range = row["high"] - row["low"]
    if day_range <= 0:
        return False
    upper = row["high"] - max(row["open"], row["close"])
    return upper / day_range > 0.45 and safe_float(row.get("量比"), 0) > 1.5


def detect_overheat(df):
    latest = df.iloc[-1]
    flags = []
    risk_score = 0

    if safe_float(latest.get("chg_5"), 0) > 20:
        flags.append("OVERHEAT_CHG5")
        risk_score += 25
    if safe_float(latest.get("chg_20"), 0) > 50:
        flags.append("OVERHEAT_CHG20")
        risk_score += 25
    if safe_float(latest.get("distance_ma20"), 0) > 15:
        flags.append("FAR_ABOVE_MA20")
        risk_score += 15
    elif safe_float(latest.get("distance_ma20"), 0) > 10:
        flags.append("EXTENDED_FROM_MA20")
        risk_score += 8

    if is_long_upper_shadow(latest):
        flags.append("LONG_UPPER_SHADOW")
        risk_score += 18

    daily_body = pct(latest["close"] - latest["open"], latest["open"])
    if safe_float(latest.get("量比"), 0) > 1.5 and daily_body is not np.nan and daily_body < 1:
        flags.append("VOLUME_STALL")
        risk_score += 15

    if pd.notna(latest.get("MA20")) and pd.notna(latest.get("MA60")):
        if latest["MA20"] < latest["MA60"] and latest["close"] < latest["MA60"]:
            flags.append("DOWNTREND")
            risk_score += 35
    if safe_float(latest.get("MA20_slope"), 0) < -0.10:
        flags.append("MA20_DECLINE")
        risk_score += 15
    if safe_float(latest.get("distance_high_60"), 0) < -30 and safe_float(latest.get("chg_20"), 0) < -15:
        flags.append("DEEP_FALL")
        risk_score += 35

    return {
        "risk_flags": flags,
        "risk_score": min(risk_score, 100),
        "hard_reject": any(f in flags for f in ["OVERHEAT_CHG5", "OVERHEAT_CHG20", "DOWNTREND", "DEEP_FALL"]),
    }


def detect_vcp_structure(df):
    latest = df.iloc[-1]
    n = len(df)
    conditions = []
    misses = []

    if n < 60:
        return {"has_structure": False, "state": "DATA_INSUFFICIENT", "conditions": [], "misses": ["有效交易日<60"]}

    close = latest["close"]
    ma20 = latest.get("MA20")
    ma60 = latest.get("MA60")

    base_ok = True
    if pd.isna(ma60) or not (close > ma60 or (pd.notna(ma20) and ma20 >= ma60)):
        base_ok = False
        misses.append("趋势基础不足")
    if safe_float(latest.get("MA60_slope"), 0) < -0.03:
        base_ok = False
        misses.append("MA60斜率偏弱")
    if safe_float(latest.get("drawdown_high_120"), 0) < -35:
        base_ok = False
        misses.append("近120日回撤过深")

    checks = [
        ("range_10 < range_20", safe_float(latest.get("range_10"), 999) < safe_float(latest.get("range_20"), -999)),
        ("range_20 < range_60", safe_float(latest.get("range_20"), 999) < safe_float(latest.get("range_60"), -999)),
        ("volume_dry_up < 0.85", safe_float(latest.get("volume_dry_up"), 999) < 0.85),
        ("vol_ma20 < vol_ma60", safe_float(latest.get("vol_ma20"), 999) < safe_float(latest.get("vol_ma60"), -999)),
        ("low_20 > low_60", safe_float(latest.get("low_20"), -999) > safe_float(latest.get("low_60"), 999)),
        ("distance_high_60 >= -15", safe_float(latest.get("distance_high_60"), -999) >= -15),
    ]
    for label, ok in checks:
        if ok:
            conditions.append(label)
        else:
            misses.append(label)

    structure_count = len(conditions)
    has_structure = base_ok and structure_count >= 3

    state = "REJECT"
    if has_structure:
        if safe_float(latest.get("distance_ma20"), 0) > 10:
            state = "P1_HIGH"
        elif safe_float(latest.get("distance_high_60"), -99) >= -6 and safe_float(latest.get("range_10"), 99) < safe_float(latest.get("range_20"), 0):
            state = "P1_TIGHT"
        else:
            state = "P1_FORMING"
    elif base_ok and safe_float(latest.get("distance_ma20"), 0) > 10 and safe_float(latest.get("distance_high_60"), -99) > -12:
        # 强趋势偏高但尚未形成标准 VCP，进入观察而非买点。
        has_structure = True
        state = "P1_HIGH"
        conditions.append("强趋势偏高观察")

    return {
        "has_structure": has_structure,
        "state": state,
        "conditions": conditions,
        "misses": misses,
        "structure_count": structure_count,
    }


def detect_p2_pullback(df, structure, overheat):
    latest = df.iloc[-1]
    if not structure["has_structure"] or structure["state"] == "P1_HIGH":
        return {"hit": False, "reason": "无P1结构或位置偏高"}
    if overheat["hard_reject"]:
        return {"hit": False, "reason": "风险硬排除"}

    near_ma20 = pd.notna(latest.get("distance_ma20")) and -4 <= latest["distance_ma20"] <= 3
    near_ma60 = pd.notna(latest.get("distance_ma60")) and -5 <= latest["distance_ma60"] <= 5
    low20 = latest.get("low_20")
    conditions = [
        safe_float(latest.get("volume_dry_up"), 999) < 0.80,
        near_ma20 or near_ma60,
        pd.notna(low20) and latest["close"] > low20 * 1.03,
        safe_float(latest.get("MA20_slope"), 0) >= -0.03,
        safe_float(latest.get("chg_5"), 0) < 12,
        "LONG_UPPER_SHADOW" not in overheat["risk_flags"],
    ]
    hit = all(conditions)
    anchor = "MA20" if near_ma20 else "MA60" if near_ma60 else ""
    support = safe_float(latest.get(anchor)) if anchor else safe_float(latest.get("MA20"))
    invalid = None
    if support:
        invalid = min(support * 0.97, safe_float(low20, support) * 0.98)
    return {
        "hit": hit,
        "anchor": anchor,
        "support_price": support,
        "invalid_price": invalid,
        "reason": f"缩量回踩{anchor}" if hit else "P2条件不足",
    }


def find_recent_breakout(df, lookback=10):
    n = len(df)
    start = max(60, n - lookback - 1)
    end = n - 1
    for idx in range(start, end):
        prev = df.iloc[:idx]
        if len(prev) < 60:
            continue
        row = df.iloc[idx]
        level = prev["high"].tail(60).max()
        vol_ma20 = row.get("vol_ma20")
        if pd.isna(vol_ma20) or vol_ma20 <= 0:
            continue
        if row["close"] > level * 1.01 and row["volume"] > vol_ma20 * 1.5 and not is_long_upper_shadow(row):
            return {"idx": idx, "date": row["date"], "level": float(level)}
    return None


def detect_p3_retest(df, structure, overheat):
    latest = df.iloc[-1]
    if overheat["hard_reject"]:
        return {"hit": False, "reason": "风险硬排除"}
    breakout = find_recent_breakout(df, lookback=12)
    if not breakout:
        return {"hit": False, "reason": "近期无有效突破"}

    days_after = len(df) - breakout["idx"] - 1
    if days_after < 3 or days_after > 10:
        return {"hit": False, "breakout_level": breakout["level"], "reason": "突破后天数不在3-10日"}

    post = df.iloc[breakout["idx"] + 1:]
    pullback_low = post["low"].min()
    volume_ok = safe_float(post["volume"].tail(min(5, len(post))).mean(), 0) < safe_float(df.iloc[breakout["idx"]]["volume"], 0)
    close_ok = latest["close"] >= breakout["level"] or (pd.notna(latest.get("MA10")) and latest["close"] >= latest["MA10"])
    hit = pullback_low >= breakout["level"] * 0.97 and volume_ok and close_ok
    return {
        "hit": hit,
        "breakout_level": breakout["level"],
        "support_price": breakout["level"],
        "invalid_price": breakout["level"] * 0.97,
        "reason": "突破后缩量回踩确认" if hit else "P3回踩确认不足",
    }


def score_setup(df, structure, p2, p3, overheat):
    latest = df.iloc[-1]
    score = 0

    structure_score = min(len(structure.get("conditions", [])) * 4, 25)
    volume_score = 0
    if safe_float(latest.get("volume_dry_up"), 999) < 0.80:
        volume_score += 10
    if safe_float(latest.get("vol_ma20"), 999) < safe_float(latest.get("vol_ma60"), -999):
        volume_score += 5

    trend_score = 0
    if pd.notna(latest.get("MA20")) and pd.notna(latest.get("MA60")) and latest["MA20"] >= latest["MA60"]:
        trend_score += 7
    if safe_float(latest.get("MA20_slope"), 0) >= 0:
        trend_score += 4
    if safe_float(latest.get("MA60_slope"), 0) >= -0.03:
        trend_score += 4

    position_score = 0
    dist20 = safe_float(latest.get("distance_ma20"), 999)
    dist_high60 = safe_float(latest.get("distance_high_60"), -999)
    if -4 <= dist20 <= 3:
        position_score += 10
    elif dist20 <= 10:
        position_score += 5
    if -12 <= dist_high60 <= 0:
        position_score += 5

    buy_point_score = 0
    if p3.get("hit"):
        buy_point_score = 20
    elif p2.get("hit"):
        buy_point_score = 16
    elif structure.get("state") == "P1_TIGHT":
        buy_point_score = 10
    elif structure.get("state") == "P1_FORMING":
        buy_point_score = 6
    elif structure.get("state") == "P1_HIGH":
        buy_point_score = 3

    score = structure_score + volume_score + trend_score + position_score + buy_point_score
    score = max(0, min(100, score - overheat["risk_score"]))

    return {
        "setup_score": round(score, 2),
        "risk_score": overheat["risk_score"],
        "components": {
            "structure": structure_score,
            "volume": volume_score,
            "trend": trend_score,
            "position": position_score,
            "buy_point": buy_point_score,
            "risk_penalty": overheat["risk_score"],
        }
    }


def classify_result(structure, p2, p3, score, overheat):
    if overheat["hard_reject"] and not structure.get("state") == "P1_HIGH":
        return "REJECT", "REJECT", "none", "0"
    if p3.get("hit"):
        return "VCP", "P3_RETEST", "standard_position", "60%-80%"
    if p2.get("hit"):
        return "VCP", "P2_PULLBACK", "light_position", "20%-30%"
    if structure.get("state") in ["P1_TIGHT", "P1_FORMING", "P1_HIGH"]:
        return "VCP", structure["state"], "watch", "0"
    if score["setup_score"] >= 50:
        return "VCP", "LOW_PRIORITY", "watch", "0"
    return "NONE", "REJECT", "none", "0"


def pool_type_for_state(state, score):
    if state in ["P2_PULLBACK", "P3_RETEST"] and score >= 65:
        return "TRADE_CANDIDATE"
    if state in ["P1_TIGHT", "P1_FORMING", "P1_HIGH"]:
        return "RESEARCH_WATCH"
    if score >= 50:
        return "LOW_PRIORITY"
    return "REJECT"


def build_reason(structure, p2, p3, overheat):
    if p3.get("hit"):
        return p3["reason"]
    if p2.get("hit"):
        return p2["reason"]
    if structure.get("state") == "P1_HIGH":
        return "强趋势偏高，等待回踩或进一步收敛"
    if structure.get("has_structure"):
        return "VCP结构观察：" + "、".join(structure.get("conditions", [])[:3])
    if overheat["risk_flags"]:
        return "风险排除：" + "、".join(overheat["risk_flags"])
    return "未形成有效VCP结构"


def screen(df):
    """确定性识别 P1/P2/P3，返回结构化结果。"""
    latest = df.iloc[-1]
    if pd.isna(latest.get("MA20")):
        return {
            "pattern": "NONE", "state": "DATA_INSUFFICIENT", "pool_type": "REJECT",
            "setup_score": 0, "risk_score": 0, "action_hint": "none", "suggested_position": "0",
            "support_price": None, "invalid_price": None, "breakout_level": None,
            "reason": f"MA20=NaN，仅{len(df)}个有效交易日", "risk_flags": [],
            "score_components": {},
        }

    overheat = detect_overheat(df)
    structure = detect_vcp_structure(df)
    p2 = detect_p2_pullback(df, structure, overheat)
    p3 = detect_p3_retest(df, structure, overheat)
    score = score_setup(df, structure, p2, p3, overheat)
    pattern, state, action_hint, suggested_position = classify_result(structure, p2, p3, score, overheat)
    pool_type = pool_type_for_state(state, score["setup_score"])

    support = p3.get("support_price") or p2.get("support_price")
    invalid = p3.get("invalid_price") or p2.get("invalid_price")
    breakout = p3.get("breakout_level")

    return {
        "pattern": pattern,
        "state": state,
        "pool_type": pool_type,
        "setup_score": score["setup_score"],
        "risk_score": score["risk_score"],
        "action_hint": action_hint,
        "suggested_position": suggested_position,
        "support_price": round_or_none(support),
        "invalid_price": round_or_none(invalid),
        "breakout_level": round_or_none(breakout),
        "reason": build_reason(structure, p2, p3, overheat),
        "risk_flags": overheat["risk_flags"],
        "score_components": score["components"],
        "structure_conditions": structure.get("conditions", []),
        "structure_misses": structure.get("misses", []),
    }


# ===================== 输出 =====================

CSV_COLUMNS = [
    "股票代码", "股票名称", "pattern", "state", "pool_type", "setup_score", "risk_score",
    "action_hint", "suggested_position", "support_price", "invalid_price", "breakout_level",
    "close", "MA20", "MA60", "MA120", "MA20_slope", "MA60_slope",
    "range_10", "range_20", "range_60", "volume_dry_up",
    "distance_ma20", "distance_ma60", "distance_high_60",
    "chg_5", "chg_20", "reason", "risk_flags", "run_date",
]


def result_from_df(code, name, df, run_date):
    latest = df.iloc[-1]
    decision = screen(df)
    result = {
        "code": code,
        "name": name,
        "run_date": run_date,
        **decision,
        "close": round_or_none(latest.get("close")),
        "MA20": round_or_none(latest.get("MA20")),
        "MA60": round_or_none(latest.get("MA60")),
        "MA120": round_or_none(latest.get("MA120")),
        "MA20_slope": round_or_none(latest.get("MA20_slope"), 4),
        "MA60_slope": round_or_none(latest.get("MA60_slope"), 4),
        "range_10": round_or_none(latest.get("range_10")),
        "range_20": round_or_none(latest.get("range_20")),
        "range_60": round_or_none(latest.get("range_60")),
        "volume_dry_up": round_or_none(latest.get("volume_dry_up"), 4),
        "distance_ma20": round_or_none(latest.get("distance_ma20")),
        "distance_ma60": round_or_none(latest.get("distance_ma60")),
        "distance_high_60": round_or_none(latest.get("distance_high_60")),
        "chg_5": round_or_none(latest.get("chg_5")),
        "chg_20": round_or_none(latest.get("chg_20")),
        "data_days": len(df),
        "last_trade_date": str(latest.get("date")),
    }
    return result


def should_write_to_quant(result, include_reject=False):
    if include_reject:
        return True
    return result["pool_type"] != "REJECT"


def write_csv(results, quant_path):
    os.makedirs(os.path.dirname(quant_path), exist_ok=True)
    with open(quant_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in results:
            writer.writerow([
                f'="{r["code"]}"',
                r["name"],
                r["pattern"],
                r["state"],
                r["pool_type"],
                r["setup_score"],
                r["risk_score"],
                r["action_hint"],
                r["suggested_position"],
                r["support_price"] if r["support_price"] is not None else "",
                r["invalid_price"] if r["invalid_price"] is not None else "",
                r["breakout_level"] if r["breakout_level"] is not None else "",
                r["close"] if r["close"] is not None else "",
                r["MA20"] if r["MA20"] is not None else "",
                r["MA60"] if r["MA60"] is not None else "",
                r["MA120"] if r["MA120"] is not None else "",
                r["MA20_slope"] if r["MA20_slope"] is not None else "",
                r["MA60_slope"] if r["MA60_slope"] is not None else "",
                r["range_10"] if r["range_10"] is not None else "",
                r["range_20"] if r["range_20"] is not None else "",
                r["range_60"] if r["range_60"] is not None else "",
                r["volume_dry_up"] if r["volume_dry_up"] is not None else "",
                r["distance_ma20"] if r["distance_ma20"] is not None else "",
                r["distance_ma60"] if r["distance_ma60"] is not None else "",
                r["distance_high_60"] if r["distance_high_60"] is not None else "",
                r["chg_5"] if r["chg_5"] is not None else "",
                r["chg_20"] if r["chg_20"] is not None else "",
                r["reason"],
                ";".join(r["risk_flags"]),
                r["run_date"],
            ])
    print(f"\n精选池已输出: {quant_path}")


def write_json(payload, json_path):
    os.makedirs(os.path.dirname(json_path), exist_ok=True)
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"结构化结果已输出: {json_path}")


def print_single_summary(result):
    print()
    print("=" * 70)
    print(f"{result['name']} {result['code']} | {result['last_trade_date']}")
    print("=" * 70)
    print(f"状态: {result['state']} | {result['pool_type']} | 分数 {result['setup_score']} / 风险 {result['risk_score']}")
    print(f"动作: {result['action_hint']} | 建议仓位 {result['suggested_position']}")
    print(f"收盘: {result['close']} | MA20 {result['MA20']} | MA60 {result['MA60']}")
    print(f"距MA20: {result['distance_ma20']}% | 距MA60: {result['distance_ma60']}% | 距60日高点: {result['distance_high_60']}%")
    print(f"支撑: {result['support_price']} | 失效: {result['invalid_price']} | 突破位: {result['breakout_level']}")
    print(f"结论: {result['reason']}")
    if result["risk_flags"]:
        print(f"风险: {', '.join(result['risk_flags'])}")


def print_summary(total, pull_ok, pull_fail, data_insufficient, results, cache_hits=0, api_calls=0):
    print()
    print("=" * 70)
    print("模型二执行摘要 — VCP/P2/P3 量价精筛")
    print("=" * 70)
    print(f"  输入标的: {total} 只")
    print(f"  数据来源: 缓存命中 {cache_hits} 只 + API 拉取 {api_calls} 只")
    print(f"  成功拉取行情: {pull_ok} 只（失败: {pull_fail} 只）")
    print(f"  数据不足跳过: {data_insufficient} 只")

    by_state = {}
    by_pool = {}
    for r in results:
        by_state[r["state"]] = by_state.get(r["state"], 0) + 1
        by_pool[r["pool_type"]] = by_pool.get(r["pool_type"], 0) + 1
    print(f"  输出结果: {len(results)} 只")
    print(f"  池子分层: {by_pool}")
    print(f"  状态分布: {by_state}")

    top = sorted([r for r in results if r["pool_type"] != "REJECT"], key=lambda x: x["setup_score"], reverse=True)[:20]
    if top:
        print()
        print(f"{'代码':<8} {'名称':<8} {'状态':<14} {'分数':>6} {'风险':>6} {'结论'}")
        print("-" * 100)
        for r in top:
            print(f"{r['code']:<8} {r['name']:<8} {r['state']:<14} {r['setup_score']:>6.1f} {r['risk_score']:>6.1f} {r['reason'][:36]}")


# ===================== LLM 可选解释 =====================

def maybe_call_llm(results, top_n):
    """可选 DeepSeek 解释层。未配置时跳过，主流程不受影响。"""
    if not results:
        return {"status": "skipped", "reason": "no_results", "reviews": []}
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not api_key:
        return {"status": "skipped", "reason": "DEEPSEEK_API_KEY missing", "reviews": []}

    selected = sorted(results, key=lambda x: x["setup_score"], reverse=True)[:top_n]
    system_prompt = (
        "你是量价形态复核助手。只解释脚本结果，不改变脚本对 P1/P2/P3 的判定。"
        "只能使用输入 JSON 中已有的指标和字段，不得引入脚本未提供的新指标或外部事实。"
        "必须输出合法 JSON 对象，格式为："
        '{"reviews":[{"code":"300604","pattern_review":"...","risk_notes":["..."],'
        '"watch_points":["..."],"confidence":"low|medium|high"}]}'
    )
    user_prompt = (
        "请基于以下脚本结果输出 JSON 复核意见。不要输出 markdown，不要输出 JSON 之外的文本。\n"
        + json.dumps(selected, ensure_ascii=False)
    )
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
                "temperature": 0.2,
                "max_tokens": 2000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content", "")
        parsed = json.loads(content)
        reviews = parsed.get("reviews", [])
        if not isinstance(reviews, list):
            raise ValueError("DeepSeek JSON response missing reviews list")
        return {
            "status": "ok",
            "provider": "deepseek",
            "model": model,
            "reviews": reviews,
            "usage": data.get("usage", {}),
        }
    except Exception as exc:
        return {"status": "failed", "provider": "deepseek", "model": model, "reason": str(exc), "reviews": []}


# ===================== 主流程 =====================

def build_output_paths(args, today_yy, mode, codes):
    if args.quant:
        quant_path = args.quant
    elif mode == "单股":
        quant_path = f"{QUANT_DIR}/single_{codes[0][0]}_{today_yy}.csv"
    elif mode == "多股":
        quant_path = f"{QUANT_DIR}/multi_{today_yy}.csv"
    elif mode == "测试":
        quant_path = f"{QUANT_DIR}/quant_{today_yy}_test.csv"
    else:
        quant_path = f"{QUANT_DIR}/quant_{today_yy}.csv"

    if args.output_json:
        json_path = args.output_json
    elif mode == "单股":
        json_path = f"{QUANT_RUNS_DIR}/{codes[0][0]}_{today_yy}.json"
    else:
        json_path = f"{QUANT_RUNS_DIR}/quant_{today_yy}.json"
    return quant_path, json_path


def process_codes(codes, today_yy, run_date, use_cache=True, allow_retry=True):
    results = []
    stats = {"pull_ok": 0, "pull_fail": 0, "data_insufficient": 0, "cache_hits": 0, "api_calls": 0}
    fatal_stop = False
    retry_queue = []

    for i, (code, name) in enumerate(codes):
        if fatal_stop:
            print(f"[{i+1}/{len(codes)}] {code} {name} ... 因上游致命错误跳过")
            stats["pull_fail"] += 1
            continue

        print(f"[{i+1}/{len(codes)}] {code} {name} ...", end=" ", flush=True)
        df, source = fetch_daily(code, name, today_yy, use_cache=use_cache)
        if df is None:
            if allow_retry and "112" in str(source):
                print(f"失败: {source}，加入重试队列")
                retry_queue.append((code, name))
            else:
                print(f"失败: {source}")
                stats["pull_fail"] += 1
                if str(source).startswith("FATAL:"):
                    fatal_stop = True
            continue

        stats["cache_hits" if source == "cache" else "api_calls"] += 1
        if len(df) < 20:
            print(f"跳过: 数据不足({len(df)}天)")
            stats["data_insufficient"] += 1
            continue

        stats["pull_ok"] += 1
        df = calc_indicators(df)
        result = result_from_df(code, name, df, run_date)
        results.append(result)
        print(f"{result['state']} | score={result['setup_score']} | {result['reason'][:48]}")

    if retry_queue:
        print(f"\n重试 {len(retry_queue)} 只频率限制失败标的...")
        time.sleep(10)
        retry_results, retry_stats = process_codes(retry_queue, today_yy, run_date, use_cache=False, allow_retry=False)
        results.extend(retry_results)
        for key, val in retry_stats.items():
            stats[key] += val

    return results, stats


def main():
    load_local_env()

    parser = argparse.ArgumentParser(description="模型二：VCP/P2/P3 量价精筛")
    parser.add_argument("--pool", help="模型一池文件路径（默认取当天 pool/pool_YYMMDD.csv）")
    parser.add_argument("--code", help="单只股票代码")
    parser.add_argument("--codes", help="多只股票代码，逗号分隔")
    parser.add_argument("--name", help="单股模式下手工指定股票名称")
    parser.add_argument("--test", action="store_true", help="测试模式，使用内建标的")
    parser.add_argument("--quant", help="CSV 输出路径")
    parser.add_argument("--output-json", help="JSON 输出路径")
    parser.add_argument("--json", action="store_true", help="同时将结构化结果打印到 stdout")
    parser.add_argument("--include-reject", action="store_true", help="CSV 中包含 REJECT 标的")
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存，强制从 API 拉取")
    parser.add_argument("--refresh", action="store_true", help="清除今日缓存后重新拉取")
    parser.add_argument("--with-llm", action="store_true", help="可选调用 LLM 对 top 标的做解释")
    parser.add_argument("--llm-top", type=int, default=10, help="LLM 解释 Top N，默认 10")
    args = parser.parse_args()

    today_yy = datetime.now().strftime("%y%m%d")
    run_date = datetime.now().strftime("%Y-%m-%d")
    use_cache = not args.no_cache

    try:
        codes, mode = resolve_input_codes(args, today_yy)
    except FileNotFoundError as exc:
        print(f"错误: {exc}")
        print("可用 --pool 指定路径，或 --code/--codes 做单股分析。")
        sys.exit(1)

    quant_path, json_path = build_output_paths(args, today_yy, mode, codes)

    print("=" * 70)
    print("模型二：VCP/P2/P3 量价精筛")
    print(f"模式: {mode} | 标的: {len(codes)} 只 | CSV: {quant_path}")
    print(f"缓存: {'关闭' if args.no_cache else '开启'} | 目录: {cache.cache_dir}")
    print("=" * 70)

    cleaned = cache.cleanup_old(keep_days=5)
    if cleaned:
        print(f"已清理 {cleaned} 个超过5天的旧缓存文件")
    if args.refresh:
        cleared = cache.clear_today(today_yy)
        print(f"已清除今日缓存 {cleared} 个文件")

    results, stats = process_codes(codes, today_yy, run_date, use_cache=use_cache)
    csv_results = [r for r in results if should_write_to_quant(r, include_reject=args.include_reject)]
    csv_results.sort(key=lambda x: x["setup_score"], reverse=True)

    llm_payload = {"status": "skipped", "reason": "not_requested", "reviews": []}
    if args.with_llm:
        llm_payload = maybe_call_llm(csv_results, args.llm_top)

    payload = {
        "meta": {
            "run_date": run_date,
            "mode": mode,
            "total": len(codes),
            "csv_path": quant_path,
            "schema": "quant_vcp_p123",
        },
        "stats": stats,
        "llm": llm_payload,
        "results": results,
    }

    print_summary(len(codes), stats["pull_ok"], stats["pull_fail"], stats["data_insufficient"],
                  results, stats["cache_hits"], stats["api_calls"])

    if csv_results:
        write_csv(csv_results, quant_path)
    else:
        print("\n无非 REJECT 标的，CSV 未写入。可用 --include-reject 输出完整结果。")
    write_json(payload, json_path)

    if mode == "单股" and results:
        print_single_summary(results[0])
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
