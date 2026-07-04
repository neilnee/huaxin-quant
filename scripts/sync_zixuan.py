#!/usr/bin/env python3
"""
sync_zixuan.py — 核心池 → 东方财富自选股同步

读取 core_pool.csv（权威列表），对比东方财富自选股"全部"分组，
自动执行增删操作，更新 signals/zixuan.csv。

用法:
  python3 scripts/sync_zixuan.py              # 交互确认后执行
  python3 scripts/sync_zixuan.py --yes        # 跳过确认
  python3 scripts/sync_zixuan.py --dry-run    # 仅打印差异，不执行

集成: tracker.py 池子维护后自动调用（如 zixuan.csv 过期）
"""

import os, sys, csv, json, time, random, argparse
from datetime import datetime
import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT

API_KEY = os.environ.get("MX_APIKEY")
if not API_KEY:
    print("❌ 环境变量 MX_APIKEY 未设置")
    sys.exit(1)

SIGNALS_DIR = f"{PROJECT_ROOT}/signals"
CORE_POOL_PATH = f"{SIGNALS_DIR}/core_pool.csv"
ZIXUAN_PATH = f"{SIGNALS_DIR}/zixuan.csv"

QUERY_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/self-select/get"
MANAGE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/self-select/manage"


def query_watchlist():
    """查询东方财富自选股'全部'分组，返回 {code: name}"""
    headers = {"Content-Type": "application/json", "apikey": API_KEY}
    try:
        resp = requests.post(QUERY_URL, headers=headers, json={}, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        print(f"❌ 查询自选股失败: {e}")
        return None

    if result.get("status") != 0 and result.get("code") != 0:
        print(f"❌ 查询失败: {result.get('message', '未知')}")
        return None

    data = result.get("data", {})
    result_data = data.get("allResults", {}).get("result", {})
    data_list = result_data.get("dataList", [])

    watchlist = {}
    for stock in data_list:
        code = stock.get("SECURITY_CODE", "")
        name = stock.get("SECURITY_SHORT_NAME", "")
        if code:
            watchlist[code] = name

    return watchlist


def manage_watchlist(code, action):
    """添加或删除自选股。action: 'add' | 'delete'"""
    headers = {"Content-Type": "application/json", "apikey": API_KEY}

    if action == "add":
        query = f"把{code}添加到我的自选股列表"
    else:
        query = f"把{code}从我的自选股列表删除"

    data = {"query": query}

    for attempt in range(2):
        try:
            resp = requests.post(MANAGE_URL, headers=headers, json=data, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if result.get("status") == 0:
                return True, result.get("message", "ok")
            if result.get("code") == 112:
                if attempt == 0:
                    time.sleep(3 + random.uniform(0, 1))
                    continue
                return False, "频率限制(重试仍失败)"
            return False, result.get("message", f"code={result.get('code')}")
        except Exception as e:
            if attempt == 0:
                time.sleep(2)
                continue
            return False, str(e)

    return False, "未知错误"


def read_b1_signals():
    """读取最新 signals JSON，返回触发了 B1 的 {code: name}"""
    # 找最新的 signals JSON
    signals_dir = SIGNALS_DIR
    json_files = sorted(
        [f for f in os.listdir(signals_dir) if f.startswith("signals_") and f.endswith(".json")],
        reverse=True
    )
    if not json_files:
        print("⚠️ 无 signals JSON，回退到 core_pool.csv")
        return None

    latest = os.path.join(signals_dir, json_files[0])
    with open(latest, "r") as f:
        data = json.load(f)

    b1_stocks = {}
    for s in data.get("stocks", []):
        meta = s.get("_meta", {})
        # ETF 不参与
        if meta.get("asset_type") == "ETF":
            continue
        # 找触发的 B1 信号
        for sig in s.get("buy_signals", []):
            if "B1" in sig.get("signal", "") and sig.get("hit"):
                code = meta["code"]
                name = meta["name"]
                b1_stocks[code] = name
                break

    print(f"📡 {json_files[0]}: {len(b1_stocks)} 只 B1 触发")
    return b1_stocks


def read_zixuan_cache():
    """读取本地 zixuan.csv 缓存"""
    if not os.path.exists(ZIXUAN_PATH):
        return {}
    codes = {}
    with open(ZIXUAN_PATH, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row["code"].strip()
            codes[code] = row.get("name", "")
    return codes


def write_zixuan_cache(watchlist, core_pool):
    """更新本地 zixuan.csv"""
    today = datetime.now().strftime("%Y-%m-%d")
    os.makedirs(SIGNALS_DIR, exist_ok=True)

    rows = []
    for code, name in sorted(watchlist.items()):
        rows.append({
            "code": code,
            "name": name,
            "added_date": today,
            "last_signal_date": today,
            "signal_type": "sync",
        })

    with open(ZIXUAN_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["code", "name", "added_date", "last_signal_date", "signal_type"])
        writer.writeheader()
        writer.writerows(rows)

    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="核心池 → 东方财富自选股同步")
    parser.add_argument("--yes", action="store_true", help="跳过交互确认")
    parser.add_argument("--dry-run", action="store_true", help="仅打印差异，不执行操作")
    args = parser.parse_args()

    # 1. 读取最新 B1 买入信号（谁触发了买点，谁就应该在自选股里）
    b1_stocks = read_b1_signals()
    if b1_stocks is None:
        print("❌ 无 signals JSON 且无 core_pool.csv，无法继续")
        sys.exit(1)
    print(f"📋 B1 买点: {len(b1_stocks)} 只")

    # 安全阀：B1=0 且有待删除项时，拒绝自动清空
    if len(b1_stocks) == 0 and len(read_zixuan_cache()) > 0:
        print("\n⚠️ B1=0，但 zixuan.csv 缓存非空。")
        print("   可能是 signals JSON 数据不完整（如 --test 模式），拒绝自动清空。")
        print("   确认数据正确后，手动删 zixuan.csv 再运行。")
        sys.exit(1)

    # 2. 读取本地缓存（zixuan.csv = 系统管理的自选股记录）
    zixuan_cache = read_zixuan_cache()
    print(f"📊 zixuan.csv 缓存: {len(zixuan_cache)} 只（系统管理）")

    # 3. 计算差异
    b1_codes = set(b1_stocks.keys())
    managed_codes = set(zixuan_cache.keys())

    # 待添加：触发了B1但从未同步过
    to_add = {c: b1_stocks[c] for c in (b1_codes - managed_codes)}

    # 待删除：之前同步过但B1已消失
    to_delete = {c: zixuan_cache[c] for c in (managed_codes - b1_codes)}

    print(f"\n{'='*50}")
    if not to_add and not to_delete:
        print("✅ 已同步，无需操作")
        return

    if to_add:
        print(f"\n📥 待添加 ({len(to_add)} 只):")
        for code, name in sorted(to_add.items()):
            print(f"  + {code} {name}")

    if to_delete:
        print(f"\n📤 待删除 ({len(to_delete)} 只):")
        for code, name in sorted(to_delete.items()):
            print(f"  - {code} {name}")

    if args.dry_run:
        print(f"\n🔍 --dry-run，不执行实际操作")
        return

    # 4. 交互确认
    if not args.yes:
        print()
        choice = input("确认执行以上操作？(y/n): ").strip().lower()
        if choice != 'y':
            print("⚠️ 已取消")
            return

    # 5. 执行删除（先删后加，避免重复）
    success_del = 0
    fail_del = 0
    if to_delete:
        print(f"\n🗑️  删除 {len(to_delete)} 只...")
        for i, (code, name) in enumerate(sorted(to_delete.items())):
            print(f"  [{i+1}/{len(to_delete)}] {code} {name} ...", end=" ")
            ok, msg = manage_watchlist(code, "delete")
            if ok:
                print("✅")
                success_del += 1
            else:
                print(f"❌ {msg}")
                fail_del += 1
            if (i + 1) % 5 == 0 and i + 1 < len(to_delete):
                time.sleep(3 + random.uniform(0, 2))  # 每5次歇一下

    # 6. 执行添加
    success_add = 0
    fail_add = 0
    if to_add:
        # 删完后稍等
        if to_delete:
            time.sleep(2)
        print(f"\n📥 添加 {len(to_add)} 只...")
        for i, (code, name) in enumerate(sorted(to_add.items())):
            print(f"  [{i+1}/{len(to_add)}] {code} {name} ...", end=" ")
            ok, msg = manage_watchlist(code, "add")
            if ok:
                print("✅")
                success_add += 1
            else:
                print(f"❌ {msg}")
                fail_add += 1
            if (i + 1) % 5 == 0 and i + 1 < len(to_add):
                time.sleep(3 + random.uniform(0, 2))

    # 7. 更新本地缓存（zixuan.csv = 当前 B1 列表）
    n = write_zixuan_cache(b1_stocks, b1_stocks)
    print(f"\n✅ zixuan.csv 已更新 ({n} 只)")

    # 8. 摘要
    print(f"\n{'='*50}")
    print(f"B1 自选股同步完成:")
    print(f"  当前 B1: {len(b1_stocks)} 只")
    print(f"  添加: {success_add} 成功 / {fail_add} 失败")
    print(f"  删除: {success_del} 成功 / {fail_del} 失败")


if __name__ == "__main__":
    main()
