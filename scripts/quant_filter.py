"""
模型二：量价精筛 — 配套脚本
对应指令: instructions/02-quant-v1.0.md (v1.0)

用法:
  python3 scripts/quant_filter.py                     # 全量模式，读取 pool/pool_<today>.csv
  python3 scripts/quant_filter.py --pool pool/pool_260518.csv  # 指定池文件
  python3 scripts/quant_filter.py --test              # 测试模式（内建 19 只标的）
  python3 scripts/quant_filter.py --test --no-cache   # 跳过缓存，强制重新拉取
  python3 scripts/quant_filter.py --refresh           # 清除今日全部缓存后重新拉取

缓存策略:
  同一标的 + 同一交易日 → 命中缓存（零 API 调用）
  日期变更或缓存不存在 → 自动拉取并写入缓存
  缓存目录: cache/daily/<code>_<YYMMDD>.pkl

版本追踪:
  脚本对应指令开发版，规则变更时同步更新，旧版本逻辑不保留
  指令稳定发布 v1.0 后更新此处版本号
"""
import os, json, time, csv, sys, argparse
from datetime import datetime
import requests
import pandas as pd
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import RateLimiter, DailyCache, PROJECT_ROOT

# ===================== 配置 =====================

API_KEY = os.environ.get("MX_APIKEY")
if not API_KEY:
    print("❌ 环境变量 MX_APIKEY 未设置")
    sys.exit(1)

BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
POOL_DIR = os.path.join(PROJECT_ROOT, "pool")
QUANT_DIR = os.path.join(PROJECT_ROOT, "quant")
cache = DailyCache()

# 内建测试标的（v1.1 验证用，覆盖 9 行业 × 大小市值）
TEST_CODES = [
    "688256", "301329", "300394", "300308", "002028",
    "300750", "002008", "688285", "002460", "300127",
    "688235", "920946", "688295", "002054", "601777",
    "603409", "600176", "301526", "300442",
]


_rate_limiter = RateLimiter()


# ===================== 数据拉取 =====================

def load_pool_codes(pool_path):
    """读取模型一池文件，返回 [(code, name), ...]"""
    codes = []
    with open(pool_path, encoding='utf-8-sig') as f:
        for row in csv.DictReader(f):
            code = row['股票代码'].strip('=\"')
            name = row['股票名称']
            codes.append((code, name))
    return codes


# 字段别名：某些股票 nameMap 中的字段名可能不同，按优先级尝试匹配
FIELD_ALIASES = {
    "成交量": ["成交量", "成交数量", "成交股数"],
    "换手率": ["换手率", "换手"],
    "开盘价": ["开盘价", "开盘"],
    "最高价": ["最高价", "最高"],
    "最低价": ["最低价", "最低"],
    "收盘价": ["收盘价", "收盘"],
}


def _find_field(ind_map, field_key):
    """从 ind_map 中按别名查找字段索引，找不到返回 None"""
    aliases = FIELD_ALIASES.get(field_key, [field_key])
    for alias in aliases:
        if alias in ind_map:
            return ind_map[alias]
    return None


def _resolve_fields(ind_map, required_fields):
    """解析所需字段，返回 {field_key: col_index}。缺失必填字段时返回 None + 错误信息"""
    resolved = {}
    for f in required_fields:
        idx = _find_field(ind_map, f)
        if idx is None:
            return None, f"nameMap缺少字段'{f}'(可用: {list(ind_map.keys())})"
        resolved[f] = idx
    return resolved, None


