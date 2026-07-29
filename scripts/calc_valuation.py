#!/usr/bin/env python3
"""
calc_valuation.py — 模型三估值计算引擎
对应指令卡: instructions/03-valuation.md

职责：接收 LLM 提供的结构化参数，执行确定性估值计算。
LLM 做判断（提供参数），脚本做计算（输出数字）。

用法:
  python3 scripts/calc_valuation.py <params.json>          # 从文件读取参数
  python3 scripts/calc_valuation.py --stdin                 # 从 stdin 读取参数
  python3 scripts/valuate.py --code 300442 --calc <params.json>  # 通过 valuate.py 调用
"""

import json
import sys
import os
import csv
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import VALUATION_RANKING_PATH

# ── 项目路径 ──────────────────────────────────────────
PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CACHE_BRIEFING = PROJECT_ROOT / "cache" / "briefing"
CACHE_CALC_PARAMS = PROJECT_ROOT / "cache" / "calc_params"
CACHE_CALC_RESULTS = PROJECT_ROOT / "cache" / "calc_results"
RANKING_PATH = Path(VALUATION_RANKING_PATH)

# ══════════════════════════════════════════════════════
# 常量表（与指令卡 §2.5 / 参考手册 同步）
# ══════════════════════════════════════════════════════

# PEG 上限 — 按增长质量分档
PEG_CAP = {
    "structural": 1.5,
    "cyclical": 1.0,
    "mature": 0.7,
}

# 市场地位 → 折扣系数（用于悲观估值）
MARKET_DISCOUNT = {
    "leader": 1.0,
    "mid": 0.85,
    "follower": 0.70,
}

# 悲观情景参数 — 增长质量决定不确定性在哪端
PESSIMISTIC_PE_DISCOUNT = {
    "structural": 0.90,   # 只压 PE（情绪波动 > 基本面波动）
    "cyclical": 0.85,      # 两端各压
    "mature": 1.00,        # 不压 PE（基本面风险 > 估值波动）
}

# 定性调整 — 每项幅度限制
QUAL_ADJUSTMENT_PER_ITEM_MIN = -0.10
QUAL_ADJUSTMENT_PER_ITEM_MAX = 0.10
QUAL_ADJUSTMENT_TOTAL_MIN = -0.20
QUAL_ADJUSTMENT_TOTAL_MAX = 0.20

# 路线级悲观/乐观折扣系数
PESSIMISTIC_PB_DISCOUNT = 0.85          # 路线 D: PB 保守折扣
PESSIMISTIC_PS_DISCOUNT = 0.80          # 路线 E: PS 保守折扣
PESSIMISTIC_EV_EBITDA_DISCOUNT = 0.85   # 路线 A: EV/EBITDA 保守折扣
OPTIMISTIC_EV_EBITDA_PREMIUM = 1.10     # 路线 A: EV/EBITDA 乐观溢价

# PE 增速比率上限 — 防止极端增速差导致 PE 过度放大
# 公司增速/可比增速 > 2.0 时，按 2.0 计算（增速 4x 不代表 PE 应该 4x）
GROWTH_RATIO_CAP = 2.0

# Type B 非经常性 PE 分档（路线 A1）
TYPE_B_PE = {"A": (5, 8), "B": (8, 15), "C": (0.6, 0.8)}  # C 类 = 核心PE × 系数

# 路线 C DCF 参数
WACC_RANGE = (0.08, 0.12)
TERMINAL_GROWTH_RANGE = (0.02, 0.03)

# 下行风险价公式
DOWNSIDE_RISK_RATIO = 0.80  # 下行风险价 = 悲观估值 × 0.8

# 分歧度分档
DIVERGENCE_LOW = 20.0
DIVERGENCE_HIGH = 50.0

# 反向检查触发阈值
REVERSE_CHECK_DISCOUNT_HIGH = 2.0
REVERSE_CHECK_DISCOUNT_LOW = 0.3
REVERSE_CHECK_DISCOUNT_LEADER = 1.5
REVERSE_CHECK_DISCOUNT_EXTRAP = 0.4
REVERSE_CHECK_VALUATION_WIDTH = 5.0  # 估值区间宽度 > 5x 触发

# ══════════════════════════════════════════════════════
# 参数解析与验证
# ══════════════════════════════════════════════════════

def parse_params(raw: dict) -> dict:
    """解析并验证 LLM 输入的参数 JSON。

    返回规范化后的参数字典。缺失可选字段填充默认值。
    必填字段缺失时抛出 ValueError，附带清晰的错误信息。
    """
    errors = []

    # ── meta ──
    meta = raw.get("meta", {})
    code = meta.get("code", "")
    name = meta.get("name", "")
    total_shares = meta.get("total_shares")
    current_price = meta.get("current_price")

    if not code:
        errors.append("meta.code 缺失")
    if total_shares is None:
        errors.append("meta.total_shares 缺失")
    if current_price is None:
        errors.append("meta.current_price 缺失")

    # ── 增长特征 ──
    growth_quality = raw.get("growth_quality", "cyclical")
    if growth_quality not in ("structural", "cyclical", "mature"):
        errors.append(f"growth_quality 无效: '{growth_quality}'，应为 structural|cyclical|mature")

    market_position = raw.get("market_position", "mid")
    if market_position not in ("leader", "mid", "follower"):
        errors.append(f"market_position 无效: '{market_position}'，应为 leader|mid|follower")

    # ── 可比公司 ──
    comparable_pe_median = raw.get("comparable_pe_median")
    comparable_pe_lower = raw.get("comparable_pe_lower")
    comparable_pe_upper = raw.get("comparable_pe_upper")

    # ── 增长率 ──
    company_growth_rate = raw.get("company_growth_rate", 0)
    comp_growth_rate = raw.get("comp_growth_rate", 0)

    # ── 定性调整 ──
    qualitative_adjustments = raw.get("qualitative_adjustments", [])

    # ── 支柱 ──
    pillars_raw = raw.get("pillars", [])
    if not pillars_raw:
        errors.append("pillars 为空，至少需要一个业务支柱")

    # ── 可选覆盖 ──
    pe_override = raw.get("pe_override")
    pb_override = raw.get("pb_override")

    # ── 路线 A 可选参数 ──
    ebitda_2026e = raw.get("ebitda_2026e")
    ebitda_2027e = raw.get("ebitda_2027e")
    comparable_ev_ebitda = raw.get("comparable_ev_ebitda")
    net_debt = raw.get("net_debt")

    # ── 路线 C 可选参数 ──
    wacc = raw.get("wacc")
    terminal_growth = raw.get("terminal_growth")
    roe = raw.get("roe")
    book_value_per_share = raw.get("book_value_per_share")

    if errors:
        raise ValueError("参数校验失败:\n  - " + "\n  - ".join(errors))

    # ── 规范化支柱 ──
    pillars = []
    for i, p in enumerate(pillars_raw):
        pillar = {
            "name": p.get("name", f"支柱{i+1}"),
            "route": p.get("route", "B"),
            "consensus_np_2026e": p.get("consensus_np_2026e"),
            "consensus_np_2027e": p.get("consensus_np_2027e"),
            "consensus_np_lower_2026e": p.get("consensus_np_lower_2026e"),
            "consensus_np_upper_2026e": p.get("consensus_np_upper_2026e"),
            "consensus_np_lower_2027e": p.get("consensus_np_lower_2027e"),
            "consensus_np_upper_2027e": p.get("consensus_np_upper_2027e"),
            "deducted_np": p.get("deducted_np"),
            "layer2": p.get("layer2"),
            "layer3_items": p.get("layer3_items", []),
            "type_b_pipeline": p.get("type_b_pipeline", []),
            # 路线 D/F 用
            "net_assets": p.get("net_assets"),      # 净资产（亿元）
            "roe_pillar": p.get("roe"),               # 该支柱 ROE
            # 路线 E 用
            "revenue": p.get("revenue"),              # 营收（亿元），兼容旧参数
            "revenue_2026e": p.get("revenue_2026e"),
            "revenue_2027e": p.get("revenue_2027e"),
            "gross_margin": p.get("gross_margin"),    # 毛利率(%)
            # 路线 A/D/E/F 的可比参数允许按支柱提供，避免多个业务线共用一组全局参数。
            "ebitda_2026e": p.get("ebitda_2026e"),
            "ebitda_2027e": p.get("ebitda_2027e"),
            "comparable_ev_ebitda": p.get("comparable_ev_ebitda"),
            "net_debt": p.get("net_debt"),
            "comparable_pb_median": p.get("comparable_pb_median"),
            "comparable_pb_lower": p.get("comparable_pb_lower"),
            "comparable_pb_upper": p.get("comparable_pb_upper"),
            "comparable_roe": p.get("comparable_roe"),
            "comparable_ps_median": p.get("comparable_ps_median"),
            "comparable_ps_lower": p.get("comparable_ps_lower"),
            "comparable_ps_upper": p.get("comparable_ps_upper"),
            "comparable_gross_margin": p.get("comparable_gross_margin"),
        }
        pillars.append(pillar)

    params = {
        "meta": {"code": code, "name": name, "total_shares": total_shares,
                  "current_price": current_price, "price_date": meta.get("price_date"),
                  "price_source": meta.get("price_source")},
        "growth_quality": growth_quality,
        "market_position": market_position,
        "comparable_pe_median": comparable_pe_median,
        "comparable_pe_lower": comparable_pe_lower,
        "comparable_pe_upper": comparable_pe_upper,
        "comparable_pb_median": raw.get("comparable_pb_median"),
        "comparable_pb_lower": raw.get("comparable_pb_lower"),
        "comparable_pb_upper": raw.get("comparable_pb_upper"),
        "comparable_ps_median": raw.get("comparable_ps_median"),
        "comparable_ps_lower": raw.get("comparable_ps_lower"),
        "comparable_ps_upper": raw.get("comparable_ps_upper"),
        "company_growth_rate": company_growth_rate,
        "comp_growth_rate": comp_growth_rate,
        "qualitative_adjustments": qualitative_adjustments,
        "pillars": pillars,
        "pe_override": pe_override,
        "pb_override": pb_override,
        "ebitda_2026e": ebitda_2026e,
        "ebitda_2027e": ebitda_2027e,
        "comparable_ev_ebitda": comparable_ev_ebitda,
        "net_debt": net_debt,
        "wacc": wacc,
        "terminal_growth": terminal_growth,
        "roe": roe,
        "book_value_per_share": book_value_per_share,
        "institution_consensus_2026e": raw.get("institution_consensus_2026e", []),
        "institution_consensus_2027e": raw.get("institution_consensus_2027e", []),
    }

    return params


