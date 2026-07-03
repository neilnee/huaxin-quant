#!/usr/bin/env python3
"""
valuate.py — 模型三估值分析 阶段零 数据准备脚本
对应指令卡: instructions/03-valuation.md

功能:
  0.1 进度检查 — 读取 _index.csv，判断首次覆盖/持续跟踪/跳过
  0.2 财务数据解析 — 从 cache/financial/ 读取 mx-data 原始 JSON
  0.3 指标计算 — OCF/NP、固定资产/总资产、毛利率波动、非经常性占比等
  0.4 决策树信号 — Q1/Q1b/Q2/Q3/Q_IRREG 自动判定
  0.5 预期差漏斗 — 产能/技术/海外/客户/非经常性 搜索优先级
  0.6 简报册输出 — cache/briefing/<code>_<YYMMDD>.json

用法:
  python3 scripts/valuate.py                      # 处理 _index.csv 中所有待处理标的
  python3 scripts/valuate.py --code 300442         # 单只标的
  python3 scripts/valuate.py --test                # 只跑前 2 只
  python3 scripts/valuate.py --phase 0             # 只刷新缓存
  python3 scripts/valuate.py --no-cache            # 强制跳过缓存检查
"""

import json
import os
import sys
import csv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import argparse
from datetime import datetime
from pathlib import Path
from collections import defaultdict

# ── 项目根目录（脚本在 scripts/ 下） ─────────────────────────
PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_FINANCIAL = PROJECT_ROOT / "cache" / "financial"
CACHE_BRIEFING = PROJECT_ROOT / "cache" / "briefing"
CACHE_RESEARCH = PROJECT_ROOT / "cache" / "research"
REPORTS_DIR = PROJECT_ROOT / "reports"
QUANT_DIR = PROJECT_ROOT / "quant"

# ── 字段中文名 → 候选关键词（用于 nameMap 模糊匹配） ──────────
FIELD_KEYWORDS = {
    "资产总计": ["资产总计", "资产总计(元)"],
    "营业收入": ["营业收入", "营业总收入"],
    "营收同比增速": ["营业总收入同比增长率", "营业收入同比增长率"],
    "归母净利润": ["归属于母公司股东的净利润"],
    "扣非净利润": ["扣除非经常性损益后归属于母公司股东的净利润",
                  "扣除非经常性损益净利润",
                  "扣除非经常性损益"],
    "扣非净利润增速": ["归属母公司股东的净利润同比增长率(扣除非经常性损益)",
                    "扣除非经常性损益同比增长率"],
    "经营现金流": ["经营活动产生的现金流量净额"],
    "ROE": ["净资产收益率ROE", "净资产收益率ROE(TTM)"],
    "资产负债率": ["资产负债率"],
    "固定资产净额": ["固定资产-净额(元)", "固定资产-净额"],
    "在建工程": ["在建工程"],
    "研发费用": ["研发费用"],
    "销售毛利率": ["销售毛利率"],
    "销售净利率": ["净利润/营业总收入", "净利润/营业总收入(销售净利率)"],
    "总市值": ["总市值"],
    "PE_TTM": ["市盈率PE(TTM)"],
    "PB": ["市净率PB"],
    "总股本": ["总股本"],
    "存货": ["存货"],
    "商誉": ["商誉", "商誉账面价值"],
    "有息负债": ["带息债务"],
    "股东权益": ["股东权益合计"],
    "折旧": ["折旧摊销"],
    "归母净利润增速": ["归属母公司股东的净利润同比增长率"],
    "一致预期归母净利润": ["预测归属于母公司的净利润中值"],
    "一致预期增速": ["预测归属于母公司的净利润增长率"],
}

# ── 工具函数 ──────────────────────────────────────────────

def find_raw_json_files(code):
    """在 cache/financial/ 中查找某只股票的所有 raw JSON 文件"""
    if not CACHE_FINANCIAL.exists():
        return []
    files = []
    for f in CACHE_FINANCIAL.iterdir():
        if f.name.endswith("_raw.json") and code in f.name:
            files.append(f)
    # 按文件大小降序（大的通常包含更多字段）
    files.sort(key=lambda x: x.stat().st_size, reverse=True)
    return files