def fetch_daily(code, name, datestr, use_cache=True):
    """
    拉取近200个交易日日线。
    缓存优先：同一标的+同一日期命中缓存则直接返回，零 API 调用。
    返回 (DataFrame, source_str) 或 (None, err_msg)
    source_str: "cache" | "api"
    """
    if use_cache:
        cached = cache.load(code, datestr)
        if cached is not None:
            return cached, "cache"

    query = f"{name}近200个交易日每日开盘价、最高价、最低价、收盘价、成交量、换手率"
    headers = {"Content-Type": "application/json", "apikey": API_KEY}
    payload = {"toolQuery": query, "toolType": "query_tool"}

    for attempt in range(3):  # 最多 3 次尝试
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
        except Exception as e:
            return None, f"网络异常: {e}"

        code_val = result.get("code", -1)

        if code_val == 0:
            _rate_limiter.reset_fails()
            break

        if code_val == 112:
            # 频率限制，等久一点重试
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
        # 其他未知 code，重试一次
        if attempt == 0:
            _rate_limiter.wait(is_fail=True)
            continue
        return None, f"API返回code={code_val}"

    # 解析数据
    try:
        tables = result["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
        hist = tables[1]
        raw = hist["rawTable"]
        nm = hist["nameMap"]
    except (KeyError, IndexError, TypeError):
        return None, "数据结构异常(缺dataTableDTOList[1])"

    ind_map = {v: k for k, v in nm.items() if k != "headNameSub"}
    fields, err = _resolve_fields(ind_map, ["成交量", "换手率", "开盘价", "最高价", "最低价", "收盘价"])
    if err:
        return None, err

    dates = raw["headName"]

    rows = []
    for i, d in enumerate(dates):
        vol_str = raw[fields["成交量"]][i]
        turn_str = raw[fields["换手率"]][i]
        open_str = raw[fields["开盘价"]][i]
        high_str = raw[fields["最高价"]][i]
        low_str = raw[fields["最低价"]][i]
        close_str = raw[fields["收盘价"]][i]
        if "-" in (vol_str, turn_str, open_str, high_str, low_str, close_str):
            continue

        rows.append({
            "date": d,
            "open": float(open_str),
            "high": float(high_str),
            "low": float(low_str),
            "close": float(close_str),
            "volume": float(vol_str),
            "turnover": float(turn_str),
        })

    if not rows:
        return None, "无有效交易日数据(可能长期停牌)"

    df = pd.DataFrame(rows)
    df = df.sort_values("date").reset_index(drop=True)

    # 写入缓存（始终写，--no-cache 只跳读不跳写）
    cache.save(code, datestr, df)

    # 正常完成，等基础间隔
    _rate_limiter.wait(is_fail=False)
    return df, "api"


# ===================== 指标计算 =====================

def calc_indicators(df):
    """对单只股票的日线 DataFrame 计算全部技术指标"""
    df = df.copy()
    df["MA20"] = df["close"].rolling(20).mean()
    df["MA60"] = df["close"].rolling(60).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ma20"] = df["volume"].rolling(20).mean()
    df["amount"] = df["volume"] * df["close"]
    df["amount_ma20"] = df["amount"].rolling(20).mean()
    df["量比"] = df["volume"] / df["vol_ma5"].shift(1)
    df["缩量程度"] = df["vol_ma5"] / df["vol_ma20"]
    df["距MA20"] = (df["close"] - df["MA20"]) / df["MA20"] * 100
    df["距MA60"] = (df["close"] - df["MA60"]) / df["MA60"] * 100
    df["近5日涨幅"] = df["close"].pct_change(5) * 100
    df["MA20_MA60差"] = (df["MA20"] - df["MA60"]) / df["MA60"] * 100
    return df


# ===================== 筛选逻辑 =====================

def screen(df):
    """
    按指令 v1.0 三层筛选规则判断。
    返回 (category, description) 或 (None, reject_reason)
    """
    latest = df.iloc[-1]
    n = len(df)

    # MA20 检查
    if pd.isna(latest["MA20"]):
        return None, f"MA20=NaN (仅{n}有效日)", ""

    ma60_ok = not pd.isna(latest["MA60"])

    # ---- 前置排除 ----
    if ma60_ok and latest["MA20"] < latest["MA60"]:
        return None, "空头排列(MA20<MA60)", ""

    if pd.notna(latest["近5日涨幅"]) and latest["近5日涨幅"] > 20:
        return None, f"近5日涨幅{latest['近5日涨幅']:.1f}%>20%", ""

    high60 = df["high"].iloc[-60:].max() if n >= 60 else df["high"].max()
    from_high = (latest["close"] - high60) / high60 * 100
    chg20 = None
    if n >= 21:
        chg20 = (latest["close"] - df["close"].iloc[-21]) / df["close"].iloc[-21] * 100
    if from_high < -30 and (chg20 is not None and chg20 < -15):
        return None, f"距60日高点{from_high:.1f}%且近20日{chg20:.1f}%(持续下跌)", ""

    # ---- A类：缩量回调至均线支撑 ----
    a_ok, a_reasons = True, []
    if not (df["量比"].iloc[-20:].max() > 1.5):
        a_ok = False; a_reasons.append("A1:近20日无量比>1.5")
    if pd.isna(latest["缩量程度"]) or latest["缩量程度"] >= 0.85:
        a_ok = False; a_reasons.append(f"A2:缩量程度{latest['缩量程度']:.2f}>=0.85")
    near_ma20 = pd.notna(latest["距MA20"]) and (-5 <= latest["距MA20"] <= 3)
    near_ma60 = ma60_ok and pd.notna(latest["距MA60"]) and (-5 <= latest["距MA60"] <= 3)
    if not (near_ma20 or near_ma60):
        a_ok = False; a_reasons.append(f"A3:距MA20={latest['距MA20']:.1f}% 距MA60={latest['距MA60']:.1f}%")
    if ma60_ok and pd.notna(latest["MA20_MA60差"]) and latest["MA20_MA60差"] <= -3:
        a_ok = False; a_reasons.append(f"A4:MA20_MA60差={latest['MA20_MA60差']:.1f}%<=-3")
    if a_ok:
        # Build detailed reason
        anchor = "MA20" if near_ma20 else "MA60"
        dist = latest["距MA20"] if near_ma20 else latest["距MA60"]
        results = [("A", f"缩量{latest['缩量程度']:.2f}，距{anchor}={dist:.1f}%，近20日有放量", anchor)]
    else:
        results = []

    # ---- B类：底部盘整后放量突破 ----
    if ma60_ok:
        b_ok, b_reasons = True, []
        last10 = df.iloc[-10:]
        days_sticky = int((last10["MA20_MA60差"].abs() < 5).sum())
        if days_sticky < 7:
            b_ok = False; b_reasons.append(f"B1:近10日仅{days_sticky}天均线粘合")
        if not (df["量比"].iloc[-3:].max() > 1.5):
            b_ok = False; b_reasons.append("B2:近3日无量比>1.5")
        if not (latest["close"] > latest["MA60"]):
            b_ok = False; b_reasons.append("B3:收盘价<=MA60")
        if pd.notna(latest["近5日涨幅"]) and latest["近5日涨幅"] >= 15:
            b_ok = False; b_reasons.append(f"B4:近5日涨幅{latest['近5日涨幅']:.1f}%>=15%")
        if pd.isna(latest["amount_ma20"]) or latest["amount_ma20"] <= 1_000_000:
            b_ok = False; b_reasons.append(f"B5:近20日均成交额{latest['amount_ma20']/1e4:.0f}万<=100万")
        if b_ok:
            results.append(("B", f"均线粘合{days_sticky}/10天，近3日放量，站上MA60", ""))

    # ---- C类：上升趋势中的健康回踩 ----
    if ma60_ok:
        c_ok, c_reasons = True, []
        if not (latest["MA20"] > latest["MA60"]):
            c_ok = False; c_reasons.append("C1:非多头排列")
        if pd.isna(latest["缩量程度"]) or latest["缩量程度"] >= 0.85:
            c_ok = False; c_reasons.append(f"C2:缩量程度{latest['缩量程度']:.2f}>=0.85")
        if pd.isna(latest["距MA20"]) or not (-5 <= latest["距MA20"] <= 3):
            c_ok = False; c_reasons.append(f"C3:距MA20={latest['距MA20']:.1f}%不在[-5,3]")
        low20 = df["low"].iloc[-20:].min()
        if not (latest["close"] > low20 * 1.05):
            c_ok = False; c_reasons.append("C4:收盘价<=20日最低价×1.05")
        if pd.notna(latest["近5日涨幅"]) and latest["近5日涨幅"] >= 15:
            c_ok = False; c_reasons.append(f"C5:近5日涨幅{latest['近5日涨幅']:.1f}%>=15%")
        if c_ok:
            results.append(("C", f"多头排列，缩量{latest['缩量程度']:.2f}，回踩MA20={latest['距MA20']:.1f}%", "MA20"))

    if not results:
        all_reasons = a_reasons + (b_reasons if ma60_ok else []) + (c_reasons if ma60_ok else [])
        return None, "; ".join(all_reasons[:3]) if all_reasons else "无类别接近", ""

    # C > A > B 优先级
    for cat in ["C", "A", "B"]:
        for r_cat, r_desc, r_ma in results:
            if r_cat == cat:
                return r_cat, r_desc, r_ma

    return None, "未知", ""


# ===================== 输出 =====================

def write_csv(results, quant_path):
    """按指令阶段四规范输出 CSV"""
    os.makedirs(os.path.dirname(quant_path), exist_ok=True)
    today_str = datetime.now().strftime("%Y-%m-%d")

    columns = [
        "股票代码", "股票名称", "通过类别", "入选原因", "entry_ma",
        "收盘价", "MA20", "MA60",
        "量比", "缩量程度", "近5日涨幅_pct",
        "距MA20_pct", "距MA60_pct", "MA20_MA60差_pct",
        "入选日期",
    ]

    with open(quant_path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(columns)
        for r in results:
            writer.writerow([
                f'="{r["code"]}"',
                r["name"],
                r["category"],
                r["reason"],
                r.get("entry_ma", ""),
                f'{r["close"]:.2f}',
                f'{r["MA20"]:.2f}' if r["MA20"] != "" else "",
                f'{r["MA60"]:.2f}' if r["MA60"] != "" else "",
                f'{r["量比"]:.2f}' if r["量比"] != "" else "",
                f'{r["缩量程度"]:.2f}' if r["缩量程度"] != "" else "",
                f'{r["近5日涨幅"]:.2f}' if r["近5日涨幅"] != "" else "",
                f'{r["距MA20"]:.2f}' if r["距MA20"] != "" else "",
                f'{r["距MA60"]:.2f}' if r["距MA60"] != "" else "",
                f'{r["MA20_MA60差"]:.2f}' if r["MA20_MA60差"] != "" else "",
                today_str,
            ])

    print(f"\n📄 精选池已输出: {quant_path}")


def print_summary(total, pull_ok, pull_fail, data_insufficient, results_all, cache_hits=0, api_calls=0):
    """终端输出执行摘要"""
    print()
    print("=" * 70)
    print("模型二执行摘要 — 量价精筛 (指令 v1.0)")
    print("=" * 70)
    print(f"  输入候选池: {total} 只")
    print(f"  数据来源:   缓存命中 {cache_hits} 只 + API 拉取 {api_calls} 只")
    print(f"  成功拉取行情: {pull_ok} 只（失败: {pull_fail} 只）")
    print(f"  数据不足跳过: {data_insufficient} 只 (<20天)")
    print(f"  通过筛选: {len(results_all)} 只")
    by_cat = {"A": 0, "B": 0, "C": 0}
    for r in results_all:
        by_cat[r["category"]] += 1
    print(f"    A类（缩量回调）: {by_cat['A']} 只")
    print(f"    B类（放量突破）: {by_cat['B']} 只")
    print(f"    C类（趋势回踩）: {by_cat['C']} 只")

    if results_all:
        print()
        header = f"{'代码':<8} {'名称':<8} {'类':<3} {'入选原因'}"
        print(header)
        print("-" * 90)
        for r in results_all:
            print(f"{r['code']:<8} {r['name']:<8} {r['category']:<3} {r['reason']}")

        print()
        detail_header = f"{'代码':<8} {'收盘':>8} {'MA20':>8} {'距MA20%':>8} {'量比':>6} {'缩量':>6} {'5日涨%':>7} {'MA20_60差%':>9}"
        print(detail_header)
        print("-" * len(detail_header.encode("utf-8")))
        for r in results_all:
            def v(x, fmt_str):
                try: return f"{float(x):{fmt_str}}"
                except: return f"{str(x):>{len(fmt_str)-1}}"
            print(f"{r['code']:<8} "
                  f"{v(r['close'], '8.2f')} {v(r['MA20'], '8.2f')} {v(r['距MA20'], '8.1f')} "
                  f"{v(r['量比'], '6.2f')} {v(r['缩量程度'], '6.2f')} {v(r['近5日涨幅'], '7.1f')} "
                  f"{v(r['MA20_MA60差'], '9.1f')}")


# ===================== MAIN =====================

def main():
    parser = argparse.ArgumentParser(description="模型二：量价精筛")
    parser.add_argument("--pool", help="模型一池文件路径（默认取当天 pool/pool_YYMMDD.csv）")
    parser.add_argument("--test", action="store_true", help="测试模式，使用内建 19 只标的")
    parser.add_argument("--quant", help="输出路径（默认 quant/quant_YYMMDD.csv）")
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存，强制从 API 拉取")
    parser.add_argument("--refresh", action="store_true", help="清除今日缓存后重新拉取")
    args = parser.parse_args()

    today_yy = datetime.now().strftime("%y%m%d")
    today_full = datetime.now().strftime("%Y-%m-%d")
    use_cache = not args.no_cache

    if args.test:
        codes = [(c, c) for c in TEST_CODES]  # name placeholder, 下面反查
        quant_path = args.quant or f"{QUANT_DIR}/quant_{today_yy}_test.csv"
        mode = "测试"
    else:
        pool_path = args.pool or f"{POOL_DIR}/pool_{today_yy}.csv"
        if not os.path.exists(pool_path):
            print(f"❌ 池文件不存在: {pool_path}")
            print("   可用 --pool 指定路径，或 --test 进入测试模式")
            sys.exit(1)
        codes = load_pool_codes(pool_path)
        quant_path = args.quant or f"{QUANT_DIR}/quant_{today_yy}.csv"
        mode = "全量"

    print("=" * 70)
    print(f"模型二：量价精筛 (指令 v1.0 / 脚本 v1.3)")
    print(f"模式: {mode}  |  标的: {len(codes)} 只  |  输出: {quant_path}")
    print(f"缓存: {'关闭' if args.no_cache else '开启'}  |  目录: {cache.cache_dir}")
    print("=" * 70)

    # 清理超过 5 天的旧缓存，防止无限累积
    old_cleaned = cache.cleanup_old(keep_days=5)
    if old_cleaned > 0:
        print(f"🧹 已清理 {old_cleaned} 个超过5天的旧缓存文件")

    if args.refresh:
        n = cache.clear_today(today_yy)
        print(f"🧹 已清除今日缓存 {n} 个文件\n")

    # 测试模式需反查名称
    if args.test:
        pool_path = f"{POOL_DIR}/pool_{today_yy}.csv"
        name_map = {}
        if os.path.exists(pool_path):
            with open(pool_path, encoding='utf-8-sig') as f:
                for row in csv.DictReader(f):
                    name_map[row['股票代码'].strip('=\"')] = row['股票名称']
        codes = [(c, name_map.get(c, c)) for c in TEST_CODES]

    results_all = []
    pull_ok = 0
    pull_fail = 0
    data_insufficient = 0
    cache_hits = 0
    api_calls = 0
    fatal_stop = False
    retry_queue = []

    for i, (code, name) in enumerate(codes):
        if fatal_stop:
            print(f"[{i+1}/{len(codes)}] {code} {name} ... ⏭️ 因上游致命错误跳过")
            pull_fail += 1
            continue

        print(f"[{i+1}/{len(codes)}] {code} {name} ...", end=" ", flush=True)

        df, source = fetch_daily(code, name, today_yy, use_cache=use_cache)

        if df is None:
            if "112" in str(source):
                print(f"❌ {source} → 加入重试队列")
                retry_queue.append((code, name))
            else:
                print(f"❌ {source}")
                pull_fail += 1
                if source.startswith("FATAL:"):
                    fatal_stop = True
            continue

        if source == "cache":
            cache_hits += 1
        else:
            api_calls += 1

        n_days = len(df)
        if n_days < 20:
            print(f"⏭️ 数据不足({n_days}天<20)")
            data_insufficient += 1
            continue

        pull_ok += 1
        df = calc_indicators(df)
        category, reason, entry_ma = screen(df)
        latest = df.iloc[-1]

        if category:
            print(f"✅ {category}类 — {reason}")
            results_all.append({
                "code": code, "name": name,
                "category": category,
                "reason": reason,
                "entry_ma": entry_ma,
                "close": round(latest["close"], 2),
                "MA20": round(latest["MA20"], 2) if pd.notna(latest["MA20"]) else "",
                "MA60": round(latest["MA60"], 2) if pd.notna(latest["MA60"]) else "",
                "量比": round(latest["量比"], 2) if pd.notna(latest["量比"]) else "",
                "缩量程度": round(latest["缩量程度"], 2) if pd.notna(latest["缩量程度"]) else "",
                "近5日涨幅": round(latest["近5日涨幅"], 2) if pd.notna(latest["近5日涨幅"]) else "",
                "距MA20": round(latest["距MA20"], 2) if pd.notna(latest["距MA20"]) else "",
                "距MA60": round(latest["距MA60"], 2) if pd.notna(latest["距MA60"]) else "",
                "MA20_MA60差": round(latest["MA20_MA60差"], 2) if pd.notna(latest["MA20_MA60差"]) else "",
            })
        else:
            print(f"❌ {reason[:80]}")

    # ── 失败重试队列 ──
    if retry_queue:
        print(f"\n{'='*70}")
        print(f"🔄 重试 {len(retry_queue)} 只频率限制失败的标的...")
        print(f"{'='*70}")
        time.sleep(10)

        for i, (code, name) in enumerate(retry_queue):
            print(f"[重试 {i+1}/{len(retry_queue)}] {code} {name} ...", end=" ", flush=True)
            df, source = fetch_daily(code, name, today_yy, use_cache=False)
            if df is None:
                print(f"❌ 重试仍失败: {source}")
                pull_fail += 1
                continue

            api_calls += 1
            n_days = len(df)
            if n_days < 20:
                print(f"⏭️ 数据不足({n_days}天<20)")
                data_insufficient += 1
                continue

            pull_ok += 1
            df = calc_indicators(df)
            category, reason, entry_ma = screen(df)
            latest = df.iloc[-1]

            if category:
                print(f"✅ {category}类 — {reason}")
                results_all.append({
                    "code": code, "name": name,
                    "category": category,
                    "reason": reason,
                    "entry_ma": entry_ma,
                    "close": round(latest["close"], 2),
                    "MA20": round(latest["MA20"], 2) if pd.notna(latest["MA20"]) else "",
                    "MA60": round(latest["MA60"], 2) if pd.notna(latest["MA60"]) else "",
                    "量比": round(latest["量比"], 2) if pd.notna(latest["量比"]) else "",
                    "缩量程度": round(latest["缩量程度"], 2) if pd.notna(latest["缩量程度"]) else "",
                    "近5日涨幅": round(latest["近5日涨幅"], 2) if pd.notna(latest["近5日涨幅"]) else "",
                    "距MA20": round(latest["距MA20"], 2) if pd.notna(latest["距MA20"]) else "",
                    "距MA60": round(latest["距MA60"], 2) if pd.notna(latest["距MA60"]) else "",
                    "MA20_MA60差": round(latest["MA20_MA60差"], 2) if pd.notna(latest["MA20_MA60差"]) else "",
                })
            else:
                print(f"❌ {reason[:80]}")

    # 摘要
    print_summary(len(codes), pull_ok, pull_fail, data_insufficient, results_all, cache_hits, api_calls)

    # 写 CSV
    if results_all:
        write_csv(results_all, quant_path)


if __name__ == "__main__":
    main()
