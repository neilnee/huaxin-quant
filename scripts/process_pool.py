#!/usr/bin/env python3
"""Process pool screening: phases 2-5."""
import csv, os, sys
from datetime import datetime, date
from pathlib import Path
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.data.pool_data import PoolSegmentCache, find_key, parse_date, parse_num, parse_pct
from scripts.shared import get_latest_annual_period, expected_trade_date
from scripts.strategy_config import load_strategy_config

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEGMENT_DIR = PROJECT_ROOT / "cache" / "xuangu"
OUTPUT_DIR = PROJECT_ROOT / "pool"
_pool_date = os.environ.get("POOL_DATE") or expected_trade_date()
TODAY = datetime.strptime(_pool_date, "%Y-%m-%d").date()
ANNUAL_PERIOD = get_latest_annual_period()
POOL_STRATEGY_FILE = "01-pool.json"
POOL_STRATEGY, POOL_STRATEGY_PATH = load_strategy_config(POOL_STRATEGY_FILE)
STRATEGY_VERSION = POOL_STRATEGY["strategy_version"]
RUNTIME_CFG = POOL_STRATEGY["runtime"]
HARD_FILTER_CFG = POOL_STRATEGY["hard_filters"]
INDUSTRY_CFG = POOL_STRATEGY["industry"]
SOFT_TAG_CFG = POOL_STRATEGY["soft_tags"]

INDUSTRY_EXCLUDE = INDUSTRY_CFG["exclude_keywords"]
SEMICONDUCTOR_KW = INDUSTRY_CFG["semiconductor_keyword"]

# Tiered gross margin floors by sub-industry
GM_TIERS = HARD_FILTER_CFG["gross_margin_tiers"]
NORMAL_GM_FLOOR = HARD_FILTER_CFG["normal_gross_margin_floor_pct"]
SEMI_RELAX_REV_GROWTH = HARD_FILTER_CFG["semiconductor_revenue_growth_gate_pct"]
MIN_MARKET_CAP = HARD_FILTER_CFG["min_market_cap"]
SMALL_CAP_TAG_CEILING = SOFT_TAG_CFG["small_cap_tag_ceiling"]
TAG_PENALTIES = SOFT_TAG_CFG["tag_penalties"]

# ── Helpers ──────────────────────────────────────────────

def row_get(row: dict, *patterns, default=None):
    """Resolve a key from *row* using *patterns*, per-row.

    Unlike the global KEY_* variables resolved once from a sample stock,
    this searches the actual row each time — safe against date-suffix
    mismatches when xuangu files span multiple fetch dates.
    """
    key = find_key(row, *patterns)
    return row.get(key) if key else default


def get_gm_floor(industry_raw: str) -> float:
    """Return the gross margin floor for a given industry string.

    Matches sub-industry keywords from gross_margin_tiers first,
    falls back to _semiconductor_default if '半导体' is in the string,
    otherwise uses _normal_default.
    """
    industry = str(industry_raw) if industry_raw else ""
    for keyword, floor in GM_TIERS.items():
        if keyword.startswith("_"):
            continue
        if keyword in industry:
            return floor
    if SEMICONDUCTOR_KW in industry:
        return GM_TIERS["_semiconductor_default"]
    return GM_TIERS["_normal_default"]


def detect_period(v) -> str:
    """从值字符串检测报告期。'5096.36万|2026一季报' → 'Q1'"""
    s = str(v) if v else ""
    if "一季报" in s: return "Q1"
    if "中报" in s or "半年报" in s: return "H1"
    if "三季报" in s: return "Q3"
    if "年报" in s: return "annual"
    return "unknown"

# 周期感知阈值 — 非线性递增。年头刚起步（Q1回款未开始、利润薄），
# 越靠年末越接近全年真实水平，阈值也越接近年报基准。
NP_MIN_ANNUAL = HARD_FILTER_CFG["np_min_annual"]
OCF_NP_THRESHOLD = HARD_FILTER_CFG["ocf_np_threshold_by_period"]
NP_MIN_BY_PERIOD = HARD_FILTER_CFG["np_min_by_period"]

def fmt(v) -> str:
    """Format value for CSV output."""
    if v is None: return ""
    if isinstance(v, float): return f"{v:.2f}"
    return str(v)

