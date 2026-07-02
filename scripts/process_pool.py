#!/usr/bin/env python3
"""Process pool screening: phases 2-5."""
import json, csv, os, re, sys
from datetime import datetime, date
from pathlib import Path
from collections import Counter
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import get_latest_annual_period

PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEGMENT_DIR = PROJECT_ROOT / "cache" / "xuangu"
OUTPUT_DIR = PROJECT_ROOT / "pool"
TODAY = date.today()
ANNUAL_PERIOD = get_latest_annual_period()

INDUSTRY_EXCLUDE = [
    "银行","保险","证券","多元金融",
    "白酒","食品饮料","服装家纺","家用电器","旅游零售","农林牧渔","商业物业经营",
    "房地产开发","房地产服务","水泥","建筑装饰",
    "煤炭","钢铁","石油石化","航运港口","航空机场",
    "燃气","水务","环保",
    "医药生物",
]

SEMICONDUCTOR_KW = "半导体"

# Relaxed thresholds for semiconductor stocks with high revenue growth
SEMI_RELAX_GM = 15          # gross margin floor (pct)
SEMI_RELAX_REV_GROWTH = 25  # revenue growth gate (pct)
NORMAL_GM_FLOOR = 20        # gross margin floor for non-semiconductors

# ── Helpers ──────────────────────────────────────────────

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
NP_MIN_ANNUAL = 50_000_000  # 年报: 归母净利 ≥ 5000万
OCF_NP_THRESHOLD = {
    "Q1": 0.0,      # Q1 刚开始，只要 OCF 不为负即可
    "H1": 0.10,     # 半年有部分回款
    "Q3": 0.30,     # 接近全年
    "annual": 0.50,
    "unknown": 0.50,
}
NP_MIN_BY_PERIOD = {
    "Q1": 5_000_000,        # Q1 利润薄，500万门槛
    "H1": 20_000_000,       # 半年利润还未完全释放
    "Q3": 35_000_000,       # 接近全年
    "annual": NP_MIN_ANNUAL,
    "unknown": NP_MIN_ANNUAL,
}

def pick_annual(v: str) -> str:
    """从 '值1|周期1, 值2|周期2' 中取年报周期的值，无年报则取第一个。
    例如 '13.78亿|2026一季报, 52.92亿|2025年报' → '52.92亿'"""
    if v is None or v == "" or v == "-":
        return ""
    s = str(v)
    if "|" not in s:
        return s
    parts = [p.strip() for p in s.split(",")]
    # 优先年报
    for p in parts:
        if "年报" in p and "一季报" not in p and "三季报" not in p and "半年报" not in p:
            return p.split("|")[0].strip()
    # 其次中报
    for p in parts:
        if "半年报" in p or "中报" in p:
            return p.split("|")[0].strip()
    # 兜底取第一个
    return parts[0].split("|")[0].strip()


def parse_num(v) -> Optional[float]:
    """'17.04亿' → 1704000000.0, '5000万' → 50000000.0"""
    if v is None or v == "" or v == "-":
        return None
    s = pick_annual(v).strip().replace(",", "").replace("%", "")
    try:
        if "亿" in s: return float(s.replace("亿", "")) * 1e8
        if "万" in s: return float(s.replace("万", "")) * 1e4
        if "元" in s: return float(s.replace("元", ""))
        return float(s)
    except ValueError:
        return None

def parse_pct(v) -> Optional[float]:
    """Parse percentage value."""
    if v is None or v == "" or v == "-":
        return None
    s = pick_annual(v).strip().replace(",", "").replace("%", "")
    try: return float(s)
    except ValueError: return None

def parse_date(v) -> Optional[datetime]:
    """Parse listing date."""
    if not v: return None
    s = str(v).split("|")[0].strip()
    try: return datetime.strptime(s, "%Y-%m-%d")
    except ValueError: return None

def fmt(v) -> str:
    """Format value for CSV output."""
    if v is None: return ""
    if isinstance(v, float): return f"{v:.2f}"
    return str(v)

def find_key(row: dict, *patterns, require_all=True) -> Optional[str]:
    """Fuzzy match field key. If require_all, ALL patterns must match."""
    for k in row:
        if require_all:
            if all(p in k for p in patterns):
                return k
        else:
            if any(p in k for p in patterns):
                return k
    return None

# ── Phase 1 load & merge ─────────────────────────────────

print("=" * 60)
print("Phase 1: Load & merge segments")
print("=" * 60)

all_stocks: dict[str, dict] = {}
segment_counts = []
stale_files = []
CACHE_MAX_AGE_DAYS = 5  # xuangu 数据以财报为主，5个交易日内有效

# 按修改时间降序排列（新的在前），确保同一股票多文件时新数据覆盖旧数据
raw_files = sorted(
    SEGMENT_DIR.glob("*_raw.json"),
    key=lambda f: f.stat().st_mtime,
    reverse=True
)

for json_file in raw_files:
    # 时效检查：超过 5 天的缓存标记为过期
    mtime = datetime.fromtimestamp(json_file.stat().st_mtime).date()
    age = (TODAY - mtime).days
    if age > CACHE_MAX_AGE_DAYS:
        stale_files.append((json_file.name, age))
        continue

    with open(json_file) as f:
        data = json.load(f)
    result = data["data"]["data"]["allResults"]["result"]
    rows = result.get("dataList", [])
    total = result.get("total", 0)
    segment_counts.append((json_file.name, len(rows), total))

    for row in rows:
        code = row.get("SECURITY_CODE", "").strip().zfill(6)
        if code and len(code) == 6:
            all_stocks[code] = row

    age_tag = f"({age}天前)" if age > 0 else ""
    print(f"  {json_file.name[-40:]}: {len(rows)} rows / {total} total {age_tag}")

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
_ocf_threshold = OCF_NP_THRESHOLD.get(_detected_period, 0.50)
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
KEY_RD = find_key(sample, "RDRATIO")
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