# ══════════════════════════════════════════════════════
# PE 三步走定价
# ══════════════════════════════════════════════════════

def calc_pe_three_step(comparable_median, comparable_lower, comparable_upper,
                        company_growth, comp_growth, growth_quality,
                        qualitative_adjustments):
    """PE 三步走定价 — 指令卡 §2.5 核心公式。

    Args:
        comparable_median: 可比PE中值
        comparable_lower: 可比PE下沿
        comparable_upper: 可比PE上沿
        company_growth: 公司净利润增速 (%)
        comp_growth: 可比公司净利润增速 (%)
        growth_quality: "structural" | "cyclical" | "mature"
        qualitative_adjustments: [{item, adjustment}]，adjustment 如 0.10=+10%

    Returns:
        dict: {final_pe, pessimistic_pe, optimistic_pe, step2_adjusted,
               step2_peg_cap, step2_clamped, step3_qual_total, details}
    """
    if any(v is None for v in [comparable_median, comparable_lower, comparable_upper]):
        return {"error": "可比PE参数不完整", "final_pe": None}

    # ── 第1步: 可比区间 ──
    step1 = {
        "median": comparable_median,
        "lower": comparable_lower,
        "upper": comparable_upper,
    }

    # ── 第2步: 增速调整 ──
    safe_comp_growth = comp_growth if comp_growth and comp_growth > 0 else 0.1
    growth_ratio = company_growth / safe_comp_growth if company_growth > 0 else 1.0
    growth_ratio = min(growth_ratio, GROWTH_RATIO_CAP)  # 上限防止极端值
    step2_adjusted = comparable_median * growth_ratio

    peg_cap_ratio = PEG_CAP.get(growth_quality, 1.0)
    step2_peg_cap = peg_cap_ratio * company_growth if company_growth > 0 else comparable_upper

    # clamp: [comparable_lower, PEG上限]
    step2_clamped = max(comparable_lower, min(step2_adjusted, step2_peg_cap))

    # ── 第3步: 定性调整 ──
    step3_qual_total = sum(adj.get("adjustment", 0) for adj in qualitative_adjustments)
    step3_qual_total = max(QUAL_ADJUSTMENT_TOTAL_MIN, min(step3_qual_total, QUAL_ADJUSTMENT_TOTAL_MAX))

    adjusted_before_clamp = step2_clamped * (1 + step3_qual_total)
    final_pe = max(comparable_lower, min(adjusted_before_clamp, step2_peg_cap))

    # ── 悲观 PE ──
    pe_discount = PESSIMISTIC_PE_DISCOUNT.get(growth_quality, 0.85)
    pessimistic_pe = final_pe * pe_discount

    # ── 乐观 PE ──
    # 乐观PE取 comparable_upper 和 final_pe×1.2 的较大者，不低于 final_pe
    optimistic_pe = max(comparable_upper, final_pe * 1.2)

    # ── 人类可读细节 ──
    peg_val = final_pe / company_growth if company_growth and company_growth > 0 else None
    details = (
        f"PE三步走:\n"
        f"  第1步: 可比PE区间 [{comparable_lower}x, {comparable_upper}x], 中枢 {comparable_median}x\n"
        f"  第2步: 增速比 = min({company_growth}/{safe_comp_growth}, {GROWTH_RATIO_CAP}) = {growth_ratio:.2f}\n"
        f"         增速调整 = {comparable_median} × {growth_ratio:.2f} = {step2_adjusted:.1f}x\n"
        f"         PEG上限({growth_quality} {peg_cap_ratio}) = {peg_cap_ratio} × {company_growth} = {step2_peg_cap:.1f}x\n"
        f"         clamp({step2_adjusted:.1f}, {comparable_lower}, {step2_peg_cap:.1f}) = {step2_clamped:.1f}x\n"
        f"  第3步: 定性调整合计 = {step3_qual_total:+.0%}\n"
        f"         {step2_clamped:.1f} × (1{step3_qual_total:+.0%}) = {adjusted_before_clamp:.1f}x\n"
        f"         clamp({adjusted_before_clamp:.1f}, {comparable_lower}, {step2_peg_cap:.1f}) = {final_pe:.1f}x\n"
        f"  悲观PE = {final_pe:.1f} × {pe_discount} = {pessimistic_pe:.1f}x\n"
        f"  乐观PE = max({comparable_upper}, {final_pe:.1f}×1.2) = {optimistic_pe:.1f}x"
    )
    if peg_val is not None:
        details += f"\n  PEG = {final_pe:.1f}/{company_growth} = {peg_val:.2f}"

    return {
        "final_pe": round(final_pe, 2),
        "pessimistic_pe": round(pessimistic_pe, 2),
        "optimistic_pe": round(optimistic_pe, 2),
        "step1_comparable_range": step1,
        "step2_adjusted": round(step2_adjusted, 2),
        "step2_peg_cap": round(step2_peg_cap, 2),
        "step2_clamped": round(step2_clamped, 2),
        "step3_qual_total": round(step3_qual_total, 4),
        "details": details,
    }