def build_soft_tags(
    *,
    mcap=None, pe=None, pb=None, roe=None, ocf_np_ratio=None,
    np_val=None, np_growth=None, debt=None, period="unknown",
    semi_relax=False,
) -> tuple[list[str], int]:
    """Build non-blocking risk/quality labels for manual review."""
    tags = []

    if mcap is None or np_val is None:
        tags.append("MISSING_KEY_DATA")
    elif mcap < SMALL_CAP_TAG_CEILING:
        tags.append("MID_SMALL_CAP_50_100")

    if period in {"Q1", "H1", "Q3"}:
        tags.append("DATA_PERIOD_QUARTERLY")

    if roe is not None:
        if roe < SOFT_TAG_CFG["low_roe_pct"]:
            tags.append("LOW_ROE")
        elif roe < SOFT_TAG_CFG["weak_roe_pct"]:
            tags.append("WEAK_ROE")

    if ocf_np_ratio is not None and ocf_np_ratio < SOFT_TAG_CFG["weak_cashflow_ocf_np"]:
        tags.append("WEAK_CASHFLOW")

    if (
        debt is not None
        and SOFT_TAG_CFG["high_debt_edge_min_pct"] <= debt < SOFT_TAG_CFG["high_debt_edge_max_pct"]
    ):
        tags.append("HIGH_DEBT_EDGE")

    if pe is not None and pe <= 0:
        tags.append("NEGATIVE_PE_TTM")
    elif (
        pe is not None and pe > SOFT_TAG_CFG["high_pe_ttm"]
    ) or (
        pb is not None and pb > SOFT_TAG_CFG["high_pb"]
    ):
        tags.append("HIGH_VALUATION")

    if (
        np_growth is not None
        and np_growth > SOFT_TAG_CFG["low_base_rebound_np_growth_pct"]
        and (roe is None or roe < SOFT_TAG_CFG["low_roe_pct"] or (np_val is not None and np_val < NP_MIN_ANNUAL))
    ):
        tags.append("LOW_BASE_REBOUND")

    if semi_relax:
        tags.append("SEMI_CASHFLOW_RELAX")

    # De-duplicate while preserving order.
    tags = list(dict.fromkeys(tags))
    score = max(
        SOFT_TAG_CFG["min_quality_score"],
        SOFT_TAG_CFG["base_quality_score"] - sum(TAG_PENALTIES.get(tag, 0) for tag in tags)
    )
    return tags, score

# ── Phase 1 load & merge ─────────────────────────────────

print("=" * 60)
print("Phase 1: Load & merge segments")
print("=" * 60)
print(f"  strategy: {STRATEGY_VERSION} ({POOL_STRATEGY_FILE})")

all_stocks: dict[str, dict] = {}
CACHE_MAX_AGE_DAYS = RUNTIME_CFG["cache_max_age_days"]
load_result = PoolSegmentCache(SEGMENT_DIR).load_recent_segments(TODAY, CACHE_MAX_AGE_DAYS)
all_stocks = load_result.stocks
segment_counts = load_result.segment_counts
stale_files = load_result.stale_files

for filename, rows, total in segment_counts:
    mtime = datetime.fromtimestamp((SEGMENT_DIR / filename).stat().st_mtime).date()
    age = (TODAY - mtime).days
    age_tag = f"({age}天前)" if age > 0 else ""
    print(f"  {filename[-40:]}: {rows} rows / {total} total {age_tag}")

if stale_files:
    print(f"\n  ⚠️ 跳过 {len(stale_files)} 个过期缓存（>{CACHE_MAX_AGE_DAYS}天）:")
    for f, age in stale_files:
        print(f"    - {f[-50:]} ({age}天前)")
    print(f"  如需刷新，删除 cache/xuangu/ 中旧文件后重新拉取")

if not all_stocks:
    print("\n  ❌ 无有效缓存文件，请先拉取 xuangu 数据")
    exit(1)

print(f"\n  Merged unique: {len(all_stocks)} stocks")

# ── Phase 2: Self-calculate filters ──────────────────────

print("\n" + "=" * 60)
print("Phase 2: Self-calculate filters")
print("=" * 60)

# Build field key map from sample
sample = next(iter(all_stocks.values()))

