"""
模型二：量价精筛 — VCP 结构与触发信号脚本驱动实现
对应指令: instructions/02-quant.md

用法:
  python3 scripts/quant_filter.py
  python3 scripts/quant_filter.py --date 260707
  python3 scripts/quant_filter.py --pool pool/pool_260703.csv
  python3 scripts/quant_filter.py --code 300604 --name 长川科技
  python3 scripts/quant_filter.py --codes 300604,300442
  python3 scripts/quant_filter.py --code 300604 --with-llm

核心原则:
  脚本负责数据、指标、形态、评分、输出；LLM 只做可选解释，不参与结构阶段和触发信号判定。
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
from scripts.shared import DailyCache, PROJECT_ROOT, VALUATION_INDEX_PATH, fetch_daily, expected_trade_date
from scripts.strategy_config import load_strategy_config


# ===================== 配置 =====================

POOL_DIR = os.path.join(PROJECT_ROOT, "pool")
QUANT_DIR = os.path.join(PROJECT_ROOT, "quant")
QUANT_RUNS_DIR = os.path.join(PROJECT_ROOT, "cache", "quant_runs")
QUANT_STRATEGY_FILE = "02-quant.json"
QUANT_STRATEGY, QUANT_STRATEGY_PATH = load_strategy_config(QUANT_STRATEGY_FILE)
STRATEGY_VERSION = QUANT_STRATEGY["strategy_version"]

VCP_CFG = QUANT_STRATEGY["vcp"]
BASE_CFG = QUANT_STRATEGY["base_rules"]
CONTRACTION_CFG = QUANT_STRATEGY["contraction_rules"]
STAGE_CFG = QUANT_STRATEGY["stage_rules"]
RISK_CFG = QUANT_STRATEGY["risk_rules"]
SETUP_CFG = QUANT_STRATEGY["setup_rules"]
SCORE_CFG = QUANT_STRATEGY["scores"]
CLASSIFICATION_CFG = QUANT_STRATEGY["classification"]
SETUP_SCORING_CFG = QUANT_STRATEGY.get("setup_scoring", {})

TEST_CODES = [
    "688256", "301329", "300394", "300308", "002028",
    "300750", "002008", "688285", "002460", "300127",
    "688235", "920946", "688295", "002054", "601777",
    "603409", "600176", "301526", "300442",
]

VCP_LOOKBACK = VCP_CFG["lookback_days"]
VCP_SWING_WINDOW = VCP_CFG["swing_window"]
VCP_MIN_PULLBACK_PCT = VCP_CFG["min_pullback_pct"]
VCP_MAX_PULLBACK_PCT = VCP_CFG["max_pullback_pct"]
VCP_MIN_PULLBACK_DAYS = VCP_CFG["min_pullback_days"]
VCP_MAX_PULLBACK_DAYS = VCP_CFG["max_pullback_days"]
VCP_MAX_STRUCTURE_AGE_DAYS = VCP_CFG["max_structure_age_days"]
VCP_MIN_PIVOT_DISTANCE = VCP_CFG["min_pivot_distance_pct"]
VCP_MAX_POST_GAIN = VCP_CFG["max_post_gain_pct"]
VCP_MAX_POST_DRAWDOWN = VCP_CFG["max_post_drawdown_pct"]

SETUP_QUALITY_THRESHOLDS = SETUP_SCORING_CFG.get("quality_thresholds", {"A": 80, "B": 70, "C": 60})


# ===================== 通用工具 =====================

def normalize_run_date(value):
    """Normalize an optional CLI date to YYYY-MM-DD."""
    if not value:
        return expected_trade_date()

    raw = value.strip()
    if len(raw) == 6 and raw.isdigit():
        parsed = datetime.strptime(raw, "%y%m%d")
        return expected_trade_date(parsed.strftime("%Y-%m-%d"))
    if len(raw) == 10:
        parsed = datetime.strptime(raw, "%Y-%m-%d")
        return expected_trade_date(parsed.strftime("%Y-%m-%d"))

    raise ValueError("日期格式必须为 YYMMDD 或 YYYY-MM-DD")


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
    names.update(read_code_name_csv(VALUATION_INDEX_PATH))
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
    return (
        upper / day_range > RISK_CFG["long_upper_shadow_ratio"]
        and safe_float(row.get("量比"), 0) > RISK_CFG["long_upper_shadow_volume_ratio"]
    )


def detect_overheat(df):
    latest = df.iloc[-1]
    flags = []
    risk_score = 0

    risk_scores = RISK_CFG["risk_scores"]
    if safe_float(latest.get("chg_5"), 0) > RISK_CFG["overheat_chg5_pct"]:
        flags.append("OVERHEAT_CHG5")
        risk_score += risk_scores["OVERHEAT_CHG5"]
    if safe_float(latest.get("chg_20"), 0) > RISK_CFG["overheat_chg20_pct"]:
        flags.append("OVERHEAT_CHG20")
        risk_score += risk_scores["OVERHEAT_CHG20"]
    if safe_float(latest.get("distance_ma20"), 0) > RISK_CFG["far_above_ma20_pct"]:
        flags.append("FAR_ABOVE_MA20")
        risk_score += risk_scores["FAR_ABOVE_MA20"]
    elif safe_float(latest.get("distance_ma20"), 0) > RISK_CFG["extended_from_ma20_pct"]:
        flags.append("EXTENDED_FROM_MA20")
        risk_score += risk_scores["EXTENDED_FROM_MA20"]

    if is_long_upper_shadow(latest):
        flags.append("LONG_UPPER_SHADOW")
        risk_score += risk_scores["LONG_UPPER_SHADOW"]

    daily_body = pct(latest["close"] - latest["open"], latest["open"])
    if (
        safe_float(latest.get("量比"), 0) > RISK_CFG["volume_stall_ratio"]
        and daily_body is not np.nan
        and daily_body < RISK_CFG["volume_stall_max_body_pct"]
    ):
        flags.append("VOLUME_STALL")
        risk_score += risk_scores["VOLUME_STALL"]

    if pd.notna(latest.get("MA20")) and pd.notna(latest.get("MA60")):
        if latest["MA20"] < latest["MA60"] and latest["close"] < latest["MA60"]:
            flags.append("DOWNTREND")
            risk_score += risk_scores["DOWNTREND"]
    if safe_float(latest.get("MA20_slope"), 0) < RISK_CFG["ma20_decline_slope"]:
        flags.append("MA20_DECLINE")
        risk_score += risk_scores["MA20_DECLINE"]
    if (
        safe_float(latest.get("distance_high_60"), 0) < RISK_CFG["deep_fall_distance_high_60_pct"]
        and safe_float(latest.get("chg_20"), 0) < RISK_CFG["deep_fall_chg20_pct"]
    ):
        flags.append("DEEP_FALL")
        risk_score += risk_scores["DEEP_FALL"]
    if pd.notna(latest.get("MA120")) and latest["close"] < latest["MA120"]:
        flags.append("BELOW_MA120")
        risk_score += risk_scores.get("BELOW_MA120", 20)

    return {
        "risk_flags": flags,
        "risk_score": min(risk_score, SCORE_CFG["max_score"]),
        "hard_reject": any(f in flags for f in RISK_CFG["hard_reject_flags"]),
    }


def find_swings(df, lookback=VCP_LOOKBACK, window=VCP_SWING_WINDOW):
    """Find alternating local highs/lows in the recent window."""
    start = max(0, len(df) - lookback)
    swings = []
    for idx in range(start + window, len(df) - window):
        cur_high = df.iloc[idx]["high"]
        cur_low = df.iloc[idx]["low"]
        local = df.iloc[idx - window:idx + window + 1]
        local_highs = local["high"].to_numpy()
        local_lows = local["low"].to_numpy()
        other_highs = np.concatenate([local_highs[:window], local_highs[window + 1:]])
        other_lows = np.concatenate([local_lows[:window], local_lows[window + 1:]])

        is_high = cur_high >= local_highs.max() and cur_high > other_highs.max()
        is_low = cur_low <= local_lows.min() and cur_low < other_lows.min()
        if is_high:
            swings.append({"idx": idx, "type": "high", "price": float(cur_high), "date": df.iloc[idx]["date"]})
        if is_low:
            swings.append({"idx": idx, "type": "low", "price": float(cur_low), "date": df.iloc[idx]["date"]})

    swings.sort(key=lambda x: x["idx"])
    alternating = []
    for swing in swings:
        if not alternating or alternating[-1]["type"] != swing["type"]:
            alternating.append(swing)
            continue
        prev = alternating[-1]
        if swing["type"] == "high" and swing["price"] > prev["price"]:
            alternating[-1] = swing
        elif swing["type"] == "low" and swing["price"] < prev["price"]:
            alternating[-1] = swing
    return alternating


def detect_contractions(df):
    swings = find_swings(df)
    contractions = []
    for i, swing in enumerate(swings[:-1]):
        if swing["type"] != "high" or swings[i + 1]["type"] != "low":
            continue
        high = swing
        low = swings[i + 1]
        duration = low["idx"] - high["idx"] + 1
        if duration < VCP_MIN_PULLBACK_DAYS or duration > VCP_MAX_PULLBACK_DAYS:
            continue

        pullback_pct = (low["price"] - high["price"]) / high["price"] * 100
        abs_pullback = abs(pullback_pct)
        if abs_pullback < VCP_MIN_PULLBACK_PCT or abs_pullback > VCP_MAX_PULLBACK_PCT:
            continue

        next_high_idx = None
        for later in swings[i + 2:]:
            if later["type"] == "high":
                next_high_idx = later["idx"]
                break
        recovery_slice = df.iloc[low["idx"] + 1:(next_high_idx + 1 if next_high_idx else len(df))]
        max_after = recovery_slice["high"].max() if not recovery_slice.empty else df.iloc[-1]["close"]
        recovery_pct = (max_after - low["price"]) / low["price"] * 100 if low["price"] else 0
        min_recovery = min(VCP_CFG["min_recovery_pct"], abs_pullback * VCP_CFG["min_recovery_pullback_ratio"])
        if recovery_pct < min_recovery and low["idx"] < len(df) - VCP_CFG["recovery_grace_days"]:
            continue

        segment = df.iloc[high["idx"]:low["idx"] + 1]
        contractions.append({
            "start_idx": high["idx"],
            "end_idx": low["idx"],
            "start_date": str(high["date"]),
            "end_date": str(low["date"]),
            "high_price": high["price"],
            "low_price": low["price"],
            "pullback_pct": round(pullback_pct, 2),
            "duration_days": int(duration),
            "avg_volume": float(segment["volume"].mean()),
            "recovery_pct": round(float(recovery_pct), 2),
        })
    return contractions


def contraction_decrease_status(contractions):
    if len(contractions) < 2:
        return {"strict_pairs": 0, "near_pairs": 0, "is_strict": False, "is_near": False}
    recent = contractions[-CONTRACTION_CFG["max_recent_contractions"]:]
    strict_pairs = 0
    near_pairs = 0
    for prev, cur in zip(recent, recent[1:]):
        prev_abs = abs(prev["pullback_pct"])
        cur_abs = abs(cur["pullback_pct"])
        if cur_abs <= prev_abs * CONTRACTION_CFG["strict_decrease_ratio"]:
            strict_pairs += 1
        if cur_abs <= prev_abs * CONTRACTION_CFG["near_decrease_ratio"]:
            near_pairs += 1
    needed = len(recent) - 1
    return {
        "strict_pairs": strict_pairs,
        "near_pairs": near_pairs,
        "is_strict": needed > 0 and strict_pairs == needed,
        "is_near": needed > 0 and near_pairs == needed,
    }


def volume_pattern_for_contractions(contractions, latest):
    recent = contractions[-CONTRACTION_CFG["max_recent_contractions"]:]
    vols = [c["avg_volume"] for c in recent if c.get("avg_volume")]
    if len(vols) >= 2 and all(cur < prev * CONTRACTION_CFG["volume_decrease_ratio"] for prev, cur in zip(vols, vols[1:])):
        return "decreasing"
    if (
        safe_float(latest.get("volume_dry_up"), 999) < CONTRACTION_CFG["volume_dry_up_threshold"]
        or safe_float(latest.get("vol_ma20"), 999) < safe_float(latest.get("vol_ma60"), -999)
    ):
        return "drying"
    if len(vols) >= 2 and vols[-1] > vols[-2] * CONTRACTION_CFG["volume_failed_ratio"]:
        return "failed"
    return "mixed"


def contraction_group_has_reset_expansion(group):
    """Return True when a later pullback is too large to belong to the same VCP."""
    if len(group) < 2:
        return False
    max_ratio = CONTRACTION_CFG.get("max_expansion_ratio")
    min_reset_pct = CONTRACTION_CFG.get("min_expansion_reset_pct", 0)
    if not max_ratio:
        return False
    for prev, cur in zip(group, group[1:]):
        prev_abs = abs(safe_float(prev.get("pullback_pct"), 0))
        cur_abs = abs(safe_float(cur.get("pullback_pct"), 0))
        if prev_abs <= 0:
            continue
        if cur_abs > prev_abs * max_ratio and cur_abs - prev_abs >= min_reset_pct:
            return True
    return False


def _check_bottom_lifting(contractions, threshold_pct=0.0):
    """检查收缩轮次的低点是否在收敛（底部不再创新低）。

    只看最后两轮低点的方向：末轮低点不低于前一轮低点，
    说明卖压已不再推动价格下行，处于筑底或收敛状态。

    Returns True if the bottom is stabilizing (last two lows not declining).
    """
    if not contractions or len(contractions) < 2:
        return True
    if len(contractions) >= 3:
        # 有 3 轮以上：看末两轮低点是否停止下移
        prev_low = contractions[-2]["low_price"]
        last_low = contractions[-1]["low_price"]
        return last_low >= prev_low * (1 + threshold_pct / 100)
    # 只有 2 轮：简单的末轮不低于首轮
    first_low = contractions[0]["low_price"]
    last_low = contractions[-1]["low_price"]
    return last_low >= first_low * (1 + threshold_pct / 100)


def evaluate_vcp_group(df, group):
    latest = df.iloc[-1]
    close = latest["close"]
    structure_pivot = max(c["high_price"] for c in group)
    market_pivot = safe_float(latest.get("high_60"))
    last_low = group[-1]["low_price"]
    last_end_idx = group[-1]["end_idx"]
    structure_age_days = len(df) - last_end_idx - 1
    pivot_distance = (close - structure_pivot) / structure_pivot * 100 if structure_pivot else None

    after = df.iloc[last_end_idx + 1:] if last_end_idx + 1 < len(df) else df.iloc[-1:]
    post_high = safe_float(after["high"].max(), close) if not after.empty else close
    post_structure_gain = (post_high - structure_pivot) / structure_pivot * 100 if structure_pivot else 0
    post_structure_drawdown = (close - post_high) / post_high * 100 if post_high else 0

    invalid_reasons = []
    if structure_age_days > VCP_MAX_STRUCTURE_AGE_DAYS:
        invalid_reasons.append("structure_too_old")
    if pivot_distance is not None and pivot_distance < VCP_MIN_PIVOT_DISTANCE:
        invalid_reasons.append("far_below_structure_pivot")
    if post_structure_gain > VCP_MAX_POST_GAIN:
        invalid_reasons.append("post_structure_extended")
    if post_structure_drawdown < VCP_MAX_POST_DRAWDOWN:
        invalid_reasons.append("post_structure_drawdown")

    return {
        "group": group,
        "structure_pivot": structure_pivot,
        "market_pivot": market_pivot,
        "pivot_price": structure_pivot,
        "pivot_distance": pivot_distance,
        "last_contraction_low": last_low,
        "structure_age_days": structure_age_days,
        "structure_valid": not invalid_reasons,
        "structure_invalid_reason": ";".join(invalid_reasons),
        "post_structure_gain": post_structure_gain,
        "post_structure_drawdown": post_structure_drawdown,
    }


def select_current_vcp_group(df, contractions):
    """Select the most recent contraction group that is still relevant now."""
    if not contractions:
        return None, None

    latest = df.iloc[-1]
    candidates = []
    best_invalid = None
    max_size = min(CONTRACTION_CFG["max_recent_contractions"], len(contractions))
    for size in range(max_size, 0, -1):
        for end in range(len(contractions), size - 1, -1):
            group = contractions[end - size:end]
            if contraction_group_has_reset_expansion(group):
                continue
            info = evaluate_vcp_group(df, group)
            if info["structure_valid"]:
                decrease = contraction_decrease_status(group)
                volume_pattern = volume_pattern_for_contractions(group, latest)
                pivot_distance = safe_float(info.get("pivot_distance"), -99)
                structure_age_days = safe_float(info.get("structure_age_days"), 999)
                score = 0
                if len(group) >= 2:
                    score += 30
                score += len(group) * 4
                if decrease["is_strict"]:
                    score += 60
                elif decrease["is_near"]:
                    score += 35
                if volume_pattern == "decreasing":
                    score += 25
                elif volume_pattern == "drying":
                    score += 12
                if pivot_distance >= BASE_CFG["near_pivot_distance_pct"]:
                    score += 15
                if pivot_distance >= STAGE_CFG["tight_min_pivot_distance_pct"]:
                    score += 10
                score -= structure_age_days * 0.5
                # Prefer current groups, but do not let a nearby noisy pullback
                # override a cleaner VCP sequence.
                score += group[-1]["end_idx"] * 0.01
                candidates.append((score, info))
                continue
            if best_invalid is None or info["structure_age_days"] < best_invalid["structure_age_days"]:
                best_invalid = info

    if candidates:
        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1], None
    return None, best_invalid


def detect_vcp_structure(df):
    latest = df.iloc[-1]
    n = len(df)
    conditions = []
    misses = []

    empty = {
        "has_structure": False,
        "state": "DATA_INSUFFICIENT",
        "conditions": [],
        "misses": [f"有效交易日<{BASE_CFG['min_data_days']}"],
        "structure_count": 0,
        "contractions": [],
        "contraction_count": 0,
        "contraction_pcts": "",
        "contraction_days": "",
        "volume_pattern": "unknown",
        "pivot_price": None,
        "structure_pivot": None,
        "market_pivot": None,
        "pivot_distance": None,
        "last_contraction_low": None,
        "structure_age_days": None,
        "structure_valid": False,
        "structure_invalid_reason": "",
        "post_structure_gain": None,
        "post_structure_drawdown": None,
        "vcp_quality": "D",
        "watch_priority": "none",
    }
    if n < BASE_CFG["min_data_days"]:
        return empty

    close = latest["close"]
    ma20 = latest.get("MA20")
    ma60 = latest.get("MA60")

    base_ok = True
    if pd.isna(ma60) or not (close > ma60 or (pd.notna(ma20) and ma20 >= ma60)):
        base_ok = False
        misses.append("趋势基础不足")
    if safe_float(latest.get("MA60_slope"), 0) < BASE_CFG["min_ma60_slope"]:
        base_ok = False
        misses.append("MA60斜率偏弱")
    if safe_float(latest.get("drawdown_high_120"), 0) < BASE_CFG["max_drawdown_high_120_pct"]:
        base_ok = False
        misses.append("近120日回撤过深")

    contractions = detect_contractions(df)
    current, invalid_group = select_current_vcp_group(df, contractions)
    group = current["group"] if current else []
    recent = group[-CONTRACTION_CFG["max_recent_contractions"]:]
    count = len(group)
    decrease = contraction_decrease_status(group)
    volume_pattern = volume_pattern_for_contractions(group, latest)
    pivot_price = current["pivot_price"] if current else None
    structure_pivot = current["structure_pivot"] if current else None
    market_pivot = current["market_pivot"] if current else safe_float(latest.get("high_60"))
    pivot_distance = current["pivot_distance"] if current else None
    last_low = current["last_contraction_low"] if current else None
    structure_age_days = current["structure_age_days"] if current else None
    structure_valid = bool(current)
    structure_invalid_reason = ""
    post_structure_gain = current["post_structure_gain"] if current else None
    post_structure_drawdown = current["post_structure_drawdown"] if current else None

    if not current and invalid_group:
        structure_pivot = invalid_group["structure_pivot"]
        market_pivot = invalid_group["market_pivot"]
        pivot_price = invalid_group["pivot_price"]
        pivot_distance = invalid_group["pivot_distance"]
        last_low = invalid_group["last_contraction_low"]
        structure_age_days = invalid_group["structure_age_days"]
        structure_invalid_reason = invalid_group["structure_invalid_reason"]
        post_structure_gain = invalid_group["post_structure_gain"]
        post_structure_drawdown = invalid_group["post_structure_drawdown"]

    if contractions:
        conditions.append(f"历史扫描共{len(contractions)}轮收缩")
    if count:
        conditions.append(f"{min(count, CONTRACTION_CFG['max_recent_contractions'])}轮有效收缩")
    else:
        misses.append("未识别当前有效收缩轮次")
        if structure_invalid_reason:
            misses.append(structure_invalid_reason)
    if decrease["is_strict"]:
        conditions.append("收缩幅度明显递减")
    elif decrease["is_near"]:
        conditions.append("收缩幅度接近递减")
    elif count >= 2:
        misses.append("收缩幅度未递减")
    if volume_pattern in {"decreasing", "drying"}:
        conditions.append(f"量能{volume_pattern}")
    else:
        misses.append(f"量能{volume_pattern}")
    if pivot_distance is not None and pivot_distance >= BASE_CFG["near_pivot_distance_pct"]:
        conditions.append("接近pivot")
    else:
        misses.append("距离pivot偏远")
    if last_low is not None and close > last_low * (1 + BASE_CFG["last_low_buffer_pct"] / 100):
        conditions.append("最近收缩低点守住")

    state = "REJECT"
    has_structure = False
    if base_ok and current and count >= STAGE_CFG["early_min_contractions"]:
        has_structure = True
        if count >= STAGE_CFG["mature_min_contractions"] and decrease["is_strict"]:
            state = "VCP_MATURE"
        elif count >= STAGE_CFG["forming_min_contractions"] and decrease["is_near"]:
            state = "VCP_FORMING"
        else:
            state = "VCP_EARLY"

        last_abs = abs(recent[-1]["pullback_pct"]) if recent else 99
        if (
            state == "VCP_MATURE"
            and last_abs <= STAGE_CFG["tight_max_last_pullback_pct"]
            and pivot_distance is not None
            and pivot_distance >= STAGE_CFG["tight_min_pivot_distance_pct"]
            and volume_pattern in {"decreasing", "drying"}
        ):
            state = "VCP_TIGHT"

        # ── 趋势背景过滤器：VCP 要求上升趋势背景 ──
        if has_structure and pd.notna(latest.get("MA120")) and latest["close"] < latest["MA120"]:
            trend_cfg = RISK_CFG.get("trend_background", {})
            if _check_bottom_lifting(recent, trend_cfg.get("bottom_lift_pct", 0.0)):
                # 底部在收敛：降级但不拒绝
                downgrade = {"VCP_MATURE": "VCP_FORMING", "VCP_FORMING": "VCP_EARLY"}
                old_state = state
                if state in downgrade:
                    state = downgrade[state]
                    misses.append(f"趋势背景存疑(低于MA120)，{old_state}→{state}")
                else:
                    misses.append("趋势背景存疑(低于MA120)")
            elif trend_cfg.get("strict_reject_no_lift", True):
                # 低点持续下移：趋势不成立，硬拒绝
                state = "REJECT"
                has_structure = False
                misses.append("趋势背景不成立(低于MA120且低点未收敛)")
    elif (
        base_ok
        and safe_float(latest.get("distance_ma20"), 0) > BASE_CFG["trend_watch_min_distance_ma20_pct"]
        and safe_float(latest.get("distance_high_60"), -99) > BASE_CFG["trend_watch_min_distance_high_60_pct"]
    ):
        state = "TREND_WATCH"
        conditions.append("强趋势但未形成收缩轮次")
    elif base_ok and structure_invalid_reason:
        if "post_structure_extended" in structure_invalid_reason:
            state = "POST_BREAKOUT"
        elif "post_structure_drawdown" in structure_invalid_reason:
            state = "TREND_REBUILD"

    quality_map = {
        "VCP_TIGHT": "A",
        "VCP_MATURE": "A" if volume_pattern in {"decreasing", "drying"} else "B",
        "VCP_FORMING": "B",
        "VCP_EARLY": "C",
        "TREND_WATCH": "D",
    }
    priority_map = {
        "VCP_TIGHT": "high",
        "VCP_MATURE": "high",
        "VCP_FORMING": "medium",
        "VCP_EARLY": "low",
        "TREND_WATCH": "low",
    }

    return {
        "has_structure": has_structure,
        "state": state,
        "conditions": conditions,
        "misses": misses,
        "structure_count": min(count, CONTRACTION_CFG["max_recent_contractions"]),
        "contractions": contractions,
        "contraction_group": group,
        "contraction_count": count,
        "contraction_pcts": " -> ".join(f'{c["pullback_pct"]:.2f}%' for c in recent),
        "contraction_days": " -> ".join(str(c["duration_days"]) for c in recent),
        "volume_pattern": volume_pattern,
        "pivot_price": pivot_price,
        "structure_pivot": structure_pivot,
        "market_pivot": market_pivot,
        "pivot_distance": pivot_distance,
        "last_contraction_low": last_low,
        "structure_age_days": structure_age_days,
        "structure_valid": structure_valid,
        "structure_invalid_reason": structure_invalid_reason,
        "post_structure_gain": post_structure_gain,
        "post_structure_drawdown": post_structure_drawdown,
        "vcp_quality": quality_map.get(state, "D"),
        "watch_priority": priority_map.get(state, "none"),
    }


def pullback_volume_confirmed(df, structure, cfg):
    latest = df.iloc[-1]
    if safe_float(latest.get("volume_dry_up"), 999) < cfg["volume_dry_up_lt"]:
        return True, "fixed_window"

    allowed_patterns = set(cfg.get("segment_volume_patterns", []))
    if structure.get("volume_pattern") not in allowed_patterns:
        return False, "insufficient_dry_up"

    group = structure.get("contraction_group") or []
    if not group:
        return False, "no_contraction_group"

    last_avg_volume = safe_float(group[-1].get("avg_volume"))
    if not last_avg_volume or last_avg_volume <= 0:
        return False, "no_segment_volume"

    days = max(1, int(cfg.get("current_low_volume_days", 3)))
    recent_avg_volume = safe_float(df["volume"].tail(days).mean())
    if recent_avg_volume is None:
        return False, "no_recent_volume"

    if recent_avg_volume <= last_avg_volume * cfg.get("current_low_volume_ratio", 1.0):
        return True, "segment_low_volume"
    return False, "recent_volume_not_low"


def setup_quality(score):
    if score >= SETUP_QUALITY_THRESHOLDS.get("A", 80):
        return "A"
    if score >= SETUP_QUALITY_THRESHOLDS.get("B", 70):
        return "B"
    if score >= SETUP_QUALITY_THRESHOLDS.get("C", 60):
        return "C"
    return "D"


def setup_hard_reject(overheat, cfg):
    hard_flags = set(cfg.get("hard_risk_flags", []))
    return any(flag in overheat["risk_flags"] for flag in hard_flags)


def recent_two_contractions_decreasing(structure):
    group = structure.get("contraction_group") or []
    if len(group) < 2:
        return False
    prev_abs = abs(group[-2]["pullback_pct"])
    cur_abs = abs(group[-1]["pullback_pct"])
    return cur_abs <= prev_abs * CONTRACTION_CFG["near_decrease_ratio"]


def clamp(value, lower, upper):
    return max(lower, min(upper, value))


def candle_metrics(row):
    high = safe_float(row.get("high"), 0)
    low = safe_float(row.get("low"), 0)
    open_ = safe_float(row.get("open"), 0)
    close = safe_float(row.get("close"), 0)
    rng = high - low
    if rng <= 0:
        return {"close_position": 0, "body_ratio": 0, "upper_shadow_ratio": 1}
    return {
        "close_position": (close - low) / rng,
        "body_ratio": abs(close - open_) / rng,
        "upper_shadow_ratio": (high - max(open_, close)) / rng,
    }


def is_strong_close(row, close_position_min=0.65, upper_shadow_max=0.30, body_ratio_min=None):
    metrics = candle_metrics(row)
    if metrics["close_position"] < close_position_min:
        return False
    if metrics["upper_shadow_ratio"] > upper_shadow_max:
        return False
    if body_ratio_min is not None and metrics["body_ratio"] < body_ratio_min:
        return False
    return True


def setup_risk_adjustment(overheat):
    cfg = SETUP_SCORING_CFG.get("risk_adjust", {})
    flags = overheat.get("risk_flags", [])
    if not flags:
        return cfg.get("no_risk", 0), ["无风险+10"], []

    flag_scores = cfg.get("flags", {})
    total = 0
    misses = []
    for flag in flags:
        if flag not in flag_scores:
            continue
        value = flag_scores[flag]
        total += value
        misses.append(f"{flag}{value}")
    total = clamp(total, cfg.get("min", -20), cfg.get("max", 10))
    return total, [], misses


def finalize_setup_score(setup_signal, action_quality_score, action_reasons, action_misses, structure, overheat):
    """Build final buy-point score from structure stage, action quality, and risk flags."""
    stage_cfg = SETUP_SCORING_CFG.get("stage_base", {})
    type_cfg = SETUP_SCORING_CFG.get("action_type_base", {})
    stage_base = stage_cfg.get(structure.get("state"), 0)
    type_base = type_cfg.get(setup_signal, 0)
    action_quality_score = clamp(int(round(action_quality_score)), 0, 15)
    setup_pattern_score = type_base + action_quality_score
    risk_adjust, risk_reasons, risk_misses = setup_risk_adjustment(overheat)
    final_score = stage_base + type_base + action_quality_score + risk_adjust
    final_score = clamp(int(round(final_score)), 0, 100)

    reasons = [
        f"结构基础{stage_base}",
        f"{setup_signal}类型+{type_base}",
        f"动作质量+{action_quality_score}",
    ]
    reasons.extend(action_reasons or [])
    reasons.extend(risk_reasons)

    misses = list(action_misses or [])
    misses.extend(risk_misses)
    if stage_base <= 0:
        misses.append("结构阶段无买点基础")

    return final_score, setup_pattern_score, reasons, misses


def base_setup_result(hit=False, reason="", score=0, reasons=None, misses=None, pattern_score=None, **kwargs):
    setup_score = max(0, min(100, int(round(score))))
    setup_pattern_score = setup_score if pattern_score is None else max(0, min(100, int(round(pattern_score))))
    result = {
        "hit": hit,
        "reason": reason,
        "setup_pattern_score": setup_pattern_score,
        "setup_score": setup_score,
        "setup_quality": setup_quality(setup_score),
        "setup_reasons": reasons or [],
        "setup_misses": misses or [],
    }
    result.update(kwargs)
    return result


def score_pullback_setup(df, structure, volume_ok, volume_reason):
    latest = df.iloc[-1]
    score = 0
    reasons = []
    misses = []

    dist20 = safe_float(latest.get("distance_ma20"), 999)
    dist60 = safe_float(latest.get("distance_ma60"), 999)
    if 0 <= dist20 <= 2:
        score += 5
        reasons.append("MA20上方贴近")
    elif -3 <= dist20 < 0 or 2 < dist20 <= 3:
        score += 2
        reasons.append("MA20近距回踩")
    elif -3 <= dist60 <= 3:
        score += 2
        reasons.append("靠近MA60")
    else:
        misses.append("回踩位置一般")

    vdu = safe_float(latest.get("volume_dry_up"), 999)
    if volume_reason == "fixed_window":
        score += 5
        reasons.append("固定窗口极度缩量")
    elif volume_reason == "segment_low_volume":
        score += 4
        reasons.append("收缩段低量确认")
    elif vdu < 0.9:
        score += 2
        reasons.append("近期缩量")
    else:
        misses.append("缩量质量不足")

    last_low = safe_float(structure.get("last_contraction_low"))
    ma20_slope = safe_float(latest.get("MA20_slope"), 0)
    if last_low and latest["close"] > last_low * 1.05 and ma20_slope >= 0:
        score += 5
        reasons.append("前低上方确认强")
    elif last_low and latest["close"] > last_low * 1.02 and ma20_slope >= -0.03:
        score += 2
        reasons.append("前低守住")
    else:
        misses.append("前低确认不足")

    return score, reasons, misses


def score_breakout_setup(df, structure, pivot):
    latest = df.iloc[-1]
    score = 0
    reasons = []
    misses = []

    close_ratio = latest["close"] / pivot if pivot else 0
    if 1.02 <= close_ratio <= 1.05:
        score += 5
        reasons.append("突破幅度高质量")
    elif 1.01 <= close_ratio < 1.02 or 1.05 < close_ratio <= 1.08:
        score += 2
        reasons.append("有效站上pivot")
    else:
        misses.append("突破幅度一般")

    volume = safe_float(latest.get("volume"), 0)
    vol_ma20 = safe_float(latest.get("vol_ma20"))
    if vol_ma20 and vol_ma20 > 0:
        vol_ratio = volume / vol_ma20
        if vol_ratio > 1.5:
            score += 5
            reasons.append("突破量能明显恢复")
        elif vol_ratio > 1.2:
            score += 3
            reasons.append("突破量能恢复")
        elif vol_ratio > 1.0:
            score += 1
            reasons.append("突破量能合格")
        else:
            misses.append("突破量能不足")
    else:
        misses.append("缺少vol_ma20")

    if is_strong_close(latest, close_position_min=0.75, upper_shadow_max=0.25, body_ratio_min=0.45):
        score += 5
        reasons.append("突破K线强")
    elif latest["close"] > pivot and not is_long_upper_shadow(latest):
        score += 2
        reasons.append("突破K线合格")
    else:
        misses.append("突破K线一般")

    return score, reasons, misses


def score_retest_setup(df, breakout):
    latest = df.iloc[-1]
    cfg = SETUP_CFG["retest_buy"]
    score = 0
    reasons = []
    misses = []

    post = df.iloc[breakout["idx"] + 1:]
    pullback_low = safe_float(post["low"].min(), latest["low"])
    low_ratio = pullback_low / breakout["level"] if breakout["level"] else 0
    max_above = cfg.get("max_pullback_above_breakout_ratio", 1.02)
    if 0.99 <= low_ratio <= 1.003:
        score += 5
        reasons.append("回踩贴近突破位")
    elif cfg["max_pullback_below_breakout_ratio"] <= low_ratio < 0.99 or 1.003 < low_ratio <= max_above:
        score += 2
        reasons.append("回踩位置合格")
    else:
        misses.append("回踩位置一般")


    volume_days = min(cfg["volume_compare_days"], len(post))
    post_volume = safe_float(post["volume"].tail(volume_days).mean(), 0)
    breakout_volume = safe_float(df.iloc[breakout["idx"]]["volume"], 0)
    if breakout_volume > 0:
        vol_ratio = post_volume / breakout_volume
        if vol_ratio < 0.70:
            score += 5
            reasons.append("回踩明显缩量")
        elif vol_ratio < 0.85:
            score += 3
            reasons.append("回踩缩量")
        elif vol_ratio < 1.0:
            score += 1
            reasons.append("回踩量能合格")
        else:
            misses.append("回踩量能不足")
    else:
        misses.append("缺少突破日量能")

    close_ok = latest["close"] >= breakout["level"]
    ma10_ok = pd.notna(latest.get("MA10")) and latest["close"] >= latest["MA10"]
    if close_ok and is_strong_close(latest, close_position_min=0.65, upper_shadow_max=0.30):
        score += 5
        reasons.append("收回突破位且收盘强")
    elif close_ok or ma10_ok:
        score += 2
        reasons.append("重新确认合格")
    else:
        misses.append("重新确认不足")

    return score, reasons, misses


def detect_pullback_buy(df, structure, overheat):
    latest = df.iloc[-1]
    cfg = SETUP_CFG["pullback_buy"]
    if structure.get("state") not in set(cfg["allowed_stages"]):
        return base_setup_result(False, "VCP结构阶段不足")
    if setup_hard_reject(overheat, cfg):
        return base_setup_result(False, "风险硬排除")

    ma20_min, ma20_max = cfg["ma20_distance_range"]
    ma60_min, ma60_max = cfg["ma60_distance_range"]
    near_ma20 = pd.notna(latest.get("distance_ma20")) and ma20_min <= latest["distance_ma20"] <= ma20_max
    near_ma60 = pd.notna(latest.get("distance_ma60")) and ma60_min <= latest["distance_ma60"] <= ma60_max
    low20 = latest.get("low_20")
    last_low = structure.get("last_contraction_low")
    volume_ok, volume_reason = pullback_volume_confirmed(df, structure, cfg)
    volume_floor_ok = volume_ok or safe_float(latest.get("volume_dry_up"), 999) < 0.9
    hard_conditions = [
        near_ma20 or near_ma60,
        volume_floor_ok,
        last_low is not None and latest["close"] > last_low * (1 + cfg["last_low_buffer_pct"] / 100),
        safe_float(latest.get("MA20_slope"), 0) >= cfg["min_ma20_slope"],
        not any(flag in overheat["risk_flags"] for flag in cfg["blocked_risk_flags"]),
    ]
    action_quality_score, reasons, misses = score_pullback_setup(df, structure, volume_ok, volume_reason)
    score, pattern_score, reasons, misses = finalize_setup_score(
        "PULLBACK_BUY", action_quality_score, reasons, misses, structure, overheat
    )
    hit = all(hard_conditions) and score >= cfg["min_setup_score"]
    anchor = "MA20" if near_ma20 else "MA60" if near_ma60 else ""
    support = safe_float(latest.get(anchor)) if anchor else safe_float(latest.get("MA20"))
    invalid = None
    if support:
        invalid = min(support * cfg["invalid_support_ratio"], safe_float(low20, support) * cfg["invalid_low20_ratio"])
    if not (near_ma20 or near_ma60):
        misses.append("未靠近MA20/MA60")
    if not volume_floor_ok:
        misses.append("回踩缩量不足")
    if last_low is None or latest["close"] <= last_low * (1 + cfg["last_low_buffer_pct"] / 100):
        misses.append("最近收缩低点未守住")
    if safe_float(latest.get("MA20_slope"), 0) < cfg["min_ma20_slope"]:
        misses.append("MA20斜率偏弱")
    if any(flag in overheat["risk_flags"] for flag in cfg["blocked_risk_flags"]):
        misses.append("阻断风险标记")
    if not hit and score < cfg["min_setup_score"]:
        misses.append("买点质量分不足")
    return base_setup_result(
        hit,
        f"缩量回踩{anchor}" if hit else "缩量回踩触发条件不足",
        score,
        reasons,
        misses,
        pattern_score=pattern_score,
        anchor=anchor,
        support_price=support,
        invalid_price=invalid,
        volume_confirmation=volume_reason,
    )


def detect_breakout_buy(df, structure, overheat):
    latest = df.iloc[-1]
    cfg = SETUP_CFG["breakout_buy"]
    if setup_hard_reject(overheat, cfg):
        return base_setup_result(False, "风险硬排除")
    if not structure.get("structure_valid"):
        return base_setup_result(False, "无当前有效VCP结构")
    if structure.get("state") not in set(cfg["allowed_stages"]):
        return base_setup_result(False, "结构阶段未达到突破前提")

    pivot = safe_float(structure.get("structure_pivot"))
    if not pivot:
        return base_setup_result(False, "缺少结构pivot")

    vol_ma20 = safe_float(latest.get("vol_ma20"))
    vol_ma5 = safe_float(latest.get("vol_ma5"))
    volume = safe_float(latest.get("volume"), 0)
    volume_ok = False
    if vol_ma20 and vol_ma20 > 0 and volume > vol_ma20 * cfg["volume_ma20_ratio"]:
        volume_ok = True
    if vol_ma5 and vol_ma5 > 0 and volume > vol_ma5 * cfg["volume_ma5_ratio"]:
        volume_ok = True

    hard_conditions = [
        latest["close"] > pivot * cfg["close_buffer_ratio"],
        latest["close"] <= pivot * cfg.get("max_close_extension_ratio", 999),
        volume_ok,
        not is_long_upper_shadow(latest),
        not any(flag in overheat["risk_flags"] for flag in cfg["blocked_risk_flags"]),
    ]
    action_quality_score, reasons, misses = score_breakout_setup(df, structure, pivot)
    score, pattern_score, reasons, misses = finalize_setup_score(
        "BREAKOUT_BUY", action_quality_score, reasons, misses, structure, overheat
    )
    hit = all(hard_conditions) and score >= cfg["min_setup_score"]
    if latest["close"] <= pivot * cfg["close_buffer_ratio"]:
        misses.append("未有效站上pivot")
    if latest["close"] > pivot * cfg.get("max_close_extension_ratio", 999):
        misses.append("突破后涨幅已延伸")
    if not volume_ok:
        misses.append("突破量能不足")
    if is_long_upper_shadow(latest):
        misses.append("突破日长上影")
    if any(flag in overheat["risk_flags"] for flag in cfg["blocked_risk_flags"]):
        misses.append("阻断风险标记")
    if not hit and score < cfg["min_setup_score"]:
        misses.append("买点质量分不足")
    return base_setup_result(
        hit,
        "VCP枢轴突破" if hit else "枢轴突破触发条件不足",
        score,
        reasons,
        misses,
        pattern_score=pattern_score,
        breakout_level=pivot,
        support_price=pivot,
        invalid_price=pivot * cfg["invalid_support_ratio"],
    )


def find_recent_breakout(df, lookback=None):
    cfg = SETUP_CFG["retest_buy"]
    if lookback is None:
        lookback = cfg["lookback_days"]
    n = len(df)
    high_days = cfg["breakout_lookback_high_days"]
    start = max(high_days, n - lookback - 1)
    end = n - 1
    for idx in range(start, end):
        prev = df.iloc[:idx]
        if len(prev) < high_days:
            continue
        row = df.iloc[idx]
        level = prev["high"].tail(high_days).max()
        vol_ma20 = row.get("vol_ma20")
        if pd.isna(vol_ma20) or vol_ma20 <= 0:
            continue
        if (
            row["close"] > level * cfg["breakout_close_buffer_ratio"]
            and row["volume"] > vol_ma20 * cfg["breakout_volume_ratio"]
            and not is_long_upper_shadow(row)
        ):
            return {
                "idx": idx,
                "date": row["date"],
                "level": float(level),
                "volume": float(row["volume"]),
                "vol_ma20": float(vol_ma20),
            }
    return None


def detect_retest_buy(df, structure, overheat):
    latest = df.iloc[-1]
    cfg = SETUP_CFG["retest_buy"]
    if setup_hard_reject(overheat, cfg):
        return base_setup_result(False, "风险硬排除")
    if not structure.get("structure_valid"):
        return base_setup_result(False, "无当前有效VCP结构")
    if structure.get("state") not in set(cfg["allowed_stages"]):
        return base_setup_result(False, "结构阶段未达到突破回踩前提")
    breakout = find_recent_breakout(df, lookback=cfg["lookback_days"])
    if not breakout:
        return base_setup_result(False, "近期无有效突破")

    days_after = len(df) - breakout["idx"] - 1
    if days_after < 1 or days_after > cfg.get("max_allowed_days_after_breakout", cfg["max_days_after_breakout"]):
        return base_setup_result(
            False,
            "突破后天数不在允许范围",
            breakout_level=breakout["level"],
        )

    post = df.iloc[breakout["idx"] + 1:]
    pullback_low = post["low"].min()
    volume_days = min(cfg["volume_compare_days"], len(post))
    volume_ok = safe_float(post["volume"].tail(volume_days).mean(), 0) < safe_float(df.iloc[breakout["idx"]]["volume"], 0)
    close_ok = latest["close"] >= breakout["level"] or (pd.notna(latest.get("MA10")) and latest["close"] >= latest["MA10"])
    hard_conditions = [
        pullback_low >= breakout["level"] * cfg["max_pullback_below_breakout_ratio"],
        pullback_low <= breakout["level"] * cfg.get("max_pullback_above_breakout_ratio", 999),
        volume_ok,
        close_ok,
        not is_long_upper_shadow(latest),
        not any(flag in overheat["risk_flags"] for flag in cfg["blocked_risk_flags"]),
    ]
    action_quality_score, reasons, misses = score_retest_setup(df, breakout)
    score, pattern_score, reasons, misses = finalize_setup_score(
        "RETEST_BUY", action_quality_score, reasons, misses, structure, overheat
    )
    hit = all(hard_conditions) and score >= cfg["min_setup_score"]
    if pullback_low < breakout["level"] * cfg["max_pullback_below_breakout_ratio"]:
        misses.append("回踩有效跌破突破位")
    if (
        pullback_low > breakout["level"] * cfg.get("max_pullback_above_breakout_ratio", 999)
        and "未真正回踩突破位" not in misses
    ):
        misses.append("未真正回踩突破位")
    if not volume_ok:
        misses.append("回踩未缩量")
    if not close_ok:
        misses.append("未收回突破位或MA10")
    if is_long_upper_shadow(latest):
        misses.append("确认日长上影")
    if any(flag in overheat["risk_flags"] for flag in cfg["blocked_risk_flags"]):
        misses.append("阻断风险标记")
    if not hit and score < cfg["min_setup_score"]:
        misses.append("买点质量分不足")
    return base_setup_result(
        hit,
        "突破后缩量回踩确认" if hit else "突破回踩确认条件不足",
        score,
        reasons,
        misses,
        pattern_score=pattern_score,
        breakout_level=breakout["level"],
        support_price=breakout["level"],
        invalid_price=breakout["level"] * cfg["invalid_support_ratio"],
    )


def score_setup(df, structure, pullback, retest, overheat):
    latest = df.iloc[-1]

    stage = structure.get("state")
    stage_scores = SCORE_CFG["stage"]
    structure_score = stage_scores.get(stage, 0)

    volume_score = 0
    volume_cfg = SCORE_CFG["volume"]
    volume_pattern = structure.get("volume_pattern")
    if volume_pattern == "decreasing":
        volume_score += volume_cfg["decreasing"]
    elif volume_pattern == "drying":
        volume_score += volume_cfg["drying"]
    elif volume_pattern == "failed":
        volume_score += volume_cfg["failed"]
    if safe_float(latest.get("volume_dry_up"), 999) < volume_cfg["dry_up_bonus_threshold"]:
        volume_score += volume_cfg["dry_up_bonus"]
    if safe_float(latest.get("vol_ma20"), 999) < safe_float(latest.get("vol_ma60"), -999):
        volume_score += volume_cfg["vol_ma20_below_ma60_bonus"]

    trend_score = 0
    trend_cfg = SCORE_CFG["trend"]
    if pd.notna(latest.get("MA20")) and pd.notna(latest.get("MA60")) and latest["MA20"] >= latest["MA60"]:
        trend_score += trend_cfg["ma20_above_ma60_bonus"]
    if safe_float(latest.get("MA20_slope"), 0) >= trend_cfg["ma20_slope_min"]:
        trend_score += trend_cfg["ma20_slope_bonus"]
    if safe_float(latest.get("MA60_slope"), 0) >= trend_cfg["ma60_slope_min"]:
        trend_score += trend_cfg["ma60_slope_bonus"]

    position_score = 0
    position_cfg = SCORE_CFG["position"]
    dist20 = safe_float(latest.get("distance_ma20"), 999)
    pivot_distance = safe_float(structure.get("pivot_distance"), None)
    ma20_min, ma20_max = position_cfg["ma20_near_range"]
    if ma20_min <= dist20 <= ma20_max:
        position_score += position_cfg["ma20_near_bonus"]
    elif dist20 <= position_cfg["ma20_extended_max_pct"]:
        position_score += position_cfg["ma20_extended_bonus"]
    if pivot_distance is not None:
        pivot_near_min, pivot_near_max = position_cfg["pivot_near_range"]
        pivot_watch_min, pivot_watch_max = position_cfg["pivot_watch_range"]
        if pivot_near_min <= pivot_distance <= pivot_near_max:
            position_score += position_cfg["pivot_near_bonus"]
        elif pivot_watch_min <= pivot_distance <= pivot_watch_max:
            position_score += position_cfg["pivot_watch_bonus"]

    score = structure_score + volume_score + trend_score + position_score
    score = max(SCORE_CFG["min_score"], min(SCORE_CFG["max_score"], score))

    return {
        "structure_score": round(score, 2),
        "structure_risk_score": overheat["risk_score"],
        "components": {
            "structure": structure_score,
            "volume": volume_score,
            "trend": trend_score,
            "position": position_score,
        }
    }


def structure_stage_from_internal(state):
    stage_map = {
        "VCP_EARLY": "VCP_EARLY",
        "VCP_FORMING": "VCP_FORMING",
        "VCP_MATURE": "VCP_MATURE",
        "VCP_TIGHT": "VCP_TIGHT",
        "TREND_WATCH": "TREND_WATCH",
        "POST_BREAKOUT": "POST_BREAKOUT",
        "TREND_REBUILD": "TREND_REBUILD",
        "DATA_INSUFFICIENT": "DATA_ISSUE",
        "REJECT": "NONE",
    }
    return stage_map.get(state, "NONE")


def setup_position(setup_signal, setup_quality):
    if setup_signal == "PULLBACK_BUY":
        if setup_quality == "A":
            return CLASSIFICATION_CFG["pullback_buy_position"]
        if setup_quality == "B":
            return CLASSIFICATION_CFG.get("pullback_buy_position_b", CLASSIFICATION_CFG["pullback_buy_position"])
        return CLASSIFICATION_CFG.get("pullback_buy_position_c", CLASSIFICATION_CFG["no_position"])
    if setup_signal == "BREAKOUT_BUY":
        if setup_quality == "A":
            return CLASSIFICATION_CFG["breakout_buy_position"]
        if setup_quality == "B":
            return CLASSIFICATION_CFG.get("breakout_buy_position_b", CLASSIFICATION_CFG["breakout_buy_position"])
        return CLASSIFICATION_CFG.get("breakout_buy_position_c", CLASSIFICATION_CFG["no_position"])
    if setup_signal == "RETEST_BUY":
        if setup_quality == "A":
            return CLASSIFICATION_CFG["retest_buy_position"]
        if setup_quality == "B":
            return CLASSIFICATION_CFG.get("retest_buy_light_position", CLASSIFICATION_CFG["breakout_buy_position"])
        return CLASSIFICATION_CFG.get("retest_buy_position_c", CLASSIFICATION_CFG["no_position"])
    return CLASSIFICATION_CFG["no_position"]


def classify_result(structure, pullback, breakout, retest, score, overheat):
    internal_stage = structure.get("state")
    if retest.get("hit"):
        position = setup_position("RETEST_BUY", retest.get("setup_quality", "D"))
        return "VCP", "RETEST_BUY", "BUY_STANDARD", position
    if breakout.get("hit"):
        return "VCP", "BREAKOUT_BUY", "BUY_BREAKOUT", setup_position("BREAKOUT_BUY", breakout.get("setup_quality", "D"))
    if pullback.get("hit"):
        return "VCP", "PULLBACK_BUY", "BUY_LIGHT", setup_position("PULLBACK_BUY", pullback.get("setup_quality", "D"))
    if internal_stage in ["VCP_TIGHT", "VCP_MATURE", "VCP_FORMING", "VCP_EARLY"]:
        return "VCP", "NONE", "AVOID_CHASE" if overheat["hard_reject"] else "WATCH", CLASSIFICATION_CFG["no_position"]
    if internal_stage == "TREND_WATCH":
        return "TREND", "NONE", "AVOID_CHASE" if overheat["hard_reject"] else "WATCH", CLASSIFICATION_CFG["no_position"]
    if internal_stage == "POST_BREAKOUT":
        return "VCP", "NONE", "AVOID_CHASE", CLASSIFICATION_CFG["no_position"]
    if internal_stage == "TREND_REBUILD":
        return "VCP", "NONE", "WAIT_REBUILD", CLASSIFICATION_CFG["no_position"]
    return "NONE", "NONE", "REJECT", CLASSIFICATION_CFG["no_position"]


def build_reason(structure, pullback, breakout, retest, overheat):
    if retest.get("hit"):
        return retest["reason"]
    if breakout.get("hit"):
        return breakout["reason"]
    if pullback.get("hit"):
        return pullback["reason"]
    if structure.get("state") == "TREND_WATCH":
        return "趋势偏强但未形成有效收缩轮次"
    if structure.get("state") in {"POST_BREAKOUT", "TREND_REBUILD"}:
        return f"{structure.get('state')}：历史结构失效({structure.get('structure_invalid_reason')})"
    if structure.get("has_structure"):
        detail = structure.get("contraction_pcts") or "无"
        return f"{structure.get('state')}：收缩 {detail}，量能 {structure.get('volume_pattern')}"
    if overheat["risk_flags"]:
        return "风险排除：" + "、".join(overheat["risk_flags"])
    return "未形成有效VCP结构"


def choose_setup_detail(setup_signal, pullback, breakout, retest):
    if setup_signal == "RETEST_BUY":
        return retest
    if setup_signal == "BREAKOUT_BUY":
        return breakout
    if setup_signal == "PULLBACK_BUY":
        return pullback
    return base_setup_result(False, "无买点触发")


def screen(df):
    """确定性识别 VCP 结构阶段和触发信号，返回结构化结果。"""
    latest = df.iloc[-1]
    if pd.isna(latest.get("MA20")):
        return {
            "structure_type": "DATA_ISSUE",
            "structure_stage": "DATA_ISSUE",
            "setup_signal": "NONE",
            "action_hint": "DATA_SKIP",
            "suggested_position": "0",
            "model2_include": False,
            "structure_score": 0,
            "structure_risk_score": 0,
            "setup_pattern_score": 0,
            "setup_score": 0,
            "setup_quality": "D",
            "setup_reasons": [],
            "setup_misses": ["数据不足"],
            "support_price": None, "invalid_price": None, "breakout_level": None,
            "reason": f"MA20=NaN，仅{len(df)}个有效交易日", "structure_risk_flags": [],
            "score_components": {},
            "structure_conditions": [],
            "structure_misses": ["数据不足"],
            "contraction_count": 0,
            "contraction_pcts": "",
            "contraction_days": "",
            "volume_pattern": "unknown",
            "pivot_price": None,
            "structure_pivot": None,
            "market_pivot": None,
            "pivot_distance": None,
            "last_contraction_low": None,
            "structure_age_days": None,
            "structure_valid": False,
            "structure_invalid_reason": "data_issue",
            "post_structure_gain": None,
            "post_structure_drawdown": None,
            "vcp_quality": "D",
            "contractions": [],
            "contraction_group": [],
        }

    overheat = detect_overheat(df)
    structure = detect_vcp_structure(df)
    score = score_setup(df, structure, {}, {}, overheat)
    structure["structure_score_estimate"] = score["structure_score"]
    pullback = detect_pullback_buy(df, structure, overheat)
    breakout = detect_breakout_buy(df, structure, overheat)
    retest = detect_retest_buy(df, structure, overheat)
    structure_type, setup_signal, action_hint, suggested_position = classify_result(structure, pullback, breakout, retest, score, overheat)
    structure_stage = structure_stage_from_internal(structure.get("state"))
    setup_detail = choose_setup_detail(setup_signal, pullback, breakout, retest)

    support = retest.get("support_price") or breakout.get("support_price") or pullback.get("support_price")
    invalid = retest.get("invalid_price") or breakout.get("invalid_price") or pullback.get("invalid_price")
    breakout_level = retest.get("breakout_level") or breakout.get("breakout_level")

    final_quality = structure.get("vcp_quality", "D")
    if setup_signal in {"PULLBACK_BUY", "BREAKOUT_BUY", "RETEST_BUY"}:
        final_quality = setup_detail.get("setup_quality", "D")
    model2_include = structure_type in {"VCP", "TREND"} and action_hint != "REJECT"

    return {
        "structure_type": structure_type,
        "structure_stage": structure_stage,
        "setup_signal": setup_signal,
        "action_hint": action_hint,
        "suggested_position": suggested_position,
        "model2_include": model2_include,
        "structure_score": score["structure_score"],
        "structure_risk_score": score["structure_risk_score"],
        "setup_pattern_score": setup_detail.get("setup_pattern_score", setup_detail.get("setup_score", 0)),
        "setup_score": setup_detail.get("setup_score", 0),
        "setup_quality": setup_detail.get("setup_quality", "D"),
        "setup_reasons": setup_detail.get("setup_reasons", []),
        "setup_misses": setup_detail.get("setup_misses", []),
        "support_price": round_or_none(support),
        "invalid_price": round_or_none(invalid),
        "breakout_level": round_or_none(breakout_level),
        "reason": build_reason(structure, pullback, breakout, retest, overheat),
        "structure_risk_flags": overheat["risk_flags"],
        "score_components": score["components"],
        "structure_conditions": structure.get("conditions", []),
        "structure_misses": structure.get("misses", []),
        "contraction_count": structure.get("contraction_count", 0),
        "contraction_pcts": structure.get("contraction_pcts", ""),
        "contraction_days": structure.get("contraction_days", ""),
        "volume_pattern": structure.get("volume_pattern", "unknown"),
        "pivot_price": round_or_none(structure.get("pivot_price")),
        "structure_pivot": round_or_none(structure.get("structure_pivot")),
        "market_pivot": round_or_none(structure.get("market_pivot")),
        "pivot_distance": round_or_none(structure.get("pivot_distance")),
        "last_contraction_low": round_or_none(structure.get("last_contraction_low")),
        "structure_age_days": structure.get("structure_age_days"),
        "structure_valid": structure.get("structure_valid", False),
        "structure_invalid_reason": structure.get("structure_invalid_reason", ""),
        "post_structure_gain": round_or_none(structure.get("post_structure_gain")),
        "post_structure_drawdown": round_or_none(structure.get("post_structure_drawdown")),
        "vcp_quality": final_quality,
        "contractions": structure.get("contractions", []),
        "contraction_group": structure.get("contraction_group", []),
    }


# ===================== 输出 =====================

CSV_COLUMNS = [
    "股票代码", "股票名称", "structure_type", "structure_stage", "setup_signal",
    "action_hint", "suggested_position", "model2_include", "structure_score", "structure_risk_score",
    "setup_pattern_score", "setup_score", "setup_quality", "setup_reasons", "setup_misses",
    "structure_risk_flags", "support_price", "invalid_price", "breakout_level",
    "contraction_count", "contraction_pcts", "contraction_days", "volume_pattern",
    "pivot_price", "structure_pivot", "market_pivot", "pivot_distance", "last_contraction_low",
    "structure_age_days", "structure_valid", "structure_invalid_reason",
    "post_structure_gain", "post_structure_drawdown", "vcp_quality",
    "close", "MA20", "MA60", "MA120", "MA20_slope", "MA60_slope",
    "range_10", "range_20", "range_60", "volume_dry_up",
    "distance_ma20", "distance_ma60", "distance_high_60",
    "chg_5", "chg_20", "reason", "run_date", "strategy_version",
]


def result_from_df(code, name, df, run_date):
    latest = df.iloc[-1]
    decision = screen(df)
    result = {
        "code": code,
        "name": name,
        "run_date": run_date,
        "strategy_version": STRATEGY_VERSION,
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
        "distance_ma120": round_or_none(latest.get("distance_ma120")),
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
    return bool(result.get("model2_include"))


def write_csv(results, quant_path):
    os.makedirs(os.path.dirname(quant_path), exist_ok=True)
    with open(quant_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_COLUMNS)
        for r in results:
            writer.writerow([
                f'="{r["code"]}"',
                r["name"],
                r["structure_type"],
                r["structure_stage"],
                r["setup_signal"],
                r["action_hint"],
                r["suggested_position"],
                r["model2_include"],
                r["structure_score"],
                r["structure_risk_score"],
                r["setup_pattern_score"],
                r["setup_score"],
                r["setup_quality"],
                ";".join(r["setup_reasons"]),
                ";".join(r["setup_misses"]),
                ";".join(r["structure_risk_flags"]),
                r["support_price"] if r["support_price"] is not None else "",
                r["invalid_price"] if r["invalid_price"] is not None else "",
                r["breakout_level"] if r["breakout_level"] is not None else "",
                r["contraction_count"],
                r["contraction_pcts"],
                r["contraction_days"],
                r["volume_pattern"],
                r["pivot_price"] if r["pivot_price"] is not None else "",
                r["structure_pivot"] if r["structure_pivot"] is not None else "",
                r["market_pivot"] if r["market_pivot"] is not None else "",
                r["pivot_distance"] if r["pivot_distance"] is not None else "",
                r["last_contraction_low"] if r["last_contraction_low"] is not None else "",
                r["structure_age_days"] if r["structure_age_days"] is not None else "",
                r["structure_valid"],
                r["structure_invalid_reason"],
                r["post_structure_gain"] if r["post_structure_gain"] is not None else "",
                r["post_structure_drawdown"] if r["post_structure_drawdown"] is not None else "",
                r["vcp_quality"],
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
                r["run_date"],
                r["strategy_version"],
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
    print(f"结构: {result['structure_type']} | {result['structure_stage']} | 分数 {result['structure_score']} / 风险 {result['structure_risk_score']}")
    print(f"触发: {result['setup_signal']} | model2_include={result['model2_include']}")
    print(f"买点质量: {result['setup_score']} / {result['setup_quality']} | 动作分 {result['setup_pattern_score']} | 加分 {result['setup_reasons']} | 扣分 {result['setup_misses']}")
    print(f"动作: {result['action_hint']} | 建议仓位 {result['suggested_position']}")
    print(f"VCP: {result['structure_stage']} | 轮次 {result['contraction_count']} | 收缩 {result['contraction_pcts']} | 量能 {result['volume_pattern']}")
    print(f"Pivot: {result['pivot_price']} | 距pivot {result['pivot_distance']}% | 年龄 {result['structure_age_days']}天 | 有效 {result['structure_valid']}")
    if result["structure_invalid_reason"]:
        print(f"结构失效: {result['structure_invalid_reason']} | 结构后涨幅 {result['post_structure_gain']}% | 回撤 {result['post_structure_drawdown']}%")
    print(f"质量 {result['vcp_quality']}")
    print(f"收盘: {result['close']} | MA20 {result['MA20']} | MA60 {result['MA60']}")
    print(f"距MA20: {result['distance_ma20']}% | 距MA60: {result['distance_ma60']}% | 距60日高点: {result['distance_high_60']}%")
    print(f"支撑: {result['support_price']} | 失效: {result['invalid_price']} | 突破位: {result['breakout_level']}")
    print(f"结论: {result['reason']}")
    if result["structure_risk_flags"]:
        print(f"风险: {', '.join(result['structure_risk_flags'])}")


def count_data_source(stats, source):
    if source.startswith("cache"):
        stats["cache_hits"] += 1
    elif source == "tdx":
        stats["tdx_calls"] += 1
    else:
        stats["api_calls"] += 1


def print_summary(total, pull_ok, pull_fail, data_insufficient, results, cache_hits=0, api_calls=0, tdx_calls=0):
    print()
    print("=" * 70)
    print("模型二执行摘要 — VCP 结构与触发信号精筛")
    print("=" * 70)
    print(f"  输入标的: {total} 只")
    print(f"  数据来源: 缓存命中 {cache_hits} 只 + 妙想API {api_calls} 只 + 通达信 {tdx_calls} 只")
    print(f"  成功拉取行情: {pull_ok} 只（失败: {pull_fail} 只）")
    print(f"  数据不足跳过: {data_insufficient} 只")

    by_signal = {}
    by_action = {}
    by_stage = {}
    by_quality = {}
    for r in results:
        by_signal[r["setup_signal"]] = by_signal.get(r["setup_signal"], 0) + 1
        by_action[r["action_hint"]] = by_action.get(r["action_hint"], 0) + 1
        by_stage[r.get("structure_stage", "unknown")] = by_stage.get(r.get("structure_stage", "unknown"), 0) + 1
        by_quality[r.get("vcp_quality", "D")] = by_quality.get(r.get("vcp_quality", "D"), 0) + 1
    print(f"  输出结果: {len(results)} 只")
    print(f"  结构阶段: {by_stage}")
    print(f"  交易触发: {by_signal}")
    print(f"  动作提示: {by_action}")
    print(f"  VCP质量: {by_quality}")

    top = sorted([r for r in results if r.get("model2_include")], key=lambda x: x["structure_score"], reverse=True)[:20]
    if top:
        print()
        print(f"{'代码':<8} {'名称':<8} {'阶段':<16} {'触发':<14} {'分数':>6} {'风险':>6} {'结论'}")
        print("-" * 100)
        for r in top:
            print(f"{r['code']:<8} {r['name']:<8} {r['structure_stage']:<16} {r['setup_signal']:<14} {r['structure_score']:>6.1f} {r['structure_risk_score']:>6.1f} {r['reason'][:36]}")


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

    selected = sorted(results, key=lambda x: x["structure_score"], reverse=True)[:top_n]
    system_prompt = (
        "你是量价形态复核助手。只解释脚本结果，不改变脚本对结构阶段和触发信号的判定。"
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
    elif mode == "多股":
        json_path = f"{QUANT_RUNS_DIR}/multi_{today_yy}.json"
    elif mode == "测试":
        json_path = f"{QUANT_RUNS_DIR}/quant_{today_yy}_test.json"
    else:
        json_path = f"{QUANT_RUNS_DIR}/quant_{today_yy}.json"
    return quant_path, json_path


def process_codes(codes, today_yy, run_date, use_cache=True, allow_retry=True):
    results = []
    stats = {
        "pull_ok": 0,
        "pull_fail": 0,
        "data_insufficient": 0,
        "cache_hits": 0,
        "api_calls": 0,
        "tdx_calls": 0,
    }
    fatal_stop = False
    retry_queue = []

    for i, (code, name) in enumerate(codes):
        if fatal_stop:
            print(f"[{i+1}/{len(codes)}] {code} {name} ... 因上游致命错误跳过")
            stats["pull_fail"] += 1
            continue

        print(f"[{i+1}/{len(codes)}] {code} {name} ...", end=" ", flush=True)
        df, source = fetch_daily(code, name, today_yy, use_cache=use_cache, as_of_date=run_date)
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

        count_data_source(stats, source)
        if len(df) < BASE_CFG["min_runtime_data_days"]:
            print(f"跳过: 数据不足({len(df)}天)")
            stats["data_insufficient"] += 1
            continue

        stats["pull_ok"] += 1
        df = calc_indicators(df)
        result = result_from_df(code, name, df, run_date)
        results.append(result)
        print(f"{result['structure_stage']} / {result['setup_signal']} | score={result['structure_score']} | {result['reason'][:48]}")

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

    parser = argparse.ArgumentParser(description="模型二：VCP 结构与触发信号精筛")
    parser.add_argument("--date", help="运行交易日，支持 YYMMDD 或 YYYY-MM-DD，默认取预期最近交易日")
    parser.add_argument("--pool", help="模型一池文件路径（默认取当天 pool/pool_YYMMDD.csv）")
    parser.add_argument("--code", help="单只股票代码")
    parser.add_argument("--codes", help="多只股票代码，逗号分隔")
    parser.add_argument("--name", help="单股模式下手工指定股票名称")
    parser.add_argument("--test", action="store_true", help="测试模式，使用内建标的")
    parser.add_argument("--quant", help="CSV 输出路径")
    parser.add_argument("--output-json", help="JSON 输出路径")
    parser.add_argument("--json", action="store_true", help="同时将结构化结果打印到 stdout")
    parser.add_argument("--include-reject", action="store_true", help="CSV 中包含未纳入 model2_include 的标的")
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存，重新拉取行情")
    parser.add_argument("--refresh", action="store_true", help="清除今日缓存后重新拉取")
    parser.add_argument("--with-llm", action="store_true", help="可选调用 LLM 对 top 标的做解释")
    parser.add_argument("--llm-top", type=int, default=10, help="LLM 解释 Top N，默认 10")
    args = parser.parse_args()

    try:
        trade_date = normalize_run_date(args.date)
    except ValueError as exc:
        print(f"错误: {exc}")
        sys.exit(1)
    today_yy = trade_date.replace("-", "")[2:]
    run_date = trade_date
    use_cache = not args.no_cache

    try:
        codes, mode = resolve_input_codes(args, today_yy)
    except FileNotFoundError as exc:
        print(f"错误: {exc}")
        print("可用 --pool 指定路径，或 --code/--codes 做单股分析。")
        sys.exit(1)

    quant_path, json_path = build_output_paths(args, today_yy, mode, codes)

    print("=" * 70)
    print("模型二：VCP 结构与触发信号精筛")
    print(f"模式: {mode} | 标的: {len(codes)} 只 | CSV: {quant_path}")
    cleanup_cache = DailyCache()
    print(f"缓存: {'关闭' if args.no_cache else '开启'} | 目录: {cleanup_cache.cache_dir}")
    print("=" * 70)

    cleaned = cleanup_cache.cleanup_old(keep_days=30)
    if cleaned:
        print(f"已清理 {cleaned} 个超过30天的旧缓存文件")
    if args.refresh:
        cleared = cleanup_cache.clear_today(today_yy)
        print(f"已清除今日缓存 {cleared} 个文件")

    results, stats = process_codes(codes, today_yy, run_date, use_cache=use_cache)
    csv_results = [r for r in results if should_write_to_quant(r, include_reject=args.include_reject)]
    csv_results.sort(key=lambda x: x["structure_score"], reverse=True)

    llm_payload = {"status": "skipped", "reason": "not_requested", "reviews": []}
    if args.with_llm:
        llm_payload = maybe_call_llm(csv_results, args.llm_top)

    payload = {
        "meta": {
            "run_date": run_date,
            "mode": mode,
            "total": len(codes),
            "csv_path": quant_path,
            "schema": "quant_vcp_structure_v2",
            "strategy_version": STRATEGY_VERSION,
            "strategy_file": f"strategies/{QUANT_STRATEGY_FILE}",
        },
        "stats": stats,
        "llm": llm_payload,
        "results": results,
    }

    print_summary(len(codes), stats["pull_ok"], stats["pull_fail"], stats["data_insufficient"],
                  results, stats["cache_hits"], stats["api_calls"], stats["tdx_calls"])

    if csv_results:
        write_csv(csv_results, quant_path)
    else:
        print("\n无 model2_include 标的，CSV 未写入。可用 --include-reject 输出完整结果。")
    write_json(payload, json_path)

    if mode == "单股" and results:
        print_single_summary(results[0])
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