# ══════════════════════════════════════════════════════
# 悲观情景参数
# ══════════════════════════════════════════════════════

def calc_pessimistic_params(growth_quality, base_pe, consensus_np,
                             consensus_np_lower, deducted_np):
    """根据增长质量计算悲观情景的 PE 和利润。

    Returns:
        dict: {pessimistic_pe, pessimistic_np, rule_description}
    """
    pe_discount = PESSIMISTIC_PE_DISCOUNT.get(growth_quality, 0.85)
    pessimistic_pe = base_pe * pe_discount

    if growth_quality == "structural":
        # 结构性成长: 只压PE不压利润（订单和产能是实物，不会因情绪消失）
        pessimistic_np = consensus_np
        rule_desc = f"结构性成长 → PE×{pe_discount}={pessimistic_pe:.1f}x，利润不变({consensus_np}亿)"
    elif growth_quality == "cyclical":
        # 周期性: 两端各压
        pessimistic_np = consensus_np_lower if consensus_np_lower else consensus_np
        rule_desc = f"周期性 → PE×{pe_discount}={pessimistic_pe:.1f}x，利润→共识下沿({pessimistic_np}亿)"
    else:
        # 成熟: 只压利润不压PE
        pessimistic_np = max(
            deducted_np if deducted_np else 0,
            consensus_np_lower if consensus_np_lower else consensus_np
        )
        pessimistic_pe = base_pe  # PE 不压
        rule_desc = f"成熟/衰退 → PE不变({base_pe}x)，利润→max(扣非,共识下沿)=({pessimistic_np}亿)"

    return {
        "pessimistic_pe": round(pessimistic_pe, 2),
        "pessimistic_np": pessimistic_np,
        "rule_description": rule_desc,
    }


# ══════════════════════════════════════════════════════
# 路线估值公式
# ══════════════════════════════════════════════════════

def _market_value_to_per_share(market_value, total_shares):
    """市值(亿) → 每股(元)"""
    if not total_shares or total_shares <= 0:
        return None
    return round(market_value / total_shares, 2)


def calc_route_b_valuation(pillar, pe_result, total_shares, year="2026e"):
    """路线 B: PE + P/FCF（轻资产）

    市值 = 共识净利 × PE × 折扣系数
    """
    np_key = f"consensus_np_{year}"
    np = pillar.get(np_key)
    if np is None:
        return {"error": f"路线B缺少{np_key}"}

    final_pe = pe_result["final_pe"]
    pessimistic_pe = pe_result["pessimistic_pe"]
    optimistic_pe = pe_result["optimistic_pe"]

    discount = MARKET_DISCOUNT.get(pe_result.get("market_position", "mid"), 0.85)

    # 三情景利润
    np_pessimistic = pillar.get(f"consensus_np_lower_{year}") or np
    np_optimistic = pillar.get(f"consensus_np_upper_{year}") or np

    # 悲观: 用悲观PE × 悲观利润 × 折扣系数
    pess_result = calc_pessimistic_params(
        pe_result.get("growth_quality", "cyclical"),
        final_pe,
        np,
        np_pessimistic,
        pillar.get("deducted_np")
    )

    base_value = np * final_pe * discount
    pessimistic_value = pess_result["pessimistic_np"] * pessimistic_pe * discount
    optimistic_value = np_optimistic * optimistic_pe * discount  # 乐观不折

    return {
        "route": "B",
        "pessimistic_value": round(pessimistic_value, 2),
        "base_value": round(base_value, 2),
        "optimistic_value": round(optimistic_value, 2),
        "pessimistic_ps": _market_value_to_per_share(pessimistic_value, total_shares),
        "base_ps": _market_value_to_per_share(base_value, total_shares),
        "optimistic_ps": _market_value_to_per_share(optimistic_value, total_shares),
        "implied_pe_pessimistic": round(pessimistic_pe, 2),
        "implied_pe_base": round(final_pe, 2),
        "implied_pe_optimistic": round(optimistic_pe, 2),
        "pessimistic_params": pess_result,
        # 明确 L1/L2/L3 归属: 路线B的估值全部归入 L1（共识基线）
        "layer1": {"pessimistic": round(pessimistic_value, 2), "base": round(base_value, 2), "optimistic": round(optimistic_value, 2)},
        "layer2": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "layer3": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "details": (
            f"路线B (PE+P/FCF):\n"
            f"  基准: {np}亿 × {final_pe}x × {discount}(折扣) = {base_value:.0f}亿 → {_market_value_to_per_share(base_value, total_shares)}元/股\n"
            f"  悲观: {pess_result['pessimistic_np']}亿 × {pessimistic_pe}x × {discount} = {pessimistic_value:.0f}亿 → {_market_value_to_per_share(pessimistic_value, total_shares)}元/股\n"
            f"  乐观: {np_optimistic}亿 × {optimistic_pe}x × {discount} = {optimistic_value:.0f}亿 → {_market_value_to_per_share(optimistic_value, total_shares)}元/股"
        ),
    }


def calc_route_c_valuation(pillar, pe_result, total_shares, year="2026e"):
    """路线 C: PE + DCF 交叉验证（中资产 + 稳定利润）"""
    result = calc_route_b_valuation(pillar, pe_result, total_shares, year)
    result["route"] = "C"
    result["dcf_note"] = "DCF交叉验证: WACC 8-12%, 永续增长 2-3%（LLM补充）"
    # L1/L2/L3 同路线B
    return result


def calc_route_d_valuation(pillar, params, total_shares, year="2026e"):
    """路线 D: PB + 周期调整PE（中资产 + 波动利润）

    合理PB = 可比PB中值 × (公司ROE / 可比ROE) × 定性调整
    市值 = 净资产 × 合理PB × 折扣系数
    """
    net_assets = pillar.get("net_assets")
    roe_pillar = pillar.get("roe_pillar") or params.get("roe")

    pb_median = pillar.get("comparable_pb_median") or params.get("comparable_pb_median")
    pb_lower = pillar.get("comparable_pb_lower") or params.get("comparable_pb_lower") or pb_median
    pb_upper = pillar.get("comparable_pb_upper") or params.get("comparable_pb_upper") or pb_median

    if net_assets is None:
        return {"error": "路线D缺少 net_assets（净资产 亿元）"}

    # ROE 调整
    comp_roe = pillar.get("comparable_roe") or params.get("comparable_roe") or roe_pillar or 15
    if roe_pillar and comp_roe and comp_roe > 0:
        roe_ratio = roe_pillar / comp_roe
    else:
        roe_ratio = 1.0

    qual_total = sum(adj.get("adjustment", 0) for adj in params.get("qualitative_adjustments", []))
    qual_total = max(QUAL_ADJUSTMENT_TOTAL_MIN, min(qual_total, QUAL_ADJUSTMENT_TOTAL_MAX))

    if pb_median:
        reasonable_pb = pb_median * roe_ratio * (1 + qual_total)
        reasonable_pb = max(pb_lower, min(reasonable_pb, pb_upper))
    else:
        reasonable_pb = 1.0  # fallback

    discount = MARKET_DISCOUNT.get(params.get("market_position", "mid"), 0.85)

    base_value = net_assets * reasonable_pb * discount
    pessimistic_value = net_assets * reasonable_pb * PESSIMISTIC_PB_DISCOUNT * discount
    optimistic_value = net_assets * pb_upper * discount

    return {
        "route": "D",
        "pessimistic_value": round(pessimistic_value, 2),
        "base_value": round(base_value, 2),
        "optimistic_value": round(optimistic_value, 2),
        "pessimistic_ps": _market_value_to_per_share(pessimistic_value, total_shares),
        "base_ps": _market_value_to_per_share(base_value, total_shares),
        "optimistic_ps": _market_value_to_per_share(optimistic_value, total_shares),
        "implied_pb_base": round(reasonable_pb, 2),
        "layer1": {"pessimistic": round(pessimistic_value, 2), "base": round(base_value, 2), "optimistic": round(optimistic_value, 2)},
        "layer2": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "layer3": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "details": (
            f"路线D (PB+周期PE):\n"
            f"  合理PB = {pb_median} × ({roe_pillar}/{comp_roe}) × (1{qual_total:+.0%}) = {reasonable_pb:.2f}x\n"
            f"  基准: {net_assets}亿 × {reasonable_pb:.2f}x × {discount}(折扣) = {base_value:.0f}亿"
        ),
    }