def resolve_ocf_np_keys(sample, annual_period):
    """统一确定 OCF 和 NP 的字段 key，保证同一报告期。

    优先年报（两者均可用时），任一个缺失年报则统一降级到 #LATEST#。
    绝不跨周期比较。
    """
    np_annual = find_key(sample, "PARENTNETPROFIT", annual_period)
    ocf_annual = find_key(sample, "NETOPERATECASHFLOW", annual_period)

    if np_annual and ocf_annual:
        print(f"  ✅ OCF+NP 统一年报({annual_period})")
        return np_annual, ocf_annual

    # 统一降级到 #LATEST#（精确匹配后缀，避免 NP 取到 2025-09-30 而 OCF 取到 2026Q1）
    np_latest = find_key(sample, "PARENTNETPROFIT", "#LATEST#")
    ocf_latest = find_key(sample, "NETOPERATECASHFLOW", "#LATEST#")
    if np_latest and ocf_latest:
        print(f"  ⚠️ OCF+NP 统一降级#LATEST#（年报不全: NP={'✅' if np_annual else '❌'} OCF={'✅' if ocf_annual else '❌'}）")
        return np_latest, ocf_latest

    # 各自尽力
    np_key = np_annual or np_latest or find_key(sample, "PARENTNETPROFIT")
    ocf_key = ocf_annual or ocf_latest or find_key(sample, "NETOPERATECASHFLOW")
    print(f"  ⚠️ OCF+NP 尽力模式: NP={'annual' if np_annual else 'latest' if np_latest else 'fallback'} OCF={'annual' if ocf_annual else 'latest' if ocf_latest else 'fallback'}")
    return np_key, ocf_key

KEY_NP, KEY_OCF = resolve_ocf_np_keys(sample, ANNUAL_PERIOD)

# 检测实际数据周期，确定 OCF/NP 阈值
_sample_np_raw = str(sample.get(KEY_NP, "")) if KEY_NP else ""
_detected_period = detect_period(_sample_np_raw)
_ocf_threshold = OCF_NP_THRESHOLD.get(_detected_period, OCF_NP_THRESHOLD["unknown"])
print(f"  📐 周期={_detected_period} | OCF/NP阈值={_ocf_threshold} | NP阈值={NP_MIN_BY_PERIOD.get(_detected_period, NP_MIN_ANNUAL)/1e4:.0f}万")

def find_key_annual_fallback(sample, metric, period, label):
    """Try period match first, fall back to metric-only if only LATEST available."""
    key = find_key(sample, metric, period)
    if key:
        print(f"  ✅ {label}({period}): {key}")
        return key
    key = find_key(sample, metric)
    if key:
        print(f"  ⚠️ {label}(降级LATEST): {key}")
        return key
    print(f"  ❌ {label}: None")
    return None

KEY_DEBT = find_key(sample, "ZCFZL")
KEY_LIST_DATE = find_key(sample, "LISTING_DATE")
KEY_INDUSTRY = find_key(sample, "INDUSTRY", require_all=False)  # 东财二级

# Output field keys
KEY_MCAP = find_key(sample, "TOAL_MARKET_VALUE")
KEY_PE = find_key(sample, "PETTM")
KEY_PB = find_key(sample, "PB") if find_key(sample, "PB") else find_key(sample, "市净率", require_all=False)

# Per-row fallback patterns for date-sensitive keys.
# When xuangu files span multiple fetch dates, the date suffix in the key
# (e.g. {2026-07-08} vs {2026-07-09}) drifts and row.get(KEY_*) returns None.
# row_get re-resolves against the actual row, safe against suffix drift.
_MCAP_PATTERN = ("TOAL_MARKET_VALUE",)
_PE_PATTERN = ("PETTM",)
_PB_PATTERN = ("PB",)
_ROE_PATTERN = ("ROE_WEIGHT",)
_DEBT_PATTERN = ("ZCFZL",)
_GM_PATTERN = ("XSMLL",)
_RD_PATTERN = ("RSEXPENSE_RATIO",)
# Be more specific for PB: exclude PB that's part of something else
for k in sample:
    if "PB<" in k or k.endswith("_PB_"):
        KEY_PB = k
        break
