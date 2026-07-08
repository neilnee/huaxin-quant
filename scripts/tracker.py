#!/usr/bin/env python3
"""
tracker.py — 模型四 择时跟踪 配套脚本
对应指令: instructions/04-tracker.md

职责（脚本管数值，LLM 管判断）:
  1. 读取 core_pool.csv / positions.csv / valuation_ranking.csv
  2. 拉取核心池所有标的的日线数据（复用 cache/daily/ 缓存层）
  3. 计算扩展技术指标（MA5/10/20、MA20斜率、箱体检测、20日高低点等）
  4. 逐条预检 B1/B2/S0-S5/W1-W3 信号条件
  5. 输出结构化 JSON，供 LLM 引用后撰写信号报告

用法:
  python3 scripts/tracker.py                     # 默认：全量核心池
  python3 scripts/tracker.py --test              # 测试：只跑持仓标的
  python3 scripts/tracker.py --code 300442       # 单只更新
  python3 scripts/tracker.py --no-cache          # 跳过缓存，重新拉取行情
"""

import os, json, time, csv, sys, argparse
from datetime import datetime
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import DailyCache, PROJECT_ROOT, VALUATION_RANKING_PATH, fetch_daily

# ===================== 配置 =====================

SIGNALS_DIR = os.path.join(PROJECT_ROOT, "signals")
CORE_POOL_PATH = os.path.join(SIGNALS_DIR, "core_pool.csv")
POSITIONS_PATH = os.path.join(SIGNALS_DIR, "positions.csv")
BATCHES_PATH = os.path.join(SIGNALS_DIR, "batches.csv")
RANKING_PATH = VALUATION_RANKING_PATH

# ===================== 数据读取 =====================