def parse_mx_json(filepath):
    """解析单个 mx-data raw JSON 文件，返回 {中文名: {period: value}} 的字典"""
    result = {}
    try:
        with open(filepath, "r") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, KeyError):
        return result

    try:
        tables = raw["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
    except (KeyError, TypeError):
        return result

    for table in tables:
        nm = table.get("nameMap", {})
        rt = table.get("rawTable", {})
        if not isinstance(rt, dict):
            continue
        heads = rt.get("headName", [])

        # 构建 code → 中文名 映射
        code_to_name = {}
        for code, cname in nm.items():
            code_to_name[code] = cname

        for code, values in rt.items():
            if code == "headName":
                continue
            if not values:
                continue
            cname = code_to_name.get(code, code)
            if cname not in result:
                result[cname] = {}
            for j, period in enumerate(heads):
                if j < len(values) and values[j] and values[j] != "-":
                    result[cname][period] = values[j]

    return result


def lookup_field(parsed_data, keywords, prefer_period=None):
    """从解析结果中按关键词查找字段值。

    Args:
        parsed_data: parse_mx_json 的返回结果
        keywords: 候选字段名列表
        prefer_period: 优先取哪个报告期（如 '2025年报'），None 则取第一个

    Returns:
        (value_str, period) 或 (None, None)
    """
    for kw in keywords:
        for field_name, periods in parsed_data.items():
            if kw in field_name:
                if not periods:
                    continue
                # 按偏好排序：优先精确匹配 prefer_period，其次包含年份
                ordered = list(periods.items())
                if prefer_period:
                    def sort_key(x):
                        period_name = str(x[0])
                        if period_name == prefer_period:
                            return 0
                        if prefer_period in period_name:
                            return 1
                        # 年报优先于中报/季报
                        if "年报" in period_name:
                            return 2
                        return 3
                    ordered.sort(key=sort_key)
                return ordered[0][0], ordered[0][1]
    return None, None


def safe_float(value_str):
    """安全转 float"""
    if value_str is None:
        return None
    try:
        return float(str(value_str).replace(",", "").replace("%", ""))
    except (ValueError, TypeError):
        return None


def get_latest_period_data(parsed_data, keywords):
    """获取某个字段在所有周期中的数据，返回 {period: float_value}"""
    result = {}
    for kw in keywords:
        for field_name, periods in parsed_data.items():
            if kw in field_name:
                for period, val in periods.items():
                    fv = safe_float(val)
                    if fv is not None:
                        result[period] = fv
    return result


def collect_period_data(parsed_data, keywords):
    """收集某个字段的所有报告期数据，用于历史趋势分析"""
    data = {}
    for kw in keywords:
        for field_name, periods in parsed_data.items():
            if kw in field_name:
                for period, val in periods.items():
                    # 只取年报
                    if "年报" in period:
                        fv = safe_float(val)
                        if fv is not None:
                            data[period] = fv
    return data


# ── 核心计算函数 ──────────────────────────────────────────

def calc_financial_indicators(all_data):
    """从解析的财务数据计算标准化指标"""
    ind = {}

    # 基础财务字段 — 取最新可用数据（与模型一 LATEST 口径一致）
    pref = None  # None = lookup_field 返回第一个可用周期（通常最新）
    ind["总资产"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["资产总计"], pref)[1])
    ind["营业收入"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["营业收入"], pref)[1])
    ind["营收同比增速"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["营收同比增速"], pref)[1])
    ind["归母净利润"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["归母净利润"], pref)[1])
    ind["扣非净利润"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["扣非净利润"], pref)[1])
    ind["经营现金流"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["经营现金流"], pref)[1])
    ind["ROE"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["ROE"], pref)[1])
    ind["资产负债率"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["资产负债率"], pref)[1])
    ind["固定资产净额"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["固定资产净额"], pref)[1])
    ind["在建工程"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["在建工程"], pref)[1])
    ind["研发费用"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["研发费用"], pref)[1])
    ind["销售毛利率"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["销售毛利率"], pref)[1])
    ind["销售净利率"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["销售净利率"], pref)[1])
    ind["存货"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["存货"], pref)[1])
    ind["商誉"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["商誉"], pref)[1])
    ind["有息负债"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["有息负债"], pref)[1])
    ind["股东权益"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["股东权益"], pref)[1])
    ind["折旧"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["折旧"], pref)[1])
    ind["归母净利润增速"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["归母净利润增速"], pref)[1])
    ind["扣非净利润增速"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["扣非净利润增速"], pref)[1])

    # 估值指标 — 取最新
    ind["总市值"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["总市值"])[1])
    ind["PE_TTM"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["PE_TTM"])[1])
    ind["PB"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["PB"])[1])
    ind["总股本"] = safe_float(lookup_field(all_data, FIELD_KEYWORDS["总股本"])[1])

    # 一致预期
    consensus_val = lookup_field(all_data, FIELD_KEYWORDS["一致预期归母净利润"])
    ind["一致预期归母净利润"] = safe_float(consensus_val[1])
    consensus_growth = lookup_field(all_data, FIELD_KEYWORDS["一致预期增速"])
    ind["一致预期增速"] = safe_float(consensus_growth[1])

    # ── 一致预期质量验证 ──
    # mx-data 的"预测归母净利润"字段可能来自简单公式推算而非真实分析师一致预期。
    # 此处做三层检测，标记数据可靠性。
    consensus_quality = "verified"  # 默认可信
    consensus_warnings = []

    c_np = ind.get("一致预期归母净利润")
    c_growth = ind.get("一致预期增速")
    actual_np = ind.get("归母净利润")
    actual_growth = ind.get("归母净利润增速")

    # 检测1: 字段缺失
    if c_np is None and c_growth is None:
        consensus_quality = "missing"
        consensus_warnings.append("无一致预期数据")
    elif c_np is None:
        consensus_quality = "missing"
        consensus_warnings.append("一致预期净利润缺失")

    if consensus_quality != "missing" and c_np is not None:
        # 检测2: 公式推算检测 — 如果 forecast ≈ actual × (1 + growth)，
        #     说明数据源只是用当期值×增速做了机械推算，不是真实分析师预测
        if actual_np and actual_np > 0 and c_growth is not None:
            formula_np = actual_np * (1 + c_growth / 100)
            if formula_np > 0:
                deviation_pct = abs(c_np - formula_np) / formula_np * 100
                if deviation_pct < 3.0:
                    consensus_quality = "auto_generated"
                    consensus_warnings.append(
                        f"疑似公式推算: forecast={c_np:.2f} ≈ actual={actual_np:.2f}×(1+{c_growth:.1f}%)={formula_np:.2f}, 偏差{deviation_pct:.1f}%"
                    )

        # 检测3: 增速复制检测 — 如果一致预期增速与当期实际增速高度一致，
        #     可能是数据源直接把当期增速当作预期增速
        if actual_growth and c_growth is not None and abs(c_growth - actual_growth) < 2.0:
            if consensus_quality == "verified":
                consensus_quality = "auto_generated"
            consensus_warnings.append(
                f"预期增速{c_growth:.1f}%与当期实际增速{actual_growth:.1f}%高度一致, 疑似复制"
            )

        # 检测4: 预期增速超过200% — 通常是异常数据或数据源错误
        if c_growth is not None and abs(c_growth) > 200:
            consensus_quality = "auto_generated"
            consensus_warnings.append(f"预期增速{c_growth:.1f}%异常(>200%), 不可靠")

    # 质量判定后处理: auto_generated → 置null, 不做伪数据
    if consensus_quality == "auto_generated":
        ind["一致预期归母净利润"] = None
        ind["一致预期增速"] = None
    elif consensus_quality == "missing":
        ind["一致预期归母净利润"] = None
        ind["一致预期增速"] = None

    ind["_consensus_quality"] = consensus_quality
    ind["_consensus_warnings"] = consensus_warnings

    # 历史毛利率（跨多个周期）
    margin_data = get_latest_period_data(all_data, FIELD_KEYWORDS["销售毛利率"])
    ind["毛利率历史"] = margin_data

    # 历史归母净利润（跨多个周期）
    profit_data = collect_period_data(all_data, FIELD_KEYWORDS["归母净利润"])
    ind["归母净利润历史"] = profit_data

    # ── 计算衍生指标 ──

    # OCF/NP
    if ind["归母净利润"] and ind["归母净利润"] > 0:
        ind["OCF_NP"] = round(ind["经营现金流"] / ind["归母净利润"], 4) if ind["经营现金流"] else None
    else:
        ind["OCF_NP"] = None

    # 应收账款/营收（从账龄表获取，暂用简化方式）
    ind["应收_营收"] = None  # 需要单独解析账龄表

    # 存货/营业成本（营业成本未直接拉取，暂用简化方式）
    ind["存货_COGS"] = None

    # 商誉/净资产
    if ind["股东权益"] and ind["股东权益"] > 0:
        ind["商誉_净资产"] = round(ind["商誉"] / ind["股东权益"], 4) if ind["商誉"] else 0.0
    else:
        ind["商誉_净资产"] = None

    # 有息负债/总资产
    if ind["总资产"] and ind["总资产"] > 0:
        ind["有息负债_总资产"] = round(ind["有息负债"] / ind["总资产"], 4) if ind["有息负债"] else None
    else:
        ind["有息负债_总资产"] = None

    # 固定资产/总资产
    if ind["总资产"] and ind["总资产"] > 0:
        ind["固定资产_总资产"] = round(ind["固定资产净额"] / ind["总资产"], 4) if ind["固定资产净额"] else None
    else:
        ind["固定资产_总资产"] = None

    # 非经常性损益占比
    if ind["归母净利润"] and ind["归母净利润"] > 0 and ind["扣非净利润"]:
        irreg = ind["归母净利润"] - ind["扣非净利润"]
        ind["非经常性损益"] = round(irreg, 2)
        ind["非经常性占比"] = round(irreg / ind["归母净利润"], 4)
    else:
        ind["非经常性损益"] = None
        ind["非经常性占比"] = None

    # 在建工程/总资产
    if ind["总资产"] and ind["总资产"] > 0:
        ind["在建工程_总资产"] = round(ind["在建工程"] / ind["总资产"], 4) if ind["在建工程"] else 0.0
    else:
        ind["在建工程_总资产"] = None

    # 研发费用率
    if ind["营业收入"] and ind["营业收入"] > 0:
        ind["研发费用率"] = round(ind["研发费用"] / ind["营业收入"], 4) if ind["研发费用"] else None
    else:
        ind["研发费用率"] = None

    # 近3年毛利率波动
    margin_vals = [v for v in margin_data.values()]
    if len(margin_vals) >= 3:
        ind["毛利率_3年波动"] = round(max(margin_vals) - min(margin_vals), 4)
    elif len(margin_vals) >= 2:
        # 尝试从不同字段补充
        all_margins = get_latest_period_data(all_data, FIELD_KEYWORDS["销售毛利率"])
        mvs = list(all_margins.values())
        if len(mvs) >= 3:
            ind["毛利率_3年波动"] = round(max(mvs) - min(mvs), 4)
        elif len(mvs) >= 2:
            ind["毛利率_3年波动"] = round(max(mvs) - min(mvs), 4)  # 至少2年
        else:
            ind["毛利率_3年波动"] = None
    else:
        ind["毛利率_3年波动"] = None

    return ind