KEY_ROE = find_key(sample, "ROE_WEIGHT")
KEY_REVENUE_ANNUAL = find_key_annual_fallback(sample, "OPERATEREVE", ANNUAL_PERIOD, "营业收入(年报)")
KEY_REV_GROWTH = find_key(sample, "营业收入最新同比增长率")
KEY_NP_GROWTH = find_key(sample, "归属母公司股东的净利润最新同比增长率")
KEY_GM = find_key(sample, "XSMLL")
KEY_RD = find_key(sample, "RDRATIO") or find_key(sample, "RSEXPENSE_RATIO")
KEY_EPS = find_key_annual_fallback(sample, "EPSJB", ANNUAL_PERIOD, "每股收益") or find_key_annual_fallback(sample, "BASICEPS", ANNUAL_PERIOD, "每股收益(BASIC)")

# Print key map
key_map = {
    "归母净利润": KEY_NP,
    "经营现金流": KEY_OCF,
    "资产负债率": KEY_DEBT,
    "上市日期": KEY_LIST_DATE,
    "所属行业(东财二级)": KEY_INDUSTRY,
    "总市值": KEY_MCAP,
    "市盈率(TTM)": KEY_PE,
    "市净率": KEY_PB,
    "ROE": KEY_ROE,
    "营业收入(年报)": KEY_REVENUE_ANNUAL,
    "营收同比增速": KEY_REV_GROWTH,
    "净利同比增速": KEY_NP_GROWTH,
    "毛利率": KEY_GM,
    "研发费用占比": KEY_RD,
    "每股收益": KEY_EPS,
}
for name, k in key_map.items():
    print(f"  {'✅' if k else '❌'} {name}: {k}")

if not KEY_MCAP:
    print("  ❌ 缺少总市值字段，无法执行 50 亿市值硬门槛")
    exit(1)

# Apply filters
passed = {}
rejected = {
    "总市值<50亿": 0, "总市值缺失": 0,
    "OCF/NP比率≤阈值": 0,
    "负债率≥70%": 0, "归母净利<阈值": 0,
    "上市不足1年": 0, "净利≤0": 0,
    "毛利率<行业门槛": 0,
}

for code, row in all_stocks.items():
    # Industry: no date suffix in key, direct lookup is safe
    industry_raw = str(row.get(KEY_INDUSTRY, "")) if KEY_INDUSTRY else ""
    gm_floor = get_gm_floor(industry_raw)
    is_semi = SEMICONDUCTOR_KW in industry_raw

    # Parse financials — row_get for date-sensitive keys (safe against suffix drift)
    mcap = parse_num(row_get(row, *_MCAP_PATTERN))
    gm = parse_pct(row_get(row, *_GM_PATTERN))

    # Condition 0: Market cap >= 50亿
    if mcap is None:
        rejected["总市值缺失"] += 1
        continue
    if mcap < MIN_MARKET_CAP:
        rejected["总市值<50亿"] += 1
        continue

    # Condition A: Listed >= 1 year
    list_date = parse_date(row.get(KEY_LIST_DATE) if KEY_LIST_DATE else None)
    if list_date and (TODAY - list_date.date()).days < HARD_FILTER_CFG["min_listing_days"]:
        rejected["上市不足1年"] += 1
        continue

    # 周期检测（NP 和 OCF 用同一个 key 周期，取 NP 的值判断）
    np_raw = str(row.get(KEY_NP, "")) if KEY_NP else ""
    period = detect_period(np_raw)

    # Condition B: OCF/NP（阈值按报告期差异化）
    ocf = parse_num(row.get(KEY_OCF)) if KEY_OCF else None
    np_val = parse_num(row.get(KEY_NP)) if KEY_NP else None

    if np_val is not None and np_val <= 0:
        rejected["净利≤0"] += 1
        continue

    if not is_semi:
        if ocf is not None and np_val is not None and np_val > 0:
            threshold = OCF_NP_THRESHOLD.get(period, OCF_NP_THRESHOLD["unknown"])
            if ocf / np_val <= threshold:
                rejected["OCF/NP比率≤阈值"] += 1
                continue

    # Condition C: NP 门槛（按报告期折算）
    np_threshold = NP_MIN_BY_PERIOD.get(period, NP_MIN_ANNUAL)
    if np_val is not None and np_val < np_threshold:
        rejected["归母净利<阈值"] += 1
        continue

    # Condition D: Debt ratio < 70%
    debt = parse_pct(row_get(row, *_DEBT_PATTERN))
    if debt is not None and debt >= HARD_FILTER_CFG["max_debt_ratio_pct"]:
        rejected["负债率≥70%"] += 1
        continue

    # Condition E: Gross margin check (tiered by sub-industry)
    if gm is not None and gm < gm_floor:
        rejected["毛利率<行业门槛"] += 1
        continue

    passed[code] = row