def calc_route_e_valuation(pillar, params, total_shares, year="2026e"):
    """路线 E: PS + PEG（亏损 + 高增长）

    合理PS = 可比PS中值 × (公司毛利率 / 可比毛利率)
    市值 = 营收 × 合理PS × 折扣系数
    """
    year_key = "revenue_2027e" if year == "2027e" else "revenue_2026e"
    revenue = pillar.get(year_key)
    if revenue is None:
        revenue = pillar.get("revenue")
    gross_margin = pillar.get("gross_margin")

    ps_median = pillar.get("comparable_ps_median") or params.get("comparable_ps_median")
    ps_lower = pillar.get("comparable_ps_lower") or params.get("comparable_ps_lower") or ps_median
    ps_upper = pillar.get("comparable_ps_upper") or params.get("comparable_ps_upper") or ps_median

    if revenue is None:
        return {"error": f"路线E缺少 {year_key} 或 revenue（营收 亿元）"}

    # 毛利率调整
    comp_gm = pillar.get("comparable_gross_margin") or params.get("comparable_gross_margin") or gross_margin or 30
    if gross_margin and comp_gm and comp_gm > 0:
        gm_ratio = gross_margin / comp_gm
    else:
        gm_ratio = 1.0

    if ps_median:
        reasonable_ps = ps_median * gm_ratio
        reasonable_ps = max(ps_lower, min(reasonable_ps, ps_upper))
    else:
        reasonable_ps = 1.0

    discount = MARKET_DISCOUNT.get(params.get("market_position", "mid"), 0.85)

    base_value = revenue * reasonable_ps * discount
    pessimistic_value = revenue * reasonable_ps * PESSIMISTIC_PS_DISCOUNT * discount
    optimistic_value = revenue * ps_upper * discount

    return {
        "route": "E",
        "pessimistic_value": round(pessimistic_value, 2),
        "base_value": round(base_value, 2),
        "optimistic_value": round(optimistic_value, 2),
        "pessimistic_ps": _market_value_to_per_share(pessimistic_value, total_shares),
        "base_ps": _market_value_to_per_share(base_value, total_shares),
        "optimistic_ps": _market_value_to_per_share(optimistic_value, total_shares),
        "implied_ps_base": round(reasonable_ps, 2),
        "layer1": {"pessimistic": round(pessimistic_value, 2), "base": round(base_value, 2), "optimistic": round(optimistic_value, 2)},
        "layer2": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "layer3": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "details": (
            f"路线E (PS+PEG, {year.upper()}):\n"
            f"  合理PS = {ps_median} × ({gross_margin}/{comp_gm}) = {reasonable_ps:.2f}x\n"
            f"  基准: {revenue}亿 × {reasonable_ps:.2f}x × {discount} = {base_value:.0f}亿"
        ),
    }


def calc_route_f_valuation(pillar, params, total_shares, year="2026e"):
    """路线 F: PB 净资产重估（亏损 + 低增长）"""
    # 简化版路线 D，无 ROE 调整
    return calc_route_d_valuation(pillar, params, total_shares, year)


def calc_route_a_valuation(pillar, params, total_shares, year="2026e"):
    """路线 A: EV/EBITDA（重资产 + 无非经常性）

    EV = EBITDA × 可比 EV/EBITDA
    股权价值 = EV - 净有息负债
    """
    ebitda_key = f"ebitda_{year}"
    ebitda = pillar.get(ebitda_key) or params.get(ebitda_key)
    ev_ebitda = pillar.get("comparable_ev_ebitda") or params.get("comparable_ev_ebitda")
    net_debt = pillar.get("net_debt") if pillar.get("net_debt") is not None else params.get("net_debt")

    if ebitda is None or ev_ebitda is None:
        return {"error": f"路线A缺少 {ebitda_key} 或 comparable_ev_ebitda"}

    ev = ebitda * ev_ebitda
    equity_value = ev - (net_debt or 0)

    discount = MARKET_DISCOUNT.get(params.get("market_position", "mid"), 0.85)
    base_value = equity_value * discount
    pessimistic_value = equity_value * discount * PESSIMISTIC_EV_EBITDA_DISCOUNT
    optimistic_value = ebitda * ev_ebitda * OPTIMISTIC_EV_EBITDA_PREMIUM - (net_debt or 0)

    return {
        "route": "A",
        "pessimistic_value": round(pessimistic_value, 2),
        "base_value": round(base_value, 2),
        "optimistic_value": round(optimistic_value, 2),
        "pessimistic_ps": _market_value_to_per_share(pessimistic_value, total_shares),
        "base_ps": _market_value_to_per_share(base_value, total_shares),
        "optimistic_ps": _market_value_to_per_share(optimistic_value, total_shares),
        "ev": round(ev, 2),
        "equity_value": round(equity_value, 2),
        "layer1": {"pessimistic": round(pessimistic_value, 2), "base": round(base_value, 2), "optimistic": round(optimistic_value, 2)},
        "layer2": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "layer3": {"pessimistic": 0, "base": 0, "optimistic": 0},
        "details": (
            f"路线A (EV/EBITDA):\n"
            f"  EV = {ebitda}亿 × {ev_ebitda}x = {ev:.0f}亿\n"
            f"  股权价值 = {ev:.0f} - {net_debt or 0} = {equity_value:.0f}亿\n"
            f"  基准: {equity_value:.0f}亿 × {discount} = {base_value:.0f}亿 → {_market_value_to_per_share(base_value, total_shares)}元/股"
        ),
    }


def calc_route_a1_valuation(pillar, pe_result, total_shares, year="2026e"):
    """路线 A1: 分部估值（重资产 + 非经常性）

    核心业务用 PE，非经常性分 Type A/B/C 处理。
    Type B 必须逐项列出管道清单。
    """
    # 核心部分用路线 B 的 PE 估值
    core_result = calc_route_b_valuation(pillar, pe_result, total_shares, year)
    if "error" in core_result:
        return core_result

    # 非经常性部分
    pipeline = pillar.get("type_b_pipeline", [])
    irregular_total = calc_type_b_pipeline(pipeline)

    # 合并: 核心 + 非经常性
    for scenario in ["pessimistic", "base", "optimistic"]:
        key_val = f"{scenario}_value"
        key_ps = f"{scenario}_ps"
        irregular_key = f"{scenario}_value"
        core_l1 = core_result[key_val]
        irregular_l2 = irregular_total.get(irregular_key, 0)
        core_result[f"{key_val}_core"] = core_l1
        core_result[f"{key_val}_irregular"] = irregular_l2
        core_result[key_val] = round(core_l1 + irregular_l2, 2)
        core_result[key_ps] = _market_value_to_per_share(core_result[key_val], total_shares)

    # L1 = 核心PE估值, L2 = Type B 非经常性, L3 = 0
    core_result["layer1"] = {
        "pessimistic": core_result["pessimistic_value_core"],
        "base": core_result["base_value_core"],
        "optimistic": core_result["optimistic_value_core"],
    }
    core_result["layer2"] = {
        "pessimistic": core_result["pessimistic_value_irregular"],
        "base": core_result["base_value_irregular"],
        "optimistic": core_result["optimistic_value_irregular"],
    }
    core_result["layer3"] = {"pessimistic": 0, "base": 0, "optimistic": 0}

    core_result["route"] = "A1"
    core_result["type_b_breakdown"] = irregular_total.get("breakdown", [])
    core_result["type_b_total"] = {k: v for k, v in irregular_total.items() if k != "breakdown"}

    return core_result