def calc_decision_tree(indicators):
    """计算估值决策树五信号

    返回: {Q1, Q1b, Q2, Q3, Q_IRREG, route}
    """
    signals = {}

    # Q1: 有稳定利润？
    np = indicators.get("归母净利润") or 0
    ocf_np = indicators.get("OCF_NP") or 0
    signals["Q1"] = np > 0 and ocf_np > 0.5

    # Q1b: 有高增长营收？（仅 Q1=false 时使用）
    rev = indicators.get("营业收入") or 0
    rev_growth = indicators.get("营收同比增速") or 0
    signals["Q1b"] = rev > 0 and rev_growth > 30

    # Q2: 资产轻重
    fixed_ratio = indicators.get("固定资产_总资产") or 0
    if fixed_ratio > 0.40:
        signals["Q2"] = "重"
    elif fixed_ratio < 0.20:
        signals["Q2"] = "轻"
    else:
        signals["Q2"] = "中"

    # Q3: 利润稳定性
    margin_vol = indicators.get("毛利率_3年波动")
    if margin_vol is not None:
        signals["Q3"] = "稳定" if margin_vol < 5 else "波动"
    else:
        signals["Q3"] = "未知"

    # Q_IRREG: 重大非经常性损益
    irreg_ratio = indicators.get("非经常性占比") or 0
    signals["Q_IRREG"] = irreg_ratio > 0.30

    # 路线分派
    if signals["Q1"]:
        if signals["Q2"] == "重" and signals["Q_IRREG"]:
            signals["route"] = "A1"
            signals["route_name"] = "分部估值（核心+非经常性）"
        elif signals["Q2"] == "重":
            signals["route"] = "A"
            signals["route_name"] = "EV/EBITDA + 隐含PE验证"
        elif signals["Q2"] == "轻":
            signals["route"] = "B"
            signals["route_name"] = "PE + P/FCF验证"
        elif signals["Q3"] == "稳定":
            signals["route"] = "C"
            signals["route_name"] = "PE + DCF交叉验证"
        else:
            signals["route"] = "D"
            signals["route_name"] = "PB + 周期调整PE"
    else:
        if signals["Q1b"]:
            signals["route"] = "E"
            signals["route_name"] = "PS + PEG"
        else:
            signals["route"] = "F"
            signals["route_name"] = "PB 净资产重估"

    return signals