print(f"\n  Filter results:")
for reason, count in rejected.items():
    print(f"    {reason}: {count} 只剔除")
print(f"  通过: {len(passed)} 只")

# ── Phase 3: Industry exclusion ──────────────────────────

print("\n" + "=" * 60)
print("Phase 3: Industry exclusion")
print("=" * 60)

industry_key = KEY_INDUSTRY
excluded_by_ind = []
final = {}

for code, row in passed.items():
    industry = str(row.get(industry_key, "")) if industry_key else ""
    hit = False
    for kw in INDUSTRY_EXCLUDE:
        if kw in industry:
            hit = True
            break
    if hit:
        excluded_by_ind.append((code, row.get("SECURITY_SHORT_NAME", ""), industry))
    else:
        final[code] = row

print(f"  行业排除: {len(excluded_by_ind)} 只")
if excluded_by_ind:
    ind_counter = Counter()
    for _, _, ind in excluded_by_ind:
        ind_counter[ind] += 1
    for ind, cnt in ind_counter.most_common(10):
        print(f"    {ind}: {cnt}")
print(f"  最终入池: {len(final)} 只")

# ── Phase 4: Output CSV ──────────────────────────────────

print("\n" + "=" * 60)
print("Phase 4: Output CSV")
print("=" * 60)

OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
csv_path = OUTPUT_DIR / f"pool_{TODAY.strftime('%y%m%d')}.csv"

fieldnames = [
    "股票代码","股票名称","所属行业","上市天数",
    "总市值_元","市盈率_倍","市净率_倍","ROE_pct",
    "营业收入_元","营收同比增速_pct","归母净利润_元",
    "净利润同比增速_pct","经营现金流_元",
    "经营现金流_净利比","毛利率_pct","研发费用占比_pct",
    "资产负债率_pct","每股收益_元",
    "质量评分","风险标签","数据周期","半导体现金流豁免",
    "strategy_version",
]

rows_out = []
for code in sorted(final):
    row = final[code]

    # Parse values
    list_date = parse_date(row.get(KEY_LIST_DATE)) if KEY_LIST_DATE else None
    list_days = (TODAY - list_date.date()).days if list_date else None

    mcap = parse_num(row_get(row, *_MCAP_PATTERN))
    pe = parse_pct(row_get(row, *_PE_PATTERN))
    pb = parse_pct(row_get(row, *_PB_PATTERN))
    roe = parse_pct(row_get(row, *_ROE_PATTERN))
    revenue = parse_num(row.get(KEY_REVENUE_ANNUAL)) if KEY_REVENUE_ANNUAL else None
    rev_g = parse_pct(row.get(KEY_REV_GROWTH)) if KEY_REV_GROWTH else None
    np_val = parse_num(row.get(KEY_NP)) if KEY_NP else None
    np_raw = str(row.get(KEY_NP, "")) if KEY_NP else ""
    period = detect_period(np_raw)
    np_g = parse_pct(row.get(KEY_NP_GROWTH)) if KEY_NP_GROWTH else None
    ocf = parse_num(row.get(KEY_OCF)) if KEY_OCF else None
    gm = parse_pct(row_get(row, *_GM_PATTERN))
    rd = parse_pct(row_get(row, *_RD_PATTERN))
    debt = parse_pct(row_get(row, *_DEBT_PATTERN))
    eps = parse_pct(row.get(KEY_EPS)) if KEY_EPS else None
    industry = str(row.get(industry_key, "")) if industry_key else ""
    is_semi = SEMICONDUCTOR_KW in industry
    semi_relax = is_semi and rev_g is not None and rev_g > SEMI_RELAX_REV_GROWTH

    # Calculate OCF/NP ratio
    ocf_np_ratio = None
    if ocf is not None and np_val is not None and np_val > 0:
        ocf_np_ratio = ocf / np_val
    tags, quality_score = build_soft_tags(
        mcap=mcap,
        pe=pe,
        pb=pb,
        roe=roe,
        ocf_np_ratio=ocf_np_ratio,
        np_val=np_val,
        np_growth=np_g,
        debt=debt,
        period=period,
        semi_relax=semi_relax,
    )

    rows_out.append({
        "股票代码": f'="{code}"',
        "股票名称": row.get("SECURITY_SHORT_NAME", ""),
        "所属行业": industry,
        "上市天数": str(list_days) if list_days else "",
        "总市值_元": fmt(mcap),
        "市盈率_倍": fmt(pe),
        "市净率_倍": fmt(pb),
        "ROE_pct": fmt(roe),
        "营业收入_元": fmt(revenue),
        "营收同比增速_pct": fmt(rev_g),
        "归母净利润_元": fmt(np_val),
        "净利润同比增速_pct": fmt(np_g),
        "经营现金流_元": fmt(ocf),
        "经营现金流_净利比": fmt(ocf_np_ratio),
        "毛利率_pct": fmt(gm),
        "研发费用占比_pct": fmt(rd),
        "资产负债率_pct": fmt(debt),
        "每股收益_元": fmt(eps),
        "质量评分": str(quality_score),
        "风险标签": ";".join(tags),
        "数据周期": period,
        "半导体现金流豁免": "Y" if semi_relax else "N",
        "strategy_version": STRATEGY_VERSION,
    })