# 路线分派表
ROUTE_DISPATCH = {
    "A": calc_route_a_valuation,
    "A1": calc_route_a1_valuation,
    "B": calc_route_b_valuation,
    "C": calc_route_c_valuation,
    "D": calc_route_d_valuation,
    "E": calc_route_e_valuation,
    "F": calc_route_f_valuation,
}


# ══════════════════════════════════════════════════════
# Type B 管道估值
# ══════════════════════════════════════════════════════

def calc_type_b_pipeline(items):
    """Type B 管道逐项估值: Σ(项目利润 × PE × 概率)

    Args:
        items: [{name, project_profit(亿元), pe, probability(0-1)}]

    Returns:
        {pessimistic_value, base_value, optimistic_value, total_per_item, breakdown}
    """
    if not items:
        return {"pessimistic_value": 0, "base_value": 0, "optimistic_value": 0, "breakdown": []}

    breakdown = []
    total_pessimistic = 0
    total_base = 0
    total_optimistic = 0

    for item in items:
        profit = item.get("project_profit", 0)
        pe = item.get("pe", 10)
        prob = item.get("probability", 0.5)

        # 三情景: 概率浮动 ±0.15
        prob_pess = max(0.1, prob - 0.15)
        prob_opt = min(1.0, prob + 0.15)

        v_pess = profit * pe * prob_pess
        v_base = profit * pe * prob
        v_opt = profit * pe * prob_opt

        total_pessimistic += v_pess
        total_base += v_base
        total_optimistic += v_opt

        breakdown.append({
            "name": item.get("name", ""),
            "project_profit": profit,
            "pe": pe,
            "probability": prob,
            "pessimistic_value": round(v_pess, 2),
            "base_value": round(v_base, 2),
            "optimistic_value": round(v_opt, 2),
            "formula": f"{profit}亿 × {pe}x × {prob:.0%} = {v_base:.1f}亿",
        })

    return {
        "pessimistic_value": round(total_pessimistic, 2),
        "base_value": round(total_base, 2),
        "optimistic_value": round(total_optimistic, 2),
        "breakdown": breakdown,
    }


# ══════════════════════════════════════════════════════
# 分歧度计算
# ══════════════════════════════════════════════════════

def calc_divergence(consensus_values):
    """分歧度 = (max-min)/mean × 100%

    Args:
        consensus_values: [float] 各机构预测值列表

    Returns:
        {divergence_pct, classification, max_val, min_val, mean_val, count}
    """
    valid = [v for v in consensus_values if v is not None and v > 0]
    if len(valid) < 2:
        return {
            "divergence_pct": None,
            "classification": "数据不足",
            "max_val": max(valid) if valid else None,
            "min_val": min(valid) if valid else None,
            "mean_val": sum(valid) / len(valid) if valid else None,
            "count": len(valid),
        }

    max_v = max(valid)
    min_v = min(valid)
    mean_v = sum(valid) / len(valid)

    if mean_v == 0:
        divergence = 0
    else:
        divergence = (max_v - min_v) / mean_v * 100

    if divergence < DIVERGENCE_LOW:
        classification = "充分定价"
    elif divergence < DIVERGENCE_HIGH:
        classification = "大部分定价"
    else:
        classification = "部分定价"

    return {
        "divergence_pct": round(divergence, 1),
        "classification": classification,
        "max_val": round(max_v, 2),
        "min_val": round(min_v, 2),
        "mean_val": round(mean_v, 2),
        "count": len(valid),
    }


# ══════════════════════════════════════════════════════
# 三层矩阵汇总
# ══════════════════════════════════════════════════════

def aggregate_matrix(pillars, pillar_results):
    """汇总三层估值矩阵。

    直接使用每个 pillar_result 中的 layer1/layer2/layer3 拆分，
    加上 pillar 参数中的 layer3_items（LLM 提供的预期差增量）。
    """
    matrix_rows = []
    l1 = {"pessimistic": 0.0, "base": 0.0, "optimistic": 0.0}
    l2 = {"pessimistic": 0.0, "base": 0.0, "optimistic": 0.0}
    l3 = {"pessimistic": 0.0, "base": 0.0, "optimistic": 0.0}

    for i, pillar in enumerate(pillars):
        pr = pillar_results[i] if i < len(pillar_results) else {}
        if "error" in pr:
            continue

        # 从 pillar_result 获取引擎计算的 L1/L2
        pr_l1 = pr.get("layer1", {})
        engine_l2 = dict(pr.get("layer2", {}))
        if pillar.get("type_b_pipeline") and not pr.get("type_b_breakdown"):
            pipeline_l2 = calc_type_b_pipeline(pillar["type_b_pipeline"])
            for key in ("pessimistic", "base", "optimistic"):
                engine_l2[key] = (engine_l2.get(key, 0) or 0) + (pipeline_l2.get(f"{key}_value", 0) or 0)
        mapped_l2 = pillar.get("layer2") or {}
        pr_l2 = {key: (engine_l2.get(key, 0) or 0) + (mapped_l2.get(key, 0) or 0)
                 for key in ("pessimistic", "base", "optimistic")}

        # 从 pillar 参数获取 LLM 提供的 L3（预期差增量，未在引擎中计算）
        l3_items = pillar.get("layer3_items") or []
        pr_l3_pess = sum(item.get("pessimistic", 0) or 0 for item in l3_items)
        pr_l3_base = sum(item.get("base", 0) or 0 for item in l3_items)
        pr_l3_opt = sum(item.get("optimistic", 0) or 0 for item in l3_items)
        pr_l3 = {"pessimistic": pr_l3_pess, "base": pr_l3_base, "optimistic": pr_l3_opt}

        # 支柱总计
        pillar_pess = pr_l1.get("pessimistic", 0) + pr_l2.get("pessimistic", 0) + pr_l3["pessimistic"]
        pillar_base = pr_l1.get("base", 0) + pr_l2.get("base", 0) + pr_l3["base"]
        pillar_opt = pr_l1.get("optimistic", 0) + pr_l2.get("optimistic", 0) + pr_l3["optimistic"]

        matrix_rows.append({
            "pillar_name": pillar.get("name", f"支柱{i+1}"),
            "layer1": {"pessimistic": pr_l1.get("pessimistic", 0), "base": pr_l1.get("base", 0), "optimistic": pr_l1.get("optimistic", 0)},
            "layer2": {"pessimistic": pr_l2.get("pessimistic", 0), "base": pr_l2.get("base", 0), "optimistic": pr_l2.get("optimistic", 0)},
            "layer3": pr_l3,
            "pillar_total": {"pessimistic": round(pillar_pess, 2), "base": round(pillar_base, 2), "optimistic": round(pillar_opt, 2)},
        })

        for k in ["pessimistic", "base", "optimistic"]:
            l1[k] += pr_l1.get(k, 0)
            l2[k] += pr_l2.get(k, 0)
            l3[k] += pr_l3[k]

    total = {
        "pessimistic": round(l1["pessimistic"] + l2["pessimistic"] + l3["pessimistic"], 2),
        "base": round(l1["base"] + l2["base"] + l3["base"], 2),
        "optimistic": round(l1["optimistic"] + l2["optimistic"] + l3["optimistic"], 2),
    }

    return {
        "matrix_rows": matrix_rows,
        "layers_summary": {
            "layer1": {**{k: round(v, 2) for k, v in l1.items()}, "pricing_status": "充分定价（共识基线）"},
            "layer2": {**{k: round(v, 2) for k, v in l2.items()}, "pricing_status": "部分定价（分歧调整）"},
            "layer3": {**{k: round(v, 2) for k, v in l3.items()}, "pricing_status": "未定价（预期差）"},
        },
        "total": total,
    }