def calc_funnel(indicators):
    """计算预期差漏斗信号和搜索优先级

    返回: [{direction, priority, reason}]
    """
    funnel = []

    cip_ratio = indicators.get("在建工程_总资产") or 0
    rd_ratio = indicators.get("研发费用率") or 0
    rev_growth = indicators.get("营收同比增速") or 0
    irreg_ratio = indicators.get("非经常性占比") or 0

    # 产能扩张
    if cip_ratio > 0.10:
        funnel.append({"direction": "产能扩张", "priority": "高",
                       "reason": f"在建工程/总资产={cip_ratio*100:.1f}% > 10%"})
    elif cip_ratio > 0.05:
        funnel.append({"direction": "产能扩张", "priority": "中",
                       "reason": f"在建工程/总资产={cip_ratio*100:.1f}% 5-10%"})

    # 技术突破
    if rd_ratio > 0.15 and rev_growth < 20:
        funnel.append({"direction": "技术突破", "priority": "高",
                       "reason": f"研发费用率={rd_ratio*100:.1f}% > 15% 且 营收增速={rev_growth:.1f}% < 20%"})
    elif rd_ratio > 0.08:
        funnel.append({"direction": "技术突破", "priority": "中",
                       "reason": f"研发费用率={rd_ratio*100:.1f}% > 8%"})

    # 海外拓展（海外营收占比需手动补充，此处仅占位）
    # funnel信号中海外拓展需要从营收构成中提取，暂不自动判定

    # 重大非经常性
    if irreg_ratio > 0.30:
        funnel.append({"direction": "重大非经常性/资本运作", "priority": "高",
                       "reason": f"非经常性损益占比={irreg_ratio*100:.1f}% > 30%"})

    return funnel