# Apply filters
passed = {}
rejected = {
    "OCF/NP比率≤阈值": 0,
    "负债率≥70%": 0, "归母净利<阈值": 0,
    "上市不足1年": 0, "净利≤0": 0,
    "毛利率<20%(非半导体)": 0, "毛利率<15%(半导体高增长)": 0,
}

for code, row in all_stocks.items():
    # Determine if semiconductor
    industry_raw = str(row.get(KEY_INDUSTRY, "")) if KEY_INDUSTRY else ""
    is_semi = SEMICONDUCTOR_KW in industry_raw

    # Parse financials
    rev_growth = parse_pct(row.get(KEY_REV_GROWTH)) if KEY_REV_GROWTH else None
    gm = parse_pct(row.get(KEY_GM)) if KEY_GM else None

    # Check if eligible for semiconductor relaxation
    semi_relax = is_semi and rev_growth is not None and rev_growth > SEMI_RELAX_REV_GROWTH

    # Condition A: Listed >= 1 year
    list_date = parse_date(row.get(KEY_LIST_DATE) if KEY_LIST_DATE else None)
    if list_date and (TODAY - list_date.date()).days < 365:
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

    if not semi_relax:
        if ocf is not None and np_val is not None and np_val > 0:
            threshold = OCF_NP_THRESHOLD.get(period, 0.50)
            if ocf / np_val <= threshold:
                rejected["OCF/NP比率≤阈值"] += 1
                continue

    # Condition C: NP 门槛（按报告期折算）
    np_threshold = NP_MIN_BY_PERIOD.get(period, NP_MIN_ANNUAL)
    if np_val is not None and np_val < np_threshold:
        rejected["归母净利<阈值"] += 1
        continue

    # Condition D: Debt ratio < 70%
    debt = parse_pct(row.get(KEY_DEBT)) if KEY_DEBT else None
    if debt is not None and debt >= 70:
        rejected["负债率≥70%"] += 1
        continue

    # Condition E: Gross margin check
    if gm is not None:
        if semi_relax:
            if gm < SEMI_RELAX_GM:
                rejected["毛利率<15%(半导体高增长)"] += 1
                continue
        else:
            if gm < NORMAL_GM_FLOOR:
                rejected["毛利率<20%(非半导体)"] += 1
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
    "资产负债率_pct","每股收益_元"
]

rows_out = []
for code in sorted(final):
    row = final[code]

    # Parse values
    list_date = parse_date(row.get(KEY_LIST_DATE)) if KEY_LIST_DATE else None
    list_days = (TODAY - list_date.date()).days if list_date else None

    mcap = parse_num(row.get(KEY_MCAP)) if KEY_MCAP else None
    pe = parse_pct(row.get(KEY_PE)) if KEY_PE else None
    pb = parse_pct(row.get(KEY_PB)) if KEY_PB else None
    roe = parse_pct(row.get(KEY_ROE)) if KEY_ROE else None
    revenue = parse_num(row.get(KEY_REVENUE_ANNUAL)) if KEY_REVENUE_ANNUAL else None
    rev_g = parse_pct(row.get(KEY_REV_GROWTH)) if KEY_REV_GROWTH else None
    np_val = parse_num(row.get(KEY_NP)) if KEY_NP else None
    np_g = parse_pct(row.get(KEY_NP_GROWTH)) if KEY_NP_GROWTH else None
    ocf = parse_num(row.get(KEY_OCF)) if KEY_OCF else None
    gm = parse_pct(row.get(KEY_GM)) if KEY_GM else None
    rd = parse_pct(row.get(KEY_RD)) if KEY_RD else None
    debt = parse_pct(row.get(KEY_DEBT)) if KEY_DEBT else None
    eps = parse_pct(row.get(KEY_EPS)) if KEY_EPS else None

    # Calculate OCF/NP ratio
    ocf_np_ratio = None
    if ocf is not None and np_val is not None and np_val > 0:
        ocf_np_ratio = ocf / np_val

    rows_out.append({
        "股票代码": f'="{code}"',
        "股票名称": row.get("SECURITY_SHORT_NAME", ""),
        "所属行业": str(row.get(industry_key, "")) if industry_key else "",
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

# Core metrics
gms, rgs, rds, roes = [], [], [], []
for r in rows_out:
    for val, lst in [(gm, gms), (rev_g, rgs), (rd, rds), (roe, roes)]:
        if val is not None: lst.append(val)

print(f"\n  核心指标均值:")
if gms: print(f"    毛利率_pct 均值: {sum(gms)/len(gms):.2f}")
if rgs: print(f"    营收同比增速_pct 均值: {sum(rgs)/len(rgs):.2f}")
if rds: print(f"    研发费用占比_pct 均值: {sum(rds)/len(rds):.2f}")
if roes: print(f"    ROE_pct 均值: {sum(roes)/len(roes):.2f}")

# Check for truncation
truncated = [s for s in segment_counts if s[1] >= 200]
if truncated:
    print(f"\n  ⚠️ 截断警告：以下分段实际返回 >= 200，可能覆盖不全:")
    for name, rows, total in truncated:
        print(f"    {name}: {rows}/{total}")

print(f"\n{'='*60}")
print(f"✅ 完成: {csv_path}")
print(f"{'='*60}")