# ══════════════════════════════════════════════════════
# 2027E 参数迁移
# ══════════════════════════════════════════════════════

def migrate_params_2027e(params, pe_result_2026e):
    """从 2026E 参数迁移到 2027E。

    规则:
    - PE 倍数: 不下调（增速放缓是基数效应，不是生意变差）
    - 悲观PE折扣: 沿用，仅增长质量降档时调整
    - 共识利润: 直接取 2027E 列
    - Layer 3 概率: 时间推进，折扣收窄
    """
    growth_quality = params["growth_quality"]

    # PE 沿用 2026E
    pe_2027e = {
        "final_pe": pe_result_2026e["final_pe"],
        "pessimistic_pe": pe_result_2026e["pessimistic_pe"],
        "optimistic_pe": pe_result_2026e["optimistic_pe"],
        "growth_quality": growth_quality,
        "market_position": params["market_position"],
        "note": "PE 不下调（基数效应），仅增长质量降档时才调整",
    }

    # 支柱迁移
    pillars_2027e = []
    for p in params["pillars"]:
        p_2027e = dict(p)
        # Layer 3 概率上调（时间推进，折扣收窄）
        l3_items_2027e = []
        for item in p.get("layer3_items", []):
            item_2027e = dict(item)
            old_prob = item.get("probability", 0.5)
            # 概率自然上调 10-15%
            item_2027e["probability"] = min(1.0, old_prob + 0.15)
            item_2027e["probability_note"] = f"时间推进: {old_prob}→{item_2027e['probability']:.0%}"
            l3_items_2027e.append(item_2027e)
        p_2027e["layer3_items"] = l3_items_2027e

        # Type B 管道概率上调（扩募验证后）
        pipeline_2027e = []
        for proj in p.get("type_b_pipeline", []):
            proj_2027e = dict(proj)
            old_prob = proj.get("probability", 0.5)
            proj_2027e["probability"] = min(1.0, old_prob + 0.15)
            pipeline_2027e.append(proj_2027e)
        p_2027e["type_b_pipeline"] = pipeline_2027e

        pillars_2027e.append(p_2027e)

    return {
        "pe_2027e": pe_2027e,
        "pillars_2027e": pillars_2027e,
        "migration_notes": [
            "PE 不下调（基数效应，不是生意变差）",
            "Layer 3 概率上调（时间推进，折扣收窄）",
            "Type B 管道概率上调（扩募验证后确定性提高）",
            "增长质量降档时才调 PE 和折扣",
        ],
    }


# ══════════════════════════════════════════════════════
# 反向检查
# ══════════════════════════════════════════════════════

def reverse_check(current_price, pessimistic_ps, base_ps, optimistic_ps,
                   growth_quality, market_position):
    """反向检查 — 指令卡 §5.2

    触发条件:
    - 折扣率 > 2.0 或 < 0.3 → 强制逐项检查
    - 折扣率 > 1.5 且龙头 → 审视增长质量
    - 折扣率 < 0.4 → 检查线性外推
    """
    if not optimistic_ps or optimistic_ps <= 0:
        return {"triggered": False, "discount_rate": None, "triggers": [], "checks": {}}

    discount_rate = current_price / optimistic_ps

    triggers = []
    checks = {}

    # 触发1: 极端折扣率
    if discount_rate > REVERSE_CHECK_DISCOUNT_HIGH:
        triggers.append(f"折扣率 {discount_rate:.2f} > {REVERSE_CHECK_DISCOUNT_HIGH}")
        checks["利润预测"] = "检查是否过度悲观（利润增速是否合理、利润率是否偏离历史中枢）"
        checks["PE倍数"] = "检查可比公司选择是否合适、PEG档位是否正确"
        checks["增长分类"] = "重新审视增长质量分类（结构性/周期性/成熟）"
        checks["可比公司"] = "检查可比公司是否可及、是否过时"
    elif discount_rate < REVERSE_CHECK_DISCOUNT_LOW:
        triggers.append(f"折扣率 {discount_rate:.2f} < {REVERSE_CHECK_DISCOUNT_LOW}")
        checks["利润预测"] = "检查是否线性外推了不可持续的高增速（增速>100%禁止线性外推）"
        checks["极端假设"] = "检查乐观情景假设是否过于激进"
        checks["利润率"] = "检查利润率是否极端偏离行业历史水平"

    # 触发2: 龙头高折扣
    if discount_rate and discount_rate > REVERSE_CHECK_DISCOUNT_LEADER and market_position == "leader":
        triggers.append(f"折扣率 {discount_rate:.2f} > {REVERSE_CHECK_DISCOUNT_LEADER} 且为龙头")
        checks["增长质量"] = "重新审视增长质量分类，检查 PEG 档位"

    # 触发3: 低折扣率
    if discount_rate and discount_rate < REVERSE_CHECK_DISCOUNT_EXTRAP:
        triggers.append(f"折扣率 {discount_rate:.2f} < {REVERSE_CHECK_DISCOUNT_EXTRAP}")
        checks["线性外推"] = "检查利润是否线性外推了不可持续的高增速"

    # 估值区间宽度
    if pessimistic_ps and optimistic_ps and pessimistic_ps > 0:
        width = optimistic_ps / pessimistic_ps
        if width > REVERSE_CHECK_VALUATION_WIDTH:
            triggers.append(f"估值区间宽度 {width:.1f}x > {REVERSE_CHECK_VALUATION_WIDTH}x")
            checks["区间宽度"] = "估值区间过宽，检查悲观/乐观假设的极端程度"

    return {
        "triggered": len(triggers) > 0,
        "discount_rate": round(discount_rate, 4),
        "discount_rate_desc": f"当前价{current_price} / 乐观估值{optimistic_ps}元",
        "triggers": triggers,
        "checks": checks,
        "conclusion_template": "🔍 反向检查：折扣率 {:.2f} [{}]。检查结论：[待LLM补充]".format(
            discount_rate, "触发" if triggers else "未触发"
        ),
    }


# ══════════════════════════════════════════════════════
# CSV 输出
# ══════════════════════════════════════════════════════

def generate_ranking_row(code, name, current_price, pessimistic_ps, base_ps,
                          optimistic_ps, implied_pe_base, valuation_method):
    """生成 valuation_ranking.csv 的一行数据。

    Returns:
        dict: 字段名→值，已格式化为字符串
    """
    discount_rate = round(current_price / optimistic_ps, 4) if optimistic_ps and optimistic_ps > 0 else None
    downside_risk = round(pessimistic_ps * DOWNSIDE_RISK_RATIO, 2) if pessimistic_ps else None

    return {
        "股票代码": f'="{code}"',
        "股票名称": name,
        "当前股价_元": f"{current_price:.2f}",
        "下行风险价_元": f"{downside_risk:.2f}" if downside_risk else "",
        "悲观估值_元": f"{pessimistic_ps:.2f}" if pessimistic_ps else "",
        "基准估值_元": f"{base_ps:.2f}" if base_ps else "",
        "乐观估值_元": f"{optimistic_ps:.2f}" if optimistic_ps else "",
        "安全边际折扣率": f"{discount_rate:.2f}" if discount_rate else "",
        "隐含PE_基准_倍": f"{implied_pe_base:.1f}" if implied_pe_base else "",
        "主估值方法": valuation_method,
        "报告日期": datetime.now().strftime("%Y-%m-%d"),
    }