# ── 简报册生成 ──────────────────────────────────────────

def build_briefing(code, name, indicators, signals, funnel, data_date="2025-12-31"):
    """构建结构化简报册 JSON"""
    today = datetime.now().strftime("%y%m%d")

    briefing = {
        "_meta": {
            "code": code,
            "name": name,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "data_date": data_date,
            "version": "1.0-dev",
        },
        "financials": {
            "revenue": indicators.get("营业收入"),
            "revenue_growth": indicators.get("营收同比增速"),
            "net_profit": indicators.get("归母净利润"),
            "net_profit_growth": indicators.get("归母净利润增速"),
            "deducted_profit": indicators.get("扣非净利润"),
            "deducted_profit_growth": indicators.get("扣非净利润增速"),
            "operating_cf": indicators.get("经营现金流"),
            "roe": indicators.get("ROE"),
            "debt_ratio": indicators.get("资产负债率"),
            "total_assets": indicators.get("总资产"),
            "fixed_assets": indicators.get("固定资产净额"),
            "cip": indicators.get("在建工程"),
            "rd_expense": indicators.get("研发费用"),
            "inventory": indicators.get("存货"),
            "goodwill": indicators.get("商誉"),
            "interest_bearing_debt": indicators.get("有息负债"),
            "equity": indicators.get("股东权益"),
            "depreciation": indicators.get("折旧"),
            "gross_margin": indicators.get("销售毛利率"),
            "net_margin": indicators.get("销售净利率"),
            "gross_margin_history": indicators.get("毛利率历史", {}),
            "net_profit_history": indicators.get("归母净利润历史", {}),
        },
        "valuation_snapshot": {
            "market_cap": indicators.get("总市值"),
            "pe_ttm": indicators.get("PE_TTM"),
            "pb": indicators.get("PB"),
            "total_shares": indicators.get("总股本"),
        },
        "consensus": {
            "net_profit_forecast": indicators.get("一致预期归母净利润"),
            "growth_forecast": indicators.get("一致预期增速"),
            "quality": indicators.get("_consensus_quality", "unknown"),
            "warnings": indicators.get("_consensus_warnings", []),
        },
        "derived_indicators": {
            "ocf_np": indicators.get("OCF_NP"),
            "ar_revenue": indicators.get("应收_营收"),
            "inventory_cogs": indicators.get("存货_COGS"),
            "goodwill_equity": indicators.get("商誉_净资产"),
            "debt_assets": indicators.get("有息负债_总资产"),
            "fixed_assets_ratio": indicators.get("固定资产_总资产"),
            "margin_3y_volatility": indicators.get("毛利率_3年波动"),
            "irregular_ratio": indicators.get("非经常性占比"),
            "irregular_amount": indicators.get("非经常性损益"),
            "cip_ratio": indicators.get("在建工程_总资产"),
            "rd_ratio": indicators.get("研发费用率"),
        },
        "decision_tree": signals,
        "funnel": funnel,
    }

    return briefing