with open(csv_path, "w", newline="", encoding="utf-8-sig") as f:
    w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    w.writerows(rows_out)

print(f"  Saved: {csv_path}")
print(f"  Rows: {len(rows_out)}")

# ── Phase 5: Summary ─────────────────────────────────────

print("\n" + "=" * 60)
print("Phase 5: Summary")
print("=" * 60)

total_api = sum(s[2] for s in segment_counts)
print(f"\n  阶段一: API 拉取 {len(all_stocks)} 只（理论总数合计 {total_api}，覆盖率 {len(all_stocks)*100//total_api if total_api else 0}%）")
print(f"  阶段二: 通过 {len(passed)} 只")
for reason, count in rejected.items():
    print(f"    - {reason}: {count} 只")
print(f"  阶段三: 行业排除 {len(excluded_by_ind)} 只 → 最终入池 {len(final)} 只")

# Industry distribution
ind_dist = Counter()
for code in final:
    row = final[code]
    ind = str(row.get(industry_key, "未知")) if industry_key else "未知"
    ind_dist[ind] += 1

print(f"\n  行业分布 (Top 10):")
for ind, cnt in ind_dist.most_common(10):
    print(f"    {ind}: {cnt}")

def collect_float(col):
    values = []
    for r in rows_out:
        raw = r.get(col, "")
        if raw == "":
            continue
        try:
            values.append(float(raw))
        except ValueError:
            continue
    return values

# Core metrics
gms = collect_float("毛利率_pct")
rgs = collect_float("营收同比增速_pct")
rds = collect_float("研发费用占比_pct")
roes = collect_float("ROE_pct")
quality_scores = collect_float("质量评分")
risk_counter = Counter()
for r in rows_out:
    for tag in r.get("风险标签", "").split(";"):
        if tag:
            risk_counter[tag] += 1

print(f"\n  核心指标均值:")
if gms: print(f"    毛利率_pct 均值: {sum(gms)/len(gms):.2f}")
if rgs: print(f"    营收同比增速_pct 均值: {sum(rgs)/len(rgs):.2f}")
if rds: print(f"    研发费用占比_pct 均值: {sum(rds)/len(rds):.2f}")
if roes: print(f"    ROE_pct 均值: {sum(roes)/len(roes):.2f}")
if quality_scores: print(f"    质量评分均值: {sum(quality_scores)/len(quality_scores):.2f}")

if risk_counter:
    print(f"\n  软标签分布 (Top 10):")
    for tag, cnt in risk_counter.most_common(10):
        print(f"    {tag}: {cnt}")

# Check for truncation
truncation_threshold = RUNTIME_CFG["truncation_threshold"]
truncated = [s for s in segment_counts if s[1] >= truncation_threshold]
if truncated:
    print(f"\n  ⚠️ 截断警告：以下分段实际返回 >= {truncation_threshold}，可能覆盖不全:")
    for name, rows, total in truncated:
        print(f"    {name}: {rows}/{total}")

print(f"\n{'='*60}")
print(f"✅ 完成: {csv_path}")
print(f"{'='*60}")