def read_core_pool():
    """读取 core_pool.csv，返回 [{code, name, priority, has_valuation, status, entry_pattern, entry_ma}, ...]"""
    if not os.path.exists(CORE_POOL_PATH):
        return []
    stocks = []
    with open(CORE_POOL_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            stocks.append({
                "code": row["code"].replace('="', "").replace('"', ""),
                "name": row["name"],
                "first_added": row.get("first_added", ""),
                "last_quant_date": row.get("last_quant_date", ""),
                "consecutive_miss": int(row.get("consecutive_miss", 0) or 0),
                "priority": row.get("priority", "正常"),
                "has_valuation": row.get("has_valuation", "FALSE"),
                "status": row.get("status", "跟踪中"),
                "entry_pattern": row.get("entry_pattern", ""),
                "entry_ma": row.get("entry_ma", ""),
            })
    return stocks

def read_positions():
    """读取 positions.csv，返回 {code: {...}}"""
    if not os.path.exists(POSITIONS_PATH):
        return {}
    positions = {}
    with open(POSITIONS_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row["code"].replace('="', "").replace('"', "")
            positions[code] = {
                "name": row["name"],
                "asset_type": row.get("asset_type", "个股"),
                "target_type": row.get("target_type", "shares"),
                "target_value": float(row.get("target_value", 0)),
                "actual_shares": int(float(row.get("actual_shares", 0))),
                "cost_price": float(row.get("cost_price", 0)),
                "stop_loss_override": row.get("stop_loss_override", ""),
                "manual_tag": row.get("manual_tag", ""),
            }
    return positions


def read_batches():
    """读取 batches.csv → {code: [batch_list]}，仅 status=持有中 的活跃批次"""
    if not os.path.exists(BATCHES_PATH):
        return {}
    batches = {}
    with open(BATCHES_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row["code"].replace('="', "").replace('"', "")
            if code not in batches:
                batches[code] = []
            batches[code].append({
                "batch_id": row["batch_id"],
                "code": code,
                "name": row["name"],
                "entry_date": row.get("entry_date", ""),
                "entry_logic": row.get("entry_logic", ""),
                "entry_ma": row.get("entry_ma", ""),
                "shares": int(float(row.get("shares", 0))),
                "cost_price": float(row.get("cost_price", 0)),
                "stop_loss_override": row.get("stop_loss_override", ""),
            })
    return batches


def read_ranking():
    """读取 valuation_ranking.csv，返回 {code: {valuation fields}}"""
    if not os.path.exists(RANKING_PATH):
        return {}
    rankings = {}
    with open(RANKING_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row["股票代码"].replace('="', "").replace('"', "")
            rankings[code] = {
                "current_price": float(row.get("当前股价_元", 0)),
                "downside_risk": float(row.get("下行风险价_元", 0)),
                "conservative": float(row.get("悲观估值_元", 0)),
                "base": float(row.get("基准估值_元", 0)),
                "optimistic": float(row.get("乐观估值_元", 0)),
                "pe_base": float(row.get("隐含PE_基准估值_倍", 0)),
                "discount_rate": float(row.get("安全边际折扣率", 0)),
                "valuation_method": row.get("主估值方法", ""),
            }
    return rankings

# ===================== 指标计算 =====================

def calc_indicators(df):
    """计算扩展技术指标（比模型二多了 MA5/MA10/MA20斜率/箱体/高低点）"""
    df = df.copy()

    # 均线
    df["MA5"] = df["close"].rolling(5).mean()
    df["MA10"] = df["close"].rolling(10).mean()
    df["MA20"] = df["close"].rolling(20).mean()
    df["MA60"] = df["close"].rolling(60).mean()

    # 均量
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()

    # 量价指标
    df["量比"] = df["volume"] / df["vol_ma5"].shift(1)
    df["缩量程度"] = df["vol_ma5"] / df["vol_ma20"]
    df["距MA20"] = (df["close"] - df["MA20"]) / df["MA20"] * 100
    df["距MA60"] = (df["close"] - df["MA60"]) / df["MA60"] * 100
    df["近5日涨幅"] = df["close"].pct_change(5) * 100

    # 滚动高低点
    df["high20"] = df["high"].rolling(20).max()
    df["low20"] = df["low"].rolling(20).min()

    # 箱体指标
    df["range20"] = df["high20"] - df["low20"]
    df["avg20"] = df["close"].rolling(20).mean()
    df["range_ratio"] = df["range20"] / df["avg20"]  # 区间收窄度
    df["box_consolidation"] = df["range_ratio"] < 0.15
    df["box_breakout"] = df["close"] >= df["high20"] * 0.98

    # MA20 斜率（近5日 MA20 线性回归，单位：%/天）
    def calc_ma_slope(ma_series, window=5):
        slopes = [np.nan] * len(ma_series)
        for i in range(window - 1, len(ma_series)):
            y = ma_series.iloc[i - window + 1 : i + 1].values
            if np.isnan(y).any():
                continue
            x = np.arange(window)
            slope = np.polyfit(x, y, 1)[0]
            slopes[i] = slope / y[-1] * 100  # % per day
        return slopes

    df["MA20_slope"] = calc_ma_slope(df["MA20"], 5)

    # 近3日量比均值（W2 用）
    df["量比_3d_avg"] = df["量比"].rolling(3).mean()

    return df

# ===================== 信号预检 =====================

def get_latest(d):
    """安全获取最新值，NaN 返回 None"""
    v = d.iloc[-1]
    return None if pd.isna(v) else v

def get_price_position(close, val):
    """价格相对估值的位置描述"""
    if val is None or close is None:
        return None
    ratio = close / val
    if ratio < 0.8:
        return "深度低估"
    elif ratio < 1.0:
        return "低于估值"
    elif ratio < 1.05:
        return "接近估值"
    elif ratio < 1.3:
        return "高于估值"
    else:
        return "严重高估"

def check_signals(code, name, df, pos_info, val_info, entry_pattern="", entry_ma="", batches=None):
    """
    逐条检查信号条件，返回 dict。
    pos_info: positions.csv 该标的的行，或 None
    val_info: valuation_ranking.csv 该标的的行，或 None
    entry_pattern: 模型二买入形态 (A/B/C)，影响 S0/S2/S3 参数
    entry_ma: A类形态的支撑均线 (MA20/MA60)
    batches: 该标的的活跃加仓批次列表 [{batch_id, entry_logic, entry_ma, shares, cost_price, ...}]
    """
    latest = df.iloc[-1]
    n_days = len(df)

    close = get_latest(df["close"])
    ma5 = get_latest(df["MA5"])
    ma10 = get_latest(df["MA10"])
    ma20 = get_latest(df["MA20"])
    ma60 = get_latest(df["MA60"])
    vol_ratio = get_latest(df["量比"])
    shrink = get_latest(df["缩量程度"])
    dist_ma20 = get_latest(df["距MA20"])
    dist_ma60 = get_latest(df["距MA60"])
    gain5 = get_latest(df["近5日涨幅"])
    ma20_slope = get_latest(df["MA20_slope"])
    high20 = get_latest(df["high20"])
    low20 = get_latest(df["low20"])
    box_cons = bool(df["box_consolidation"].iloc[-1]) if "box_consolidation" in df.columns else False
    box_break = bool(df["box_breakout"].iloc[-1]) if "box_breakout" in df.columns else False
    vol_3d_avg = get_latest(df["量比_3d_avg"])

    # 持仓信息
    asset_type = pos_info.get("asset_type", "个股") if pos_info else "无持仓"
    is_etf = asset_type == "ETF"
    has_position = pos_info is not None and pos_info.get("actual_shares", 0) > 0

    # 活跃批次汇总（批次是从总持仓中拆出的独立监控单元，不额外加总）
    active_batches = batches or []
    batch_total_shares = sum(b["shares"] for b in active_batches)

    # 仓位状态计算（总持仓 = positions.actual_shares，已包含批次）
    hold_status = "未持仓"
    if has_position:
        actual = pos_info["actual_shares"]
        target_type = pos_info.get("target_type", "shares")
        target_val = pos_info.get("target_value", 0)
        if target_type == "shares":
            pct = actual / target_val if target_val > 0 else 0
        else:
            pct = (actual * close) / target_val if target_val > 0 and close else 0
        if pct <= 0.50:
            hold_status = "轻仓"
        elif pct < 0.75:
            hold_status = "标准仓"
        else:
            hold_status = "重仓"

    cost = pos_info.get("cost_price", 0) if pos_info else 0
    stop_override = pos_info.get("stop_loss_override", "") if pos_info else ""
    manual_tag = pos_info.get("manual_tag", "") if pos_info else ""

    # 估值锚点
    conservative = val_info.get("conservative") if val_info else None
    base_val = val_info.get("base") if val_info else None
    optimistic = val_info.get("optimistic") if val_info else None
    downside = val_info.get("downside_risk") if val_info else None
    has_val = val_info is not None

    result = {
        "_meta": {
            "code": code, "name": name,
            "close": round(close, 2) if close else None,
            "asset_type": asset_type,
            "hold_status": hold_status,
            "cost_price": cost,
            "has_valuation": has_val,
            "n_days": n_days,
            "batch_count": len(active_batches),
            "batch_total_shares": batch_total_shares,
        },
        "indicators": {
            "MA5": round(ma5, 2) if ma5 else None,
            "MA10": round(ma10, 2) if ma10 else None,
            "MA20": round(ma20, 2) if ma20 else None,
            "MA60": round(ma60, 2) if ma60 else None,
            "vol_ratio": round(vol_ratio, 2) if vol_ratio else None,
            "shrink": round(shrink, 2) if shrink else None,
            "dist_ma20": round(dist_ma20, 2) if dist_ma20 else None,
            "dist_ma60": round(dist_ma60, 2) if dist_ma60 else None,
            "gain5": round(gain5, 2) if gain5 else None,
            "ma20_slope": round(ma20_slope, 3) if ma20_slope else None,
            "high20": round(high20, 2) if high20 else None,
            "low20": round(low20, 2) if low20 else None,
            "box_consolidation": box_cons,
            "box_breakout": box_break,
            "vol_3d_avg": round(vol_3d_avg, 2) if vol_3d_avg else None,
        },
        "buy_signals": [],
        "sell_signals": [],
        "batch_signals": [],
        "warnings": [],
        "valuation_review": [],
    }

    if has_val and close:
        result["valuation"] = {
            "downside_risk": downside,
            "conservative": conservative,
            "base": base_val,
            "optimistic": optimistic,
            "discount_rate": val_info.get("discount_rate"),
            "position": get_price_position(close, base_val),
        }

    # ══════ B0-形态到位（纯技术面，不依赖估值）══════
    if not is_etf and not has_position:
        # B0a: 缩量回调至均线支撑（与B1技术条件相同，去掉估值约束）
        b0a_shrink = shrink is not None and shrink < 0.8
        b0a_near_ma = (dist_ma20 is not None and -5 <= dist_ma20 <= 3) or \
                      (dist_ma60 is not None and -5 <= dist_ma60 <= 3)
        b0a_hit = b0a_shrink and b0a_near_ma
        result["buy_signals"].append({
            "signal": "B0-形态到位（缩量回调至均线支撑，建议优先生成估值报告）",
            "hit": b0a_hit,
            "type": "注意",
            "conditions": {
                "shrink": {"ok": b0a_shrink, "value": shrink, "threshold": 0.8},
                "near_ma": {"ok": b0a_near_ma, "dist_ma20": dist_ma20, "dist_ma60": dist_ma60},
            },
            "suggestion": "技术形态到位，建议优先生成模型三估值报告，确认估值安全边际后再考虑B1买入",
        })

        # B0b: 箱体盘整后放量突破（与B2技术条件相同，去掉估值约束）
        b0b_box = box_cons and box_break
        b0b_vol = vol_ratio is not None and vol_ratio > 1.5
        b0b_hit = b0b_box and b0b_vol
        result["buy_signals"].append({
            "signal": "B0-形态到位（箱体盘整后放量突破，建议优先生成估值报告）",
            "hit": b0b_hit,
            "type": "注意",
            "conditions": {
                "box": {"ok": b0b_box, "consolidation": box_cons, "breakout": box_break},
                "vol_ratio": {"ok": b0b_vol, "value": vol_ratio, "threshold": 1.5},
            },
            "suggestion": "技术形态到位，建议优先生成模型三估值报告，确认估值安全边际后再考虑B2买入",
        })

    # ──── ETF 跳过大部分信号 ────
    if is_etf:
        _check_etf_signals(result, code, name, close, cost, ma5, ma10, ma20,
                           ma20_slope, dist_ma20, dist_ma60, vol_ratio, shrink,
                           gain5, hold_status, stop_override, n_days, df)
        # ETF 也走手动标记覆盖
        if manual_tag and has_position:
            result["_meta"]["manual_tag"] = manual_tag
            for s in result["sell_signals"]:
                if s["hit"]:
                    s["hit"] = False
                    s["overridden"] = True
                    s["override_reason"] = f"手动标记「{manual_tag}」，卖出信号已抑制"
        return result

    # ══════ 买入信号 ══════

    # B1: 估值区间缩量回调
    if has_val and close and conservative and base_val:
        b1_price_ok = conservative <= close <= base_val
        b1_shrink = shrink is not None and shrink < 0.8
        b1_near_ma = (dist_ma20 is not None and -5 <= dist_ma20 <= 3) or \
                     (dist_ma60 is not None and -5 <= dist_ma60 <= 3)
        b1_hit = b1_price_ok and b1_shrink and b1_near_ma
        result["buy_signals"].append({
            "signal": "B1-估值买点（价格在悲观~基准估值区间，缩量回调至均线支撑）",
            "hit": b1_hit,
            "conditions": {
                "price_range": {"ok": b1_price_ok, "close": close, "conservative": conservative, "base": base_val},
                "shrink": {"ok": b1_shrink, "value": shrink, "threshold": 0.8},
                "near_ma": {"ok": b1_near_ma, "dist_ma20": dist_ma20, "dist_ma60": dist_ma60},
            },
            "suggestion": _b1_suggestion(hold_status),
        })

    # T0-买入逻辑有效性检查（B1触发时，检查模型二的买入形态支撑是否仍在）
    if has_val and not is_etf:
        b1_triggered = any(s["hit"] for s in result["buy_signals"] if "B1" in s["signal"])
        if b1_triggered and entry_pattern:
            entry_warning = None
            if entry_pattern == "A" and entry_ma:
                entry_ma_val = ma20 if entry_ma == "MA20" else (ma60 if entry_ma == "MA60" else None)
                if entry_ma_val and close and close < entry_ma_val:
                    entry_warning = f"买入支撑{entry_ma}({entry_ma_val:.2f})已跌破(现价{close:.2f})，买入逻辑弱化"
            elif entry_pattern == "C" and ma20_slope is not None and ma20_slope < -0.2:
                entry_warning = f"上升趋势可能终结（MA20斜率{ma20_slope:.3f}%/天< -0.2），C类买入逻辑弱化"

            if entry_warning:
                result["buy_signals"].append({
                    "signal": "T0-买入逻辑弱化（技术面与估值信号冲突）",
                    "hit": True,
                    "type": "注意",
                    "conditions": {"warning": entry_warning, "entry_pattern": entry_pattern, "entry_ma": entry_ma},
                    "suggestion": "估值到位但买入形态支撑在弱化，建议等企稳确认后再建仓，不要急于抄底",
                })
                # 给B1信号加冲突标记
                for s in result["buy_signals"]:
                    if "B1" in s["signal"] and s["hit"]:
                        s["conflict"] = True
                        s["conflict_reason"] = entry_warning
                        s["suggestion"] = "⚠️ " + s["suggestion"] + "（买入逻辑支撑弱化，建议等待企稳）"

    # B2: 箱体放量突破
    if has_val and close and conservative and optimistic:
        b2_price_ok = conservative <= close <= optimistic
        b2_box = box_cons and box_break
        b2_vol = vol_ratio is not None and vol_ratio > 1.5
        b2_hit = b2_price_ok and b2_box and b2_vol
        result["buy_signals"].append({
            "signal": "B2-突破买点（箱体盘整后放量突破上沿）",
            "hit": b2_hit,
            "conditions": {
                "price_range": {"ok": b2_price_ok, "close": close, "conservative": conservative, "optimistic": optimistic},
                "box": {"ok": b2_box, "consolidation": box_cons, "breakout": box_break},
                "vol_ratio": {"ok": b2_vol, "value": vol_ratio, "threshold": 1.5},
            },
            "suggestion": _b2_suggestion(hold_status, vol_ratio),
        })

    # ══════ 卖出信号 ══════

    if not has_position:
        return result  # 非持仓不展示卖出信号

    # S0-支撑止损
    if close:
        s0_tolerance = {"轻仓": 0.05, "标准仓": 0.03, "重仓": 0.02}.get(hold_status, 0.03)
        # 按买入形态确定支撑位
        support = low20  # 默认：近20日最低价
        if entry_pattern == "A" and entry_ma:
            if entry_ma == "MA20" and ma20:
                support = ma20
            elif entry_ma == "MA60" and ma60:
                support = ma60
        elif entry_pattern == "C" and ma20:
            support = ma20
        # B类 / 无形态记录 → 维持原逻辑(近20日最低价)
        if downside and support:
            support = max(support, downside)
        s0_trigger = support and close < support * (1 - s0_tolerance)
        is_volume_break = vol_ratio is not None and vol_ratio > 1.0
        support_label = "近20日最低价"
        if entry_pattern == "A" and entry_ma:
            support_label = f"买入支撑均线({entry_ma})"
        elif entry_pattern == "C":
            support_label = "趋势均线(MA20)"
        if downside:
            support_label += "+估值兜底"
        result["sell_signals"].append({
            "signal": "S0-支撑止损（买入逻辑破坏，关键支撑位失守）",
            "hit": s0_trigger if support else False,
            "priority": "最高",
            "type": "执行" if (hold_status == "重仓" or (hold_status != "轻仓" and is_volume_break)) else "注意",
            "conditions": {
                "close": close, "support": round(support, 2) if support else None,
                "support_source": support_label,
                "tolerance_pct": s0_tolerance * 100,
                "trigger_price": round(support * (1 - s0_tolerance), 2) if support else None,
                "volume_break": is_volume_break,
            },
            "suggestion": _s0_support_suggestion(hold_status, is_volume_break),
        })

    # S0-硬止损
    if cost > 0 and close:
        stop_pct = float(stop_override) if stop_override else {"轻仓": -0.12, "标准仓": -0.10, "重仓": -0.08}.get(hold_status, -0.10)
        s0_hard_trigger = close < cost * (1 + stop_pct)
        result["sell_signals"].append({
            "signal": "S0-硬止损（成本线击穿，无条件执行）",
            "hit": s0_hard_trigger,
            "priority": "最高",
            "type": "执行",
            "conditions": {
                "close": close, "cost": cost, "stop_pct": stop_pct * 100,
                "trigger_price": round(cost * (1 + stop_pct), 2),
            },
            "suggestion": "建议减仓或清仓，成本线失守，无条件执行",
        })

    # S1-估值卖点
    if has_val and optimistic and close:
        s1_trigger = close > optimistic * 1.1
        result["sell_signals"].append({
            "signal": "S1-估值卖点（价格透支乐观估值，减半仓+触发估值复核）",
            "hit": s1_trigger,
            "priority": "最高",
            "type": "执行" if has_position else "注意",
            "conditions": {
                "close": close, "optimistic": optimistic, "trigger_price": round(optimistic * 1.1, 2),
            },
            "suggestion": "建议减仓至半仓以下，触发估值复核" if has_position else "不追买",
        })

    # S4-均线破位 (覆盖 S2)
    if ma20_slope is not None:
        ma_flat = abs(ma20_slope) < 0.3
        ma_decline = ma20_slope < -0.2
        s4_ma = {"轻仓": ma20, "标准仓": ma10, "重仓": ma5}.get(hold_status)
        s4_below = close and s4_ma and close < s4_ma
        s4_trigger = s4_below and (ma_flat or ma_decline)
        result["sell_signals"].append({
            "signal": "S4-均线破位（趋势转折，均线走平或向下）",
            "hit": s4_trigger,
            "priority": "中",
            "type": "注意" if hold_status == "轻仓" else "执行",
            "conditions": {
                "close": close, "ma": round(s4_ma, 2) if s4_ma else None,
                "ma_slope": round(ma20_slope, 3),
                "ma_state": "走平" if ma_flat else ("向下" if ma_decline else "向上"),
            },
            "suggestion": _s4_suggestion(hold_status),
        })

    # S2-移动止盈 (仅在均线上升时)
    if ma20_slope is not None and ma20_slope > 0.3 and close and cost:
        if entry_pattern == "C":
            # C类趋势回踩容忍度更大，止盈均线下移一级
            s2_ma = {"轻仓": ma10, "标准仓": ma5, "重仓": ma5}.get(hold_status)
        else:
            s2_ma = {"轻仓": ma20, "标准仓": ma10, "重仓": ma5}.get(hold_status)
        s2_below = s2_ma and close < s2_ma
        s2_profit = close > cost * 1.05
        s2_trigger = s2_below and s2_profit
        is_vol_down = vol_ratio is not None and vol_ratio < 1.0
        result["sell_signals"].append({
            "signal": "S2-移动止盈（均线止盈，保护利润）",
            "hit": s2_trigger,
            "priority": "中",
            "type": "注意" if (hold_status == "轻仓" or (hold_status == "标准仓" and not is_vol_down)) else "执行",
            "conditions": {
                "close": close, "ma": round(s2_ma, 2) if s2_ma else None,
                "profit_above_cost": s2_profit,
                "volume_break": is_vol_down,
            },
            "suggestion": _s2_suggestion(hold_status, is_vol_down),
        })

    # S3-量价背离
    if gain5 is not None and shrink is not None:
        if entry_pattern == "A":
            # A类买的是缩量反弹，缩量涨不动即为风险，涨幅阈值下调
            s3_threshold = {"轻仓": 3, "标准仓": 2, "重仓": 0}.get(hold_status, 2)
        else:
            s3_threshold = {"轻仓": 5, "标准仓": 3, "重仓": 0}.get(hold_status, 3)
        s3_trigger = gain5 > s3_threshold and shrink < 0.6
        result["sell_signals"].append({
            "signal": "S3-量价背离（动能衰竭，价涨量缩，早期预警）",
            "hit": s3_trigger,
            "priority": "低",
            "type": "注意" if hold_status != "重仓" else "执行",
            "conditions": {
                "gain5": gain5, "gain_threshold": s3_threshold,
                "shrink": shrink, "shrink_threshold": 0.6,
                "entry_adjusted": entry_pattern == "A",
            },
            "suggestion": "量价背离，关注动能衰竭" if hold_status != "重仓" else "重仓+量价背离，建议减至标准仓",
        })

    # S5-加速赶顶
    if gain5 is not None and vol_ratio is not None:
        s5_trigger = gain5 > 20 and vol_ratio > 2.5
        result["sell_signals"].append({
            "signal": "S5-加速赶顶（情绪极端，5日急涨+放量）",
            "hit": s5_trigger,
            "priority": "高",
            "type": "执行",
            "conditions": {
                "gain5": gain5, "gain_threshold": 20,
                "vol_ratio": vol_ratio, "vol_threshold": 2.5,
            },
            "suggestion": "短期急涨+放量，建议减仓或清仓" if has_position else "不追买",
        })

    # ══════ 批次卖出信号（每批次独立检查）══════
    if has_position and active_batches:
        for batch in active_batches:
            b_sigs = []
            b_cost = batch["cost_price"]
            b_logic = batch.get("entry_logic", "")
            b_ma = batch.get("entry_ma", "")
            b_stop_override = batch.get("stop_loss_override", "")

            # S0-批次支撑止损（按买入逻辑确定支撑位）
            b_support = low20  # 默认
            b_support_label = "近20日最低价"
            if b_logic == "B1" and b_ma:
                if b_ma == "MA20" and ma20:
                    b_support = ma20
                    b_support_label = f"买入均线(MA20)"
                elif b_ma == "MA60" and ma60:
                    b_support = ma60
                    b_support_label = f"买入均线(MA60)"
            elif b_logic == "B2":
                b_support_label = "箱体下沿(待激活)"
            # 手动补仓 → 保持 low20

            if b_support and close:
                b_s0_tolerance = {"轻仓": 0.05, "标准仓": 0.03, "重仓": 0.02}.get(hold_status, 0.03)
                if downside:
                    b_support = max(b_support, downside)
                    b_support_label += "+估值兜底"
                b_s0_trigger = close < b_support * (1 - b_s0_tolerance)
                b_sigs.append({
                    "signal": "S0-批次支撑止损",
                    "hit": b_s0_trigger,
                    "priority": "最高",
                    "conditions": {
                        "support": round(b_support, 2), "support_source": b_support_label,
                        "trigger_price": round(b_support * (1 - b_s0_tolerance), 2),
                        "close": close, "tolerance_pct": b_s0_tolerance * 100,
                    },
                    "suggestion": f"该批次买入逻辑({b_logic})支撑位失守，建议卖出{batch['shares']}股",
                })

            # S0-批次硬止损（用批次成本价）
            if b_cost > 0 and close:
                b_stop_pct = float(b_stop_override) if b_stop_override else \
                    {"轻仓": -0.12, "标准仓": -0.10, "重仓": -0.08}.get(hold_status, -0.10)
                b_hard_trigger = close < b_cost * (1 + b_stop_pct)
                b_sigs.append({
                    "signal": "S0-批次硬止损",
                    "hit": b_hard_trigger,
                    "priority": "最高",
                    "conditions": {
                        "cost_price": b_cost, "stop_pct": b_stop_pct * 100,
                        "trigger_price": round(b_cost * (1 + b_stop_pct), 2),
                        "close": close,
                    },
                    "suggestion": f"批次成本{b_cost}已跌破止损线，建议卖出{batch['shares']}股",
                })

            # S2-批次移动止盈（用批次成本价）
            if b_cost > 0 and close and ma20_slope is not None and ma20_slope > 0.3:
                b_s2_ma = {"轻仓": ma20, "标准仓": ma10, "重仓": ma5}.get(hold_status)
                if b_s2_ma and close < b_s2_ma and close > b_cost * 1.05:
                    b_sigs.append({
                        "signal": "S2-批次移动止盈",
                        "hit": True,
                        "priority": "中",
                        "conditions": {
                            "close": close, "ma": round(b_s2_ma, 2),
                            "batch_cost": b_cost, "profit_pct": round((close - b_cost) / b_cost * 100, 1),
                        },
                        "suggestion": f"批次浮盈{(close-b_cost)/b_cost*100:.1f}%，跌破{b_s2_ma}，建议卖出{batch['shares']}股",
                    })

            # 浮盈/浮亏快照（仅展示，不触发信号）
            b_pnl = round((close - b_cost) / b_cost * 100, 1) if b_cost > 0 and close else None

            result["batch_signals"].append({
                "batch_id": batch["batch_id"],
                "entry_logic": b_logic,
                "entry_ma": b_ma if b_ma else None,
                "shares": batch["shares"],
                "cost_price": b_cost,
                "pnl_pct": b_pnl,
                "signals": b_sigs,
            })

    # ══════ 前瞻预警 ══════

    # W1-价格预警
    if has_position and low20 and close:
        w1_dist = (close - low20) / low20 * 100
        if 0 < w1_dist < 2:
            result["warnings"].append({
                "type": "W1-价格预警",
                "detail": f"距S0支撑位仅{w1_dist:.1f}%，明日若继续下跌将触发支撑止损",
            })

    if gain5 is not None and vol_ratio is not None:
        if gain5 > 15 and vol_ratio > 2.0:
            result["warnings"].append({
                "type": "W1-加速预警",
                "detail": f"近5日涨{gain5:.1f}%，量比{vol_ratio:.1f}，若明日继续放量急拉可能触发S5",
            })

    # W2-量价预警
    if shrink is not None and shrink < 0.5:
        result["warnings"].append({
            "type": "W2-量价预警",
            "detail": f"缩量程度{shrink:.2f}，极度缩量，变盘前兆",
        })
    if vol_3d_avg is not None and vol_3d_avg < 0.6:
        result["warnings"].append({
            "type": "W2-量能预警",
            "detail": f"近3日量比均值{vol_3d_avg:.2f}，量能持续萎缩，关注方向选择",
        })

    # ══════ 手动标记覆盖 ══════
    if manual_tag and has_position:
        result["_meta"]["manual_tag"] = manual_tag
        for s in result["sell_signals"]:
            if s["hit"]:
                s["hit"] = False
                s["overridden"] = True
                s["override_reason"] = f"手动标记「{manual_tag}」，卖出信号已抑制"
        # 但保留 W 类预警（仍提醒关注，只是不执行卖出）

    # ══════ 估值复核 ══════
    if has_val and close:
        if downside and close < downside * 0.7:
            result["valuation_review"].append({
                "type": "价格严重偏离",
                "detail": f"当前价{close} < 下行风险价{downside}×0.7，估值可能失真",
            })
        if optimistic and close > optimistic * 1.3:
            result["valuation_review"].append({
                "type": "价格严重偏离",
                "detail": f"当前价{close} > 乐观估值{optimistic}×1.3，估值可能失真",
            })

    return result

def _check_etf_signals(result, code, name, close, cost, ma5, ma10, ma20,
                        ma20_slope, dist_ma20, dist_ma60, vol_ratio, shrink,
                        gain5, hold_status, stop_override, n_days, df):
    """ETF 简化信号：仅 S0硬止损 + S4均线破位 + S2移动止盈 + W1/W2"""
    has_position = hold_status != "未持仓"

    # S0-硬止损
    if cost > 0 and close and has_position:
        stop_pct = float(stop_override) if stop_override else {"轻仓": -0.12, "标准仓": -0.10, "重仓": -0.08}.get(hold_status, -0.10)
        s0_hard_trigger = close < cost * (1 + stop_pct)
        result["sell_signals"].append({
            "signal": "S0-硬止损（成本线击穿，无条件执行）",
            "hit": s0_hard_trigger,
            "priority": "最高", "type": "执行",
            "conditions": {"close": close, "cost": cost, "stop_pct": stop_pct * 100,
                          "trigger_price": round(cost * (1 + stop_pct), 2)},
            "suggestion": "建议减仓或清仓，ETF成本线失守，无条件执行",
        })

    # S4-均线破位 (ETF 只看 MA20)
    if ma20_slope is not None and close and ma20 and has_position:
        ma_flat = abs(ma20_slope) < 0.3
        ma_decline = ma20_slope < -0.2
        s4_trigger = close < ma20 and (ma_flat or ma_decline)
        result["sell_signals"].append({
            "signal": "S4-均线破位（ETF，趋势转折）",
            "hit": s4_trigger,
            "priority": "中", "type": "注意" if hold_status == "轻仓" else "执行",
            "conditions": {"close": close, "MA20": round(ma20, 2),
                          "ma_state": "走平" if ma_flat else ("向下" if ma_decline else "向上")},
            "suggestion": _s4_suggestion(hold_status),
        })

    # S2-移动止盈 (ETF, MA20 上升中跌破)
    if ma20_slope is not None and ma20_slope > 0.3 and close and ma20 and cost and has_position:
        s2_trigger = close < ma20 and close > cost * 1.05
        result["sell_signals"].append({
            "signal": "S2-移动止盈（ETF，跌破均线但趋势仍向上）",
            "hit": s2_trigger,
            "priority": "中", "type": "注意",
            "conditions": {"close": close, "MA20": round(ma20, 2)},
            "suggestion": "ETF跌破MA20但趋势仍向上，关注不急于减仓",
        })

    # W1/W2 (与个股相同)
    if shrink is not None and shrink < 0.5:
        result["warnings"].append({"type": "W2-量价预警", "detail": f"缩量程度{shrink:.2f}，极度缩量"})
    vol_3d_avg = get_latest(df["量比_3d_avg"]) if "量比_3d_avg" in df.columns else None
    if vol_3d_avg is not None and vol_3d_avg < 0.6:
        result["warnings"].append({"type": "W2-量能预警", "detail": f"近3日量比均值{vol_3d_avg:.2f}，量能持续萎缩"})


# ── 操作建议生成 ──

def _b1_suggestion(hold_status):
    m = {"未持仓": "建议建仓至轻仓", "轻仓": "建议加仓至标准仓",
         "标准仓": "持有观察，不加仓", "重仓": "不加仓"}
    return m.get(hold_status, "")

def _b2_suggestion(hold_status, vol_ratio):
    if hold_status == "未持仓": return "建议建仓至轻仓"
    if hold_status == "轻仓": return "建议加仓至标准仓"
    if hold_status == "标准仓":
        return "持有观察（量比>2.0可加一次仓）" if (vol_ratio and vol_ratio > 2.0) else "持有观察"
    return "不加仓"

def _s0_support_suggestion(hold_status, is_vol_break):
    if hold_status == "重仓": return "减仓至标准仓"
    if is_vol_break: return "减仓至轻仓"
    return "关注，等次日确认"

def _s4_suggestion(hold_status):
    return {"轻仓": "关注，暂不减", "标准仓": "减至轻仓", "重仓": "减至标准仓"}.get(hold_status, "")

def _s2_suggestion(hold_status, is_vol_down):
    if hold_status == "重仓": return "减至标准仓"
    if is_vol_down: return "减至轻仓"
    return "关注，暂不减"


# ===================== 主流程 =====================

def process_stock(stock, positions, rankings, datestr, use_cache=True):
    """处理单只标的，返回 signal_check dict 或 None"""
    code = stock["code"]
    name = stock["name"]
    entry_pattern = stock.get("entry_pattern", "")
    entry_ma = stock.get("entry_ma", "")

    # 拉取日线
    df, source = fetch_daily(code, name, datestr, use_cache)
    if df is None:
        print(f"  ❌ {code} {name}: {source}")
        return None

    # 计算指标
    df = calc_indicators(df)

    # 信号预检
    pos_info = positions.get(code)
    val_info = rankings.get(code)
    stock_batches = batches.get(code, []) if batches else []
    result = check_signals(code, name, df, pos_info, val_info, entry_pattern, entry_ma, stock_batches)

    source_icon = "✅" if source.startswith("cache") else ("📶" if source == "tdx" else "📡")
    print(f"  {source_icon} {code} {name} | "
          f"{result['_meta']['hold_status']} | "
          f"{'有估值' if result['_meta']['has_valuation'] else '无估值'} | "
          f"{len(df)}日")

    return result


# ===================== 池子维护 =====================

def read_latest_quant():
    """读取 quant/ 目录下最新日期的精选池 CSV。返回 [{code, name, 通过类别, entry_ma, 量比}]"""
    quant_dir = f"{PROJECT_ROOT}/quant"
    if not os.path.isdir(quant_dir):
        return []
    quants = sorted(
        [f for f in os.listdir(quant_dir) if f.startswith("quant_") and f.endswith(".csv")],
        reverse=True
    )
    if not quants:
        return []

    quant_path = os.path.join(quant_dir, quants[0])
    stocks = []
    with open(quant_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row.get("股票代码", "").replace('="', "").replace('"', "")
            stocks.append({
                "code": code,
                "name": row.get("股票名称", ""),
                "entry_pattern": row.get("通过类别", "").strip(),
                "entry_ma": row.get("entry_ma", "").strip(),
                "量比": float(row.get("量比", 0) or 0),
            })

    # 解析 quant 文件名中的日期，判断是否过期
    fname = quants[0]
    date_part = fname.replace("quant_", "").replace(".csv", "")
    stale = False
    try:
        from datetime import datetime as dt
        fdate = dt.strptime(date_part, "%y%m%d")
        if (dt.now() - fdate).days > 5:
            stale = True
    except ValueError:
        pass

    return stocks, stale, quants[0]


def maintain_pool(pool, positions, rankings, today_str, interactive=True):
    """池子维护：调入/调出/缺失估值检测。

    Returns:
        new_pool: 更新后的池子
        changelist: {added, removed, updated, missing_valuation, stale_quant}
    """
    quant_result = read_latest_quant()
    if not quant_result:
        return pool, {"added": [], "removed": [], "updated": [], "missing_valuation": [], "stale_quant": False}
    quant_stocks, stale_quant, quant_fname = quant_result

    today_full = datetime.now().strftime("%Y-%m-%d")
    changelist = {
        "added": [], "removed": [], "updated": [],
        "missing_valuation": [], "stale_quant": stale_quant
    }

    # 构建 code → pool_entry 索引
    pool_by_code = {s["code"]: s for s in pool}
    quant_codes = {s["code"] for s in quant_stocks}

    # ── 调入判断 ──
    for qs in quant_stocks:
        code = qs["code"]
        if code not in pool_by_code:
            priority = "优先" if qs["量比"] > 2.0 else "正常"
            entry = {
                "code": code,
                "name": qs["name"],
                "first_added": today_full,
                "last_quant_date": today_full,
                "consecutive_miss": 0,
                "priority": priority,
                "has_valuation": "TRUE" if code in rankings else "FALSE",
                "status": "跟踪中",
                "entry_pattern": qs["entry_pattern"],
                "entry_ma": qs["entry_ma"],
            }
            pool.append(entry)
            pool_by_code[code] = entry
            changelist["added"].append({"code": code, "name": qs["name"], "priority": priority})
        else:
            existing = pool_by_code[code]
            existing["consecutive_miss"] = 0
            existing["last_quant_date"] = today_full
            # 重回 quant 的标的，重置状态
            if existing.get("status") == "已移出":
                existing["status"] = "跟踪中"
                changelist["updated"].append({"code": code, "name": existing["name"], "change": "已移出→跟踪中"})

    # ── 调出判断 ──
    for s in pool:
        code = s["code"]
        if code in quant_codes:
            continue  # 仍在 quant 中，不处理

        # 持仓保留
        pos = positions.get(code, {})
        if pos.get("actual_shares", 0) > 0:
            continue

        s["consecutive_miss"] = int(s.get("consecutive_miss", 0)) + 1

        if s["consecutive_miss"] >= 5:
            if s.get("has_valuation") == "TRUE":
                s["status"] = "观察中"
            else:
                s["status"] = "已移出"
                changelist["removed"].append({
                    "code": code, "name": s["name"],
                    "consecutive_miss": s["consecutive_miss"],
                    "has_valuation": s.get("has_valuation", "FALSE")
                })
        else:
            s["status"] = "观察中"

    # ── 缺失估值检测 ──
    for s in pool:
        if s.get("has_valuation") == "FALSE" and s.get("status") != "ETF跟踪" and s["code"] not in [r["code"] for r in changelist["removed"]]:
            changelist["missing_valuation"].append({"code": s["code"], "name": s["name"]})

    # ── 打印变更清单 ──
    print(f"\n{'='*60}")
    print(f"池子维护 — 数据源: {quant_fname}")
    if stale_quant:
        print(f"⚠️ 模型二精选池已超过5天未更新，建议先运行模型二。")
    print(f"{'='*60}")

    if changelist["added"]:
        print(f"\n📥 调入 ({len(changelist['added'])} 只):")
        for a in changelist["added"]:
            print(f"  + {a['code']} {a['name']} [{a['priority']}]")

    if changelist["removed"]:
        print(f"\n📤 调出 ({len(changelist['removed'])} 只):")
        for r in changelist["removed"]:
            print(f"  - {r['code']} {r['name']} (连续{r['consecutive_miss']}次未入选, 估值={'有' if r['has_valuation'] == 'TRUE' else '无'})")

    if changelist["updated"]:
        print(f"\n🔄 状态更新 ({len(changelist['updated'])} 只):")
        for u in changelist["updated"]:
            print(f"  ~ {u['code']} {u['name']}: {u['change']}")

    if changelist["missing_valuation"]:
        print(f"\n⚠️ 缺少估值 ({len(changelist['missing_valuation'])} 只):")
        for mv in changelist["missing_valuation"]:
            print(f"  ? {mv['code']} {mv['name']}")

    if not any([changelist["added"], changelist["removed"], changelist["updated"], changelist["missing_valuation"]]):
        print(f"\n✅ 无变更，核心池保持 {len(pool)} 只")

    # ── 交互确认 ──
    if interactive:
        print()
        choice = input("确认以上变更？(y/n): ").strip().lower()
        if choice != 'y':
            print("⚠️ 已跳过写入。可手动编辑 core_pool.csv 后重跑。")
            return pool, changelist

    write_core_pool(pool)
    return pool, changelist


def write_core_pool(pool, path=None):
    """写回 core_pool.csv"""
    if path is None:
        path = CORE_POOL_PATH

    fieldnames = ["code", "name", "first_added", "last_quant_date", "consecutive_miss",
                  "priority", "has_valuation", "status", "entry_pattern", "entry_ma"]

    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for s in pool:
            row = {k: s.get(k, "") for k in fieldnames}
            # code 用公式文本格式
            code_val = str(row["code"]).replace('="', "").replace('"', "")
            row["code"] = f'="{code_val}"'
            writer.writerow(row)

    print(f"✅ core_pool.csv 已更新 ({len(pool)} 只)")


def main():
    parser = argparse.ArgumentParser(description="模型四 tracker.py — 日线拉取 + 指标计算 + 信号预检")
    parser.add_argument("--test", action="store_true", help="只处理有持仓的标的")
    parser.add_argument("--code", type=str, help="单只标的")
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存，重新拉取行情")
    parser.add_argument("--yes", action="store_true", help="跳过池子维护交互确认")
    parser.add_argument("--date", type=str, default=None, help="指定日期(YYMMDD)，默认今天，周末需手动指定最近交易日")
    args = parser.parse_args()

    datestr = args.date if args.date else datetime.now().strftime("%y%m%d")

    # 清理旧缓存
    cleanup_cache = DailyCache()
    cleaned = cleanup_cache.cleanup_old(keep_days=30)
    if cleaned:
        print(f"🧹 清理旧缓存: {cleaned} 个文件")

    # 读取数据
    pool = read_core_pool()
    positions = read_positions()
    batches = read_batches()
    rankings = read_ranking()

    # 池子维护（调入/调出/缺失估值检测）
    pool, changelist = maintain_pool(pool, positions, rankings, datestr, interactive=not args.yes)

    if args.code:
        # 单只模式
        pool = [s for s in pool if s["code"] == args.code]
        if not pool:
            # 可能不在 core_pool 但用户在测试单只
            pool = [{"code": args.code, "name": args.code, "priority": "正常",
                     "has_valuation": str(args.code in rankings), "status": "跟踪中"}]

    if args.test:
        # 测试模式：只跑有持仓的
        pool = [s for s in pool if s["code"] in positions]
        print(f"🧪 测试模式：{len(pool)} 只持仓标的")

    if not pool:
        print("❌ 无标的可处理")
        sys.exit(1)

    print(f"\n{'='*60}")
    print(f"模型四 tracker.py — 日线拉取 + 信号预检")
    print(f"日期: {datestr} | 标的: {len(pool)} 只 | 缓存: {'OFF' if args.no_cache else 'ON'}")
    print(f"{'='*60}")

    # 逐只处理
    results = []
    cache_hits = 0
    api_calls = 0
    tdx_calls = 0
    api_fails = 0
    retry_queue = []  # code=112 失败的，待主循环结束后重试

    for i, stock in enumerate(pool):
        print(f"[{i+1}/{len(pool)}]", end=" ")
        df, source = fetch_daily(stock["code"], stock["name"], datestr, not args.no_cache)
        if df is None:
            if "112" in str(source):
                print(f"❌ {stock['code']} {stock['name']}: {source} → 加入重试队列")
                retry_queue.append(stock)
            else:
                print(f"❌ {stock['code']} {stock['name']}: {source}")
                api_fails += 1
            continue

        if source.startswith("cache"):
            cache_hits += 1
        elif source == "tdx":
            tdx_calls += 1
        else:
            api_calls += 1

        df = calc_indicators(df)
        pos_info = positions.get(stock["code"])
        val_info = rankings.get(stock["code"])
        stock_batches = batches.get(stock["code"], [])
        result = check_signals(stock["code"], stock["name"], df, pos_info, val_info,
                              stock.get("entry_pattern", ""), stock.get("entry_ma", ""),
                              stock_batches)
        results.append(result)

        has_val_str = "📊" if result["_meta"]["has_valuation"] else "  "
        buy_count = sum(1 for s in result["buy_signals"] if s["hit"])
        sell_count = sum(1 for s in result["sell_signals"] if s["hit"])
        batch_sig_count = sum(1 for bs in result.get("batch_signals", []) for s in bs["signals"] if s["hit"])
        warn_count = len(result["warnings"])
        flags = ""
        if buy_count: flags += f" B×{buy_count}"
        if sell_count: flags += f" S×{sell_count}"
        if batch_sig_count: flags += f" 批次S×{batch_sig_count}"
        if warn_count: flags += f" W×{warn_count}"
        batch_info = f" +{result['_meta']['batch_count']}批次" if result['_meta'].get('batch_count') else ""
        print(f"{has_val_str} {result['_meta']['hold_status']:<4}{batch_info} {flags}")

    # ── 失败重试队列 ──
    if retry_queue:
        print(f"\n{'='*60}")
        print(f"🔄 重试 {len(retry_queue)} 只频率限制失败的标的...")
        print(f"{'='*60}")
        time.sleep(10)  # 冷却

        for i, stock in enumerate(retry_queue):
            print(f"[重试 {i+1}/{len(retry_queue)}]", end=" ")
            df, source = fetch_daily(stock["code"], stock["name"], datestr, use_cache=False)
            if df is None:
                print(f"❌ {stock['code']} {stock['name']}: 重试仍失败")
                api_fails += 1
                continue

            if source.startswith("cache"):
                cache_hits += 1
            elif source == "tdx":
                tdx_calls += 1
            else:
                api_calls += 1
            df = calc_indicators(df)
            pos_info = positions.get(stock["code"])
            val_info = rankings.get(stock["code"])
            stock_batches = batches.get(stock["code"], [])
            result = check_signals(stock["code"], stock["name"], df, pos_info, val_info,
                                  stock.get("entry_pattern", ""), stock.get("entry_ma", ""),
                                  stock_batches)
            results.append(result)

            has_val_str = "📊" if result["_meta"]["has_valuation"] else "  "
            buy_count = sum(1 for s in result["buy_signals"] if s["hit"])
            sell_count = sum(1 for s in result["sell_signals"] if s["hit"])
            flags = ""
            if buy_count: flags += f" B×{buy_count}"
            if sell_count: flags += f" S×{sell_count}"
            print(f"{has_val_str} {result['_meta']['hold_status']:<4} {flags}")

    # 输出 JSON
    output = {
        "_meta": {
            "date": datestr,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "total_stocks": len(pool),
            "successful": len(results),
            "failed": api_fails,
            "cache_hits": cache_hits,
            "api_calls": api_calls,
            "tdx_calls": tdx_calls,
        },
        "stocks": results,
    }

    out_path = f"{SIGNALS_DIR}/signals_{datestr}.json"
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    class NpEncoder(json.JSONEncoder):
        def default(self, obj):
            if isinstance(obj, (np.bool_,)):
                return bool(obj)
            if isinstance(obj, (np.integer,)):
                return int(obj)
            if isinstance(obj, (np.floating,)):
                return float(obj)
            if isinstance(obj, np.ndarray):
                return obj.tolist()
            return super().default(obj)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, cls=NpEncoder)

    # 摘要
    print(f"\n{'='*60}")
    print(f"✅ 完成: {len(results)}/{len(pool)} 只 | 缓存命中 {cache_hits} | 妙想API {api_calls} | 通达信 {tdx_calls} | 失败 {api_fails}")

    buy_triggered = [r for r in results if any(s["hit"] for s in r["buy_signals"])]
    sell_triggered = [r for r in results if any(s["hit"] for s in r["sell_signals"])]
    batch_triggered = [r for r in results if any(s["hit"] for bs in r.get("batch_signals", []) for s in bs["signals"])]
    warned = [r for r in results if r["warnings"]]
    val_review = [r for r in results if r["valuation_review"]]

    if buy_triggered:
        print(f"\n🔵 买入信号触发: {len(buy_triggered)} 只")
        for r in buy_triggered:
            for s in r["buy_signals"]:
                if s["hit"]:
                    print(f"   {r['_meta']['code']} {r['_meta']['name']}: {s['signal']}")

    if sell_triggered:
        print(f"\n🔴 卖出信号触发(底仓): {len(sell_triggered)} 只")
        for r in sell_triggered:
            for s in r["sell_signals"]:
                if s["hit"]:
                    print(f"   {r['_meta']['code']} {r['_meta']['name']}: {s['signal']} ({s['priority']})")

    if batch_triggered:
        print(f"\n🟠 批次卖出信号触发: {len(batch_triggered)} 只")
        for r in batch_triggered:
            for bs in r.get("batch_signals", []):
                for s in bs["signals"]:
                    if s["hit"]:
                        print(f"   {r['_meta']['code']} {r['_meta']['name']} [{bs['batch_id']}]: {s['signal']}")

    if warned:
        print(f"\n🟡 前瞻预警: {len(warned)} 只")

    if val_review:
        print(f"\n⚠️  估值复核: {len(val_review)} 只")

    print(f"\n📄 详细结果: {out_path}")


if __name__ == "__main__":
    main()