def update_ranking_csv(ranking_row, ranking_path=None):
    """更新 valuation_ranking.csv：如果代码已存在则替换，否则追加。

    写入后按安全边际折扣率升序排序。
    """
    if ranking_path is None:
        ranking_path = RANKING_PATH

    code = ranking_row["股票代码"]
    code_clean = code.replace('="', "").replace('"', "")

    os.makedirs(ranking_path.parent, exist_ok=True)

    fieldnames = [
        "股票代码", "股票名称", "当前股价_元", "下行风险价_元",
        "悲观估值_元", "基准估值_元", "乐观估值_元",
        "安全边际折扣率", "隐含PE_基准_倍", "主估值方法", "报告日期"
    ]

    rows = []
    if ranking_path.exists():
        with open(ranking_path, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                existing_code = row.get("股票代码", "").replace('="', "").replace('"', "")
                if existing_code != code_clean:
                    rows.append(row)
                # 如果匹配，跳过旧行（=替换）

    rows.append(ranking_row)

    # 按折扣率升序
    def sort_key(r):
        try:
            return float(r.get("安全边际折扣率", 999))
        except (ValueError, TypeError):
            return 999

    rows.sort(key=sort_key)

    with open(ranking_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


# ══════════════════════════════════════════════════════
# 报告片段生成
# ══════════════════════════════════════════════════════

def generate_report_fragments(result, total_shares, current_price):
    """生成报告中的数字表格片段（markdown），LLM 直接嵌入报告。"""
    fragments = {}

    # ── 估值总览表 ──
    total_2026e = result.get("total_2026e", {})
    total_2027e = result.get("total_2027e", {})

    ps_2026e = {
        "pessimistic": _market_value_to_per_share(total_2026e.get("pessimistic", 0), total_shares),
        "base": _market_value_to_per_share(total_2026e.get("base", 0), total_shares),
        "optimistic": _market_value_to_per_share(total_2026e.get("optimistic", 0), total_shares),
    }
    ps_2027e = {
        "pessimistic": _market_value_to_per_share(total_2027e.get("pessimistic", 0), total_shares),
        "base": _market_value_to_per_share(total_2027e.get("base", 0), total_shares),
        "optimistic": _market_value_to_per_share(total_2027e.get("optimistic", 0), total_shares),
    }

    # 隐含 PE
    pe_2026e = result.get("pe_2026e", {})
    pe_base = pe_2026e.get("final_pe", "-") if pe_2026e else "-"

    lines = []
    lines.append("### 双年估值对照")
    lines.append("")
    lines.append(f"| | 2026E 悲观 | 2026E 基准 | 2026E 乐观 | 2027E 悲观 | 2027E 基准 | 2027E 乐观 |")
    lines.append(f"|--|----------|----------|----------|----------|----------|----------|")
    lines.append(
        f"| 市值(亿元) | {total_2026e.get('pessimistic', '-')} | {total_2026e.get('base', '-')} | "
        f"{total_2026e.get('optimistic', '-')} | {total_2027e.get('pessimistic', '-')} | "
        f"{total_2027e.get('base', '-')} | {total_2027e.get('optimistic', '-')} |"
    )
    lines.append(
        f"| 股价(元) | {ps_2026e['pessimistic']} | {ps_2026e['base']} | {ps_2026e['optimistic']} | "
        f"{ps_2027e['pessimistic']} | {ps_2027e['base']} | {ps_2027e['optimistic']} |"
    )

    # 距当前价
    if current_price:
        def dist_pct(ps_val):
            if ps_val is None: return "-"
            return f"{((ps_val - current_price) / current_price * 100):+.0f}%"
        lines.append(
            f"| 距当前 | {dist_pct(ps_2026e['pessimistic'])} | {dist_pct(ps_2026e['base'])} | "
            f"{dist_pct(ps_2026e['optimistic'])} | {dist_pct(ps_2027e['pessimistic'])} | "
            f"{dist_pct(ps_2027e['base'])} | {dist_pct(ps_2027e['optimistic'])} |"
        )

    fragments["overview_table"] = "\n".join(lines)

    # ── 三层汇总表（2026E）─
    matrix = result.get("matrix_2026e", {})
    layers = matrix.get("layers_summary", {})

    lines2 = []
    lines2.append("### 2026E 三层汇总")
    lines2.append("")
    lines2.append("| 层级 | 悲观(亿) | 基准(亿) | 乐观(亿) | 定价状态 |")
    lines2.append("|------|---------|---------|---------|---------|")

    for layer_name, layer_key in [("Layer 1: 共识基线", "layer1"),
                                     ("Layer 2: 分歧调整", "layer2"),
                                     ("Layer 3: 预期差增量", "layer3")]:
        l = layers.get(layer_key, {})
        lines2.append(
            f"| {layer_name} | {l.get('pessimistic', 0):.0f} | {l.get('base', 0):.0f} | "
            f"{l.get('optimistic', 0):.0f} | {l.get('pricing_status', '')} |"
        )

    total_m = matrix.get("total", {})
    lines2.append(
        f"| **合计** | **{total_m.get('pessimistic', 0):.0f}** | **{total_m.get('base', 0):.0f}** | "
        f"**{total_m.get('optimistic', 0):.0f}** | |"
    )

    fragments["matrix_summary"] = "\n".join(lines2)

    # ── 价格带 ──
    if all(ps_2026e.values()):
        band_2026e = f"2026E:  下行 {ps_2026e['pessimistic']} —— 悲观 {ps_2026e['pessimistic']} —— 基准 {ps_2026e['base']} —— 乐观 {ps_2026e['optimistic']}"
        fragments["price_band_2026e"] = band_2026e

    # ── 反向检查片段 ──
    rc = result.get("reverse_check", {})
    if rc:
        fragments["reverse_check"] = (
            f"🔍 反向检查：折扣率 {rc.get('discount_rate', '-')} "
            f"[{'触发' if rc.get('triggered') else '未触发'}]。"
        )

    return fragments


def format_price_band(current, pessimistic, base, optimistic, year=""):
    """生成价格带 ASCII 图"""
    if not all([pessimistic, base, optimistic]):
        return ""
    label = f"{year}: " if year else ""
    return f"{label}下行 —— 悲观 {pessimistic} —— 基准 {base} —— 乐观 {optimistic}"


# ══════════════════════════════════════════════════════
# 主调度器
# ══════════════════════════════════════════════════════

def run_valuation(params):
    """主入口：接收 LLM 参数，执行完整估值计算。

    Args:
        params: dict，LLM 提供的结构化参数（已通过 parse_params 验证）

    Returns:
        dict: 完整估值结果
    """
    meta = params["meta"]
    code = meta["code"]
    name = meta["name"]
    total_shares = meta["total_shares"]
    current_price = meta["current_price"]
    growth_quality = params["growth_quality"]
    pillars = params["pillars"]

    # ── 步骤 1: PE 三步走（2026E）──
    pe_2026e = None
    if params.get("pe_override"):
        override_pe = params["pe_override"]
        pess_pe = round(override_pe * PESSIMISTIC_PE_DISCOUNT.get(growth_quality, 0.85), 2)
        opt_pe = max(
            params.get("comparable_pe_upper") or 0,
            override_pe * 1.2
        )
        pe_2026e = {
            "final_pe": override_pe,
            "pessimistic_pe": pess_pe,
            "optimistic_pe": round(opt_pe, 2),
            "details": (
                "PE 情景覆盖（研究卡已说明取值依据）:\n"
                f"  悲观：{override_pe}x × {PESSIMISTIC_PE_DISCOUNT.get(growth_quality, 0.85):.2f} = {pess_pe}x\n"
                f"  基准：{override_pe}x\n"
                f"  乐观：max(可比上沿 {params.get('comparable_pe_upper') or 0}x, {override_pe}x × 1.20) = {opt_pe:.1f}x"
            ),
        }
    elif params.get("comparable_pe_median"):
        pe_2026e = calc_pe_three_step(
            params["comparable_pe_median"],
            params.get("comparable_pe_lower") or params["comparable_pe_median"],
            params.get("comparable_pe_upper") or params["comparable_pe_median"],
            params["company_growth_rate"],
            params["comp_growth_rate"],
            growth_quality,
            params.get("qualitative_adjustments", []),
        )
    pe_2026e = pe_2026e or {}
    pe_2026e["growth_quality"] = growth_quality
    pe_2026e["market_position"] = params["market_position"]

    # ── 步骤 2: 每支柱估值（2026E）──
    pillar_results_2026e = []
    for pillar in pillars:
        route = pillar.get("route", "B")
        handler = ROUTE_DISPATCH.get(route)
        if handler is None:
            pillar_results_2026e.append({"error": f"未知路线: {route}"})
            continue

        # 路线分两类：需要 PE 参数的（A1/B/C）和不需要的（A/D/E/F）
        if route in ("A1", "B", "C"):
            pr = handler(pillar, pe_2026e, total_shares, "2026e")
        else:
            pr = handler(pillar, params, total_shares, "2026e")

        pillar_results_2026e.append(pr)

    # ── 步骤 3: 矩阵汇总（2026E）──
    matrix_2026e = aggregate_matrix(pillars, pillar_results_2026e)

    # ── 步骤 4: 分歧度 ──
    all_consensus_2026e = params.get("institution_consensus_2026e", [])
    divergence_2026e = calc_divergence(all_consensus_2026e)

    # ── 步骤 5: 2027E 迁移 ──
    migration = migrate_params_2027e(params, pe_2026e)
    pe_2027e = migration["pe_2027e"]

    pillar_results_2027e = []
    for pillar in migration["pillars_2027e"]:
        route = pillar.get("route", "B")
        handler = ROUTE_DISPATCH.get(route)
        if handler is None:
            pillar_results_2027e.append({"error": f"未知路线: {route}"})
            continue
        if route in ("A1", "B", "C"):
            pr = handler(pillar, pe_2027e, total_shares, "2027e")
        else:
            pr = handler(pillar, params, total_shares, "2027e")
        pillar_results_2027e.append(pr)

    matrix_2027e = aggregate_matrix(migration["pillars_2027e"], pillar_results_2027e)

    all_consensus_2027e = params.get("institution_consensus_2027e", [])
    divergence_2027e = calc_divergence(all_consensus_2027e)

    # ── 步骤 6: 反向检查 ──
    ps_2026e = _market_value_to_per_share(matrix_2026e["total"]["pessimistic"], total_shares)
    ps_2026e_base = _market_value_to_per_share(matrix_2026e["total"]["base"], total_shares)
    ps_2026e_opt = _market_value_to_per_share(matrix_2026e["total"]["optimistic"], total_shares)

    rc = reverse_check(
        current_price, ps_2026e, ps_2026e_base, ps_2026e_opt,
        growth_quality, params["market_position"]
    )

    # ── 步骤 7: 生成 valuation_ranking.csv 行 ──
    # 取第一个支柱的主路线作为整体估值方法
    primary_route = pillars[0].get("route", "B") if pillars else "B"
    route_names = {
        "A": "EV/EBITDA", "A1": "分部估值", "B": "PE+P/FCF",
        "C": "PE+DCF", "D": "PB+周期PE", "E": "PS+PEG", "F": "PB重估"
    }
    valuation_method = route_names.get(primary_route, primary_route)

    pe_for_ranking = pe_2026e.get("final_pe") if pe_2026e else None

    ranking_row = generate_ranking_row(
        code, name, current_price,
        ps_2026e, ps_2026e_base, ps_2026e_opt,
        pe_for_ranking, valuation_method
    )

    # ── 步骤 8: 报告片段 ──
    result_for_fragments = {
        "total_2026e": matrix_2026e["total"],
        "total_2027e": matrix_2027e["total"],
        "matrix_2026e": matrix_2026e,
        "pe_2026e": pe_2026e,
        "reverse_check": rc,
    }
    fragments = generate_report_fragments(result_for_fragments, total_shares, current_price)

    # ── 组装最终输出 ──
    return {
        "_meta": {
            "code": code,
            "name": name,
            "generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
            "engine_version": "1.0-dev",
        },
        "pe_2026e": pe_2026e,
        "pe_2027e": pe_2027e,
        "pillars_2026e": pillar_results_2026e,
        "pillars_2027e": pillar_results_2027e,
        "matrix_2026e": matrix_2026e,
        "matrix_2027e": matrix_2027e,
        "divergence_2026e": divergence_2026e,
        "divergence_2027e": divergence_2027e,
        "reverse_check": rc,
        "ranking_row": ranking_row,
        "report_fragments": fragments,
    }


# ══════════════════════════════════════════════════════
# CLI 入口
# ══════════════════════════════════════════════════════

def main():
    if len(sys.argv) < 2:
        print("用法: python3 calc_valuation.py <params.json>")
        print("      python3 calc_valuation.py --stdin")
        sys.exit(1)

    if sys.argv[1] == "--stdin":
        raw = json.load(sys.stdin)
    else:
        params_path = sys.argv[1]
        if not os.path.exists(params_path):
            print(f"❌ 参数文件不存在: {params_path}")
            sys.exit(1)
        with open(params_path, "r", encoding="utf-8") as f:
            raw = json.load(f)

    # 解析参数
    try:
        params = parse_params(raw)
    except ValueError as e:
        print(f"❌ 参数校验失败:\n{e}")
        sys.exit(1)

    # 运行估值
    result = run_valuation(params)

    # 输出结果
    code = params["meta"]["code"]
    today = datetime.now().strftime("%y%m%d")

    # 确保目录存在
    CACHE_CALC_RESULTS.mkdir(parents=True, exist_ok=True)
    result_path = CACHE_CALC_RESULTS / f"{code}_{today}.json"
    with open(result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 更新 valuation_ranking.csv
    ranking_row = result.get("ranking_row", {})
    if ranking_row:
        n = update_ranking_csv(ranking_row)
        print(f"✅ valuation_ranking.csv 已更新 ({n} 行)")

    print(f"✅ 估值结果: {result_path}")

    # 摘要输出
    meta = result["_meta"]
    pe = result.get("pe_2026e", {})
    m = result.get("matrix_2026e", {})
    total = m.get("total", {})
    rc = result.get("reverse_check", {})

    print(f"\n{'='*60}")
    print(f"估值完成: {meta['code']} {meta['name']}")
    if pe and pe.get("final_pe"):
        print(f"  PE(2026E): {pe['final_pe']}x (悲观 {pe.get('pessimistic_pe', '-')}x)")
    print(f"  2026E 基准估值: {total.get('base', '-')} 亿"
          f" / {_market_value_to_per_share(total.get('base'), params['meta']['total_shares'])} 元/股")
    print(f"  反向检查: {'⚠️ 触发' if rc.get('triggered') else '✅ 未触发'}"
          f" (折扣率 {rc.get('discount_rate', '-')})")

    # 打印报告片段
    fragments = result.get("report_fragments", {})
    if fragments.get("overview_table"):
        print(f"\n{fragments['overview_table']}")
    if fragments.get("matrix_summary"):
        print(f"\n{fragments['matrix_summary']}")


if __name__ == "__main__":
    main()