# ── 进度管理 ──────────────────────────────────────────

def read_index():
    """读取 _index.csv"""
    index_path = REPORTS_DIR / "_index.csv"
    if not index_path.exists():
        return []
    rows = []
    with open(index_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def read_quant_pool(date_str=None):
    """读取 quant 精选池"""
    if date_str is None:
        date_str = datetime.now().strftime("%y%m%d")
    quant_path = QUANT_DIR / f"quant_{date_str}.csv"
    if not quant_path.exists():
        # 尝试最近的
        quants = sorted(QUANT_DIR.glob("quant_*.csv"))
        if quants:
            quant_path = quants[-1]
        else:
            return []

    stocks = []
    with open(quant_path, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            code = row.get("股票代码", "").replace('="', "").replace('"', "")
            name = row.get("股票名称", "")
            stocks.append({"code": code, "name": name})
    return stocks


def get_stock_name_from_index(code):
    """从 _index.csv 获取股票名称"""
    rows = read_index()
    for row in rows:
        idx_code = row.get("股票代码", "").replace('="', "").replace('"', "")
        if idx_code == code:
            return row.get("股票名称", code)
    return code


# ── 主流程 ──────────────────────────────────────────────

def process_stock(code, name=None, force_refresh=False):
    """处理单只标的的完整阶段零流程"""
    if name is None:
        name = get_stock_name_from_index(code)

    print(f"\n{'='*60}")
    print(f"处理: {code} {name}")

    # 0.2 读取并解析所有财务缓存文件
    raw_files = find_raw_json_files(code)
    if not raw_files:
        print(f"  ⚠️ 无缓存文件，请先通过 mx-data skill 拉取数据")
        return None

    # 检查缓存文件时效：超过 90 天的财报缓存标记为可疑
    now = datetime.now()
    stale_threshold_days = 90
    stale_files = []
    for rf in raw_files:
        mtime = datetime.fromtimestamp(rf.stat().st_mtime)
        if (now - mtime).days > stale_threshold_days:
            stale_files.append(rf.name)
    if stale_files:
        print(f"  ⚠️ {len(stale_files)} 个缓存文件超过 {stale_threshold_days} 天，建议重新拉取:")
        for sf in stale_files[:5]:
            print(f"    - {sf[-60:]}")

    all_data = {}
    for rf in raw_files:
        print(f"  解析: {rf.name}")
        parsed = parse_mx_json(rf)
        # 合并字段：保留所有报告期，不覆盖已有数据
        for fname, periods in parsed.items():
            if fname not in all_data:
                all_data[fname] = {}
            for period, val in periods.items():
                if period not in all_data[fname]:
                    all_data[fname][period] = val
                # 如果已有同周期数据，保留先加载的（文件按大小降序，大的=更全的先加载）

    print(f"  共提取 {len(all_data)} 个字段")

    # 0.3 计算标准化指标
    indicators = calc_financial_indicators(all_data)

    # 0.3.1 决策树信号
    signals = calc_decision_tree(indicators)
    print(f"  决策树: {signals['route']} ({signals['route_name']})")
    print(f"    Q1={signals['Q1']} Q2={signals['Q2']} Q3={signals['Q3']} Q_IRREG={signals['Q_IRREG']}")

    # 0.4 预期差漏斗
    funnel = calc_funnel(indicators)
    for f_item in funnel:
        print(f"  漏斗: [{f_item['priority']}] {f_item['direction']} — {f_item['reason']}")

    # 0.6 生成简报册
    briefing = build_briefing(code, name, indicators, signals, funnel)

    # 写入缓存
    CACHE_BRIEFING.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%y%m%d")
    briefing_path = CACHE_BRIEFING / f"{code}_{today}.json"
    with open(briefing_path, "w", encoding="utf-8") as f:
        json.dump(briefing, f, ensure_ascii=False, indent=2)
    print(f"  ✅ 简报册: {briefing_path}")

    return briefing


def print_summary(results):
    """打印处理摘要"""
    successful = [r for r in results if r is not None]
    print(f"\n{'='*60}")
    print(f"阶段零完成: {len(successful)}/{len(results)} 只标的")
    print(f"简报册目录: {CACHE_BRIEFING}")

    for b in successful:
        meta = b["_meta"]
        sig = b["decision_tree"]
        print(f"  {meta['code']} {meta['name']}: 路线 {sig['route']} "
              f"(Q1={sig['Q1']}, Q2={sig['Q2']}, Q_IRREG={sig['Q_IRREG']})")


# ── CLI ────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="模型三 阶段零 数据准备脚本")
    parser.add_argument("--code", type=str, help="单只股票代码")
    parser.add_argument("--test", action="store_true", help="只处理前2只")
    parser.add_argument("--phase", type=str, default="0", help="阶段号（目前仅支持0）")
    parser.add_argument("--no-cache", action="store_true", help="清除旧简报缓存后重新生成（财务缓存不受影响）")
    parser.add_argument("--pool", type=str, help="pool CSV 路径")
    parser.add_argument("--quant", type=str, help="quant CSV 路径")
    args = parser.parse_args()

    # 确定要处理的标的列表
    if args.code:
        stocks = [{"code": args.code, "name": get_stock_name_from_index(args.code)}]
    else:
        # 从 _index.csv 读取
        index_rows = read_index()
        if index_rows:
            stocks = []
            for row in index_rows:
                code = row.get("股票代码", "").replace('="', "").replace('"', "")
                stocks.append({"code": code, "name": row.get("股票名称", code)})
        else:
            # 从 quant 池初始化
            stocks = read_quant_pool()
            if not stocks:
                print("❌ 无标的可处理。请先运行模型二，或使用 --code 指定单只标的。")
                sys.exit(1)

    if args.test:
        stocks = stocks[:2]

    # --no-cache: 清除旧简报缓存
    if args.no_cache:
        cleaned = 0
        if CACHE_BRIEFING.exists():
            today = datetime.now().strftime("%y%m%d")
            for f in CACHE_BRIEFING.iterdir():
                if f.suffix == ".json" and today not in f.name:
                    f.unlink()
                    cleaned += 1
        print(f"🧹 --no-cache: 已清除 {cleaned} 个旧简报缓存")

    # 自动清理 cache/financial/ 中的无用文件（valuate.py 只读 _raw.json，其余为 mx-data skill 副产品）
    fin_cleaned = 0
    if CACHE_FINANCIAL.exists():
        for f in CACHE_FINANCIAL.iterdir():
            if f.suffix in (".xlsx", ".txt"):
                f.unlink()
                fin_cleaned += 1
    if fin_cleaned:
        print(f"🧹 已清除 cache/financial/ 中 {fin_cleaned} 个无用文件(.xlsx/.txt)")

    print(f"待处理标的: {len(stocks)} 只")
    for s in stocks:
        print(f"  {s['code']} {s['name']}")

    results = []
    for stock in stocks:
        result = process_stock(stock["code"], stock["name"])
        results.append(result)

    print_summary(results)


if __name__ == "__main__":
    main()
