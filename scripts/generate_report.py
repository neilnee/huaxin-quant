#!/usr/bin/env python3
"""
generate_report.py — 从 tracker JSON + valuation_ranking.csv 自动生成每日信号报告 markdown
用法: python3 scripts/generate_report.py [260521]
"""

import json, csv, os, sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT, VALUATION_RANKING_PATH

SIGNALS_DIR = f"{PROJECT_ROOT}/signals"
RANKING_PATH = VALUATION_RANKING_PATH


def load_data(datestr):
    json_path = f"{SIGNALS_DIR}/signals_{datestr}.json"
    if not os.path.exists(json_path):
        print(f"❌ JSON 不存在: {json_path}，请先运行 tracker.py")
        sys.exit(1)
    with open(json_path) as f:
        data = json.load(f)

    ranking = {}
    if os.path.exists(RANKING_PATH):
        with open(RANKING_PATH, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row["股票代码"].strip('="')
                ranking[code] = row

    # 加载 core_pool 用于 T0 检查
    core_pool = {}
    core_path = f"{SIGNALS_DIR}/core_pool.csv"
    if os.path.exists(core_path):
        with open(core_path, encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row["code"].replace('="', "").replace('"', "")
                core_pool[code] = {
                    "entry_pattern": row.get("entry_pattern", "").strip(),
                    "entry_ma": row.get("entry_ma", "").strip(),
                }

    return data, ranking, core_pool


def fmt(v, default="-"):
    if v is None or v == "": return default
    try: return f"{float(v):.2f}"
    except: return str(v)


def build_tables(data, ranking, core_pool):
    positions = {}
    if os.path.exists(f"{SIGNALS_DIR}/positions.csv"):
        with open(f"{SIGNALS_DIR}/positions.csv", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row["code"].replace('="', "").replace('"', "")
                positions[code] = row

    batches = {}
    if os.path.exists(f"{SIGNALS_DIR}/batches.csv"):
        with open(f"{SIGNALS_DIR}/batches.csv", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = row["code"].replace('="', "").replace('"', "")
                if code not in batches:
                    batches[code] = []
                batches[code].append(row)

    held_rows = []
    unheld_rows = []

    for s in data["stocks"]:
        meta = s["_meta"]
        code = meta["code"]
        name = meta["name"]
        hold = meta["hold_status"]
        close = meta.get("close")

        pos = positions.get(code, {})
        manual_tag = pos.get("manual_tag", "")
        is_etf = pos.get("asset_type", "") == "ETF"

        val = ranking.get(code, {})
        conserv = val.get("悲观估值_元", "")
        optim = val.get("乐观估值_元", "")
        base = val.get("基准估值_元", "")
        discount = val.get("安全边际折扣率", "")

        # B1 独立计算（不依赖 tracker.py 的 buy_signals，因其可能用错误的保守估值）
        ind = s.get("indicators", {})
        close_val = meta.get("close") or 0
        shrink_val = ind.get("shrink") or 999
        d20 = ind.get("dist_ma20")
        d60 = ind.get("dist_ma60")
        near_ma = (-5 <= (d20 or -999) <= 3) or (-5 <= (d60 or -999) <= 3)

        b1 = False
        b0 = False
        t0 = False
        if conserv and base and close_val:
            try:
                price_ok = float(conserv) <= float(close_val) <= float(base)
                shrink_ok = float(shrink_val) < 0.8
                b1 = price_ok and shrink_ok and near_ma
                b0 = shrink_ok and near_ma and not b1
            except (ValueError, TypeError):
                pass
        else:
            b0 = bool(shrink_val < 0.8 and near_ma)

        # T0: 独立计算买入逻辑有效性（不依赖 tracker JSON）
        t0 = False
        if b1:
            cp = core_pool.get(code, {})
            ep = cp.get("entry_pattern", "")
            ema = cp.get("entry_ma", "")
            if ep == "A" and ema == "MA60":
                ma60 = ind.get("MA60")
                t0 = ma60 is not None and close_val < ma60
            elif ep == "A" and ema == "MA20":
                ma20 = ind.get("MA20")
                t0 = ma20 is not None and close_val < ma20
            elif ep == "C":
                slope = ind.get("ma20_slope") or 0
                t0 = slope < -0.2

        # signals
        buy_flags = []
        sell_flags = [sig["signal"][:2] for sig in s["sell_signals"] if sig["hit"]]
        warns = len(s["warnings"]) > 0

        if b1: buy_flags.append("🔵B1")
        elif b0: buy_flags.append("🟢B0")
        if sell_flags: buy_flags.append("🔴S")
        if t0: buy_flags.append("⚠️T0")

        # batch info — 从 tracker JSON 中获取止损价，分散到各列
        batch_rows = []
        for bsig in s.get("batch_signals", []):
            b_cost = bsig.get("cost_price", 0)
            b_shares = bsig.get("shares", 0)
            b_pnl = bsig.get("pnl_pct")
            pnl_str = f"{b_pnl:+.1f}%" if b_pnl is not None else "-"

            hard_stop = ""
            for sig in bsig.get("signals", []):
                if "硬止损" in sig["signal"]:
                    hard_stop = sig["conditions"].get("trigger_price", "")
                    break

            batch_rows.append({
                "id": f"↳ 🟠{bsig['batch_id']}",
                "logic": bsig.get("entry_logic", ""),
                "cost": f"@{b_cost}",
                "shares": f"{b_shares}股",
                "pnl": pnl_str,
                "stop": f"止损{hard_stop}" if hard_stop else "",
            })

        # signal string
        signal_str = " ".join(buy_flags) if buy_flags else "-"

        # valuation range
        val_range = f"{conserv}~{optim}" if conserv and optim else "-~-"
        base_str = fmt(base) if base else "-"

        # price position (用悲观估值和乐观估值，而非仅基准)
        pos_str = "-"
        if close and conserv and optim and base != "-":
            try:
                c, p, o, b = float(close), float(conserv), float(optim), float(base)
                if c < p: pos_str = "深度低估"
                elif c < b: pos_str = "悲观~基准"
                elif c < o: pos_str = "基准~乐观"
                else: pos_str = "远超乐观"
            except: pass
        elif close and base and base != "-":
            try:
                ratio = float(close) / float(base)
                if ratio < 0.8: pos_str = "深度低估"
                elif ratio < 1.0: pos_str = "悲观~基准"
                elif ratio < 1.05: pos_str = "接近基准"
                elif ratio < 1.3: pos_str = "基准~乐观"
                else: pos_str = "远超乐观"
            except: pass

        # status
        if manual_tag:
            status_icon = "🏷️"
            status_desc = manual_tag
        elif t0 and b1:
            status_icon = "🔵"
            status_desc = "估值买点+⚠️T0"
        elif b1:
            status_icon = "🔵"
            status_desc = "估值买点"
        elif b0:
            status_icon = "🟢"
            if val and pos_str == "远超乐观":
                status_desc = "形态到位 ✗贵"
            elif val and pos_str == "深度低估":
                status_desc = "形态到位 ⚡低价"
            elif val:
                status_desc = "形态到位(有估值)"
            else:
                status_desc = "形态到位"
        else:
            status_icon = "⚪"
            status_desc = "-"

        row = {
            "status_icon": status_icon,
            "status_desc": status_desc,
            "name": name,
            "code": code,
            "hold": hold,
            "close": fmt(close),
            "val_range": val_range,
            "base": base_str,
            "discount": discount if discount else "-",
            "pos_str": pos_str,
            "signal_str": signal_str,
            "batch_rows": batch_rows,
            "b1": b1, "b0": b0, "t0": t0,
            "discount_num": float(discount) if discount else 0,
        }

        if hold != "未持仓":
            held_rows.append(row)
        else:
            unheld_rows.append(row)

    # Sort unheld: B1 (by discount desc), then B0 (by discount desc), then rest
    def sort_key(r):
        if r["b1"]: return (0, -r["discount_num"])
        if r["b0"]: return (1, -r["discount_num"])
        return (2, 0)

    unheld_rows.sort(key=sort_key)

    return held_rows, unheld_rows


def render_markdown(held_rows, unheld_rows, data, today_str):
    lines = []
    meta = data["_meta"]
    total = meta["total_stocks"]
    val_count = sum(1 for r in held_rows + unheld_rows if r["discount"] != "-")

    b1_count = sum(1 for r in held_rows + unheld_rows if r["b1"])
    b0_count = sum(1 for r in held_rows + unheld_rows if r["b0"])
    t0_count = sum(1 for r in held_rows + unheld_rows if r["t0"])
    sell_count = sum(1 for r in held_rows if "🔴S" in r["signal_str"])
    tagged_count = sum(1 for r in held_rows if r["status_icon"] == "🏷️")
    batch_count = sum(1 for r in held_rows if r["batch_rows"])

    lines.append(f"# 每日信号报告 | {today_str}")
    lines.append("")
    lines.append(f"> 核心池 {total} 只 | 估值覆盖 {val_count} | 🔵B1 {b1_count} | 🟢B0 {b0_count} | 🔴卖出 {sell_count} | 🏷️抑制 {tagged_count} | ⚠️T0 {t0_count}")
    lines.append(f"> 图例：🔵买点 🟢形态 🔴卖出 🟠批次 ⚠️T0冲突 🏷️标记 ⚪正常")
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("## 总览")
    lines.append("")

    # --- 持仓标的表 ---
    lines.append("### 持仓标的（{}只）".format(len(held_rows)))
    lines.append("")
    header = "| 状态 | 说明 | 标的 | 仓位 | 现价 | 保守~乐观 | 基准 | 折扣率 | 价格位置 | 信号 |"
    sep =    "|------|------|------|------|------|----------|------|--------|----------|------|"
    lines.append(header)
    lines.append(sep)

    for r in held_rows:
        line = f"| {r['status_icon']} | {r['status_desc']} | **{r['name']}** {r['code']} | {r['hold']} | {r['close']} | {r['val_range']} | {r['base']} | {r['discount']} | {r['pos_str']} | {r['signal_str']} |"
        lines.append(line)
        # batch sub-row — 分散到各列
        for br in r.get("batch_rows", []):
            lines.append(f"| | | {br['id']} | {br['logic']} | {br['cost']} | {br['pnl']} | {br['shares']} | | {br['stop']} | |")

    lines.append("")

    # --- 量化精选池表 ---
    lines.append("### 量化精选池（未持仓）（{}只）".format(len(unheld_rows)))
    lines.append("")
    lines.append(header)
    lines.append(sep)

    for r in unheld_rows:
        line = f"| {r['status_icon']} | {r['status_desc']} | **{r['name']}** {r['code']} | {r['hold']} | {r['close']} | {r['val_range']} | {r['base']} | {r['discount']} | {r['pos_str']} | {r['signal_str']} |"
        lines.append(line)

    lines.append("")
    lines.append(f"| | | **合计 {total} 只** | | | | | | | |")
    lines.append("")
    lines.append("---")
    lines.append("")

    # --- B1 聚焦表 ---
    b1_rows = [r for r in unheld_rows if r["b1"]]
    if b1_rows:
        lines.append("## 🔵 B1-估值买点（{}只，估值+形态双确认）".format(len(b1_rows)))
        lines.append("")
        lines.append("| 排名 | 标的 | 现价 | 估值区间 | 折扣率 | 缩量 | 均线 | T0 | 建议 |")
        lines.append("|------|------|------|----------|--------|------|------|----|------|")

        for i, r in enumerate(b1_rows):
            code = r["code"]
            # Get detailed data from JSON
            detail = None
            for s in data["stocks"]:
                if s["_meta"]["code"] == code:
                    detail = s
                    break
            shrink = "-"
            ma_info = "-"
            if detail:
                ind = detail.get("indicators", {})
                shrink = fmt(ind.get("shrink"), "-")
                d20 = ind.get("dist_ma20")
                d60 = ind.get("dist_ma60")
                if d20 is not None and abs(d20) <= 5:
                    ma_info = f"MA20({d20:+.1f}%)"
                elif d60 is not None and abs(d60) <= 5:
                    ma_info = f"MA60({d60:+.1f}%)"
                else:
                    ma_info = f"MA20({d20:+.1f}%)" if d20 else "-"

            t0_flag = "⚠️" if r["t0"] else "—"
            disc = r["discount"]
            disc_str = f"**{disc}**" if disc != "-" and float(disc) > 1.1 else disc

            if r["t0"]:
                sug = "⚠️等企稳（跌破买入支撑，不要急于抄底）"
            elif disc != "-" and float(disc) > 0.8:
                sug = "建议建仓"
            elif disc != "-" and float(disc) > 0.65:
                sug = "可轻仓（安全边际适中）"
            elif disc != "-" and float(disc) > 0.5:
                sug = "等回调"
            else:
                sug = "观望"

            lines.append(f"| {i+1} | **{r['name']}** {code} | {r['close']} | {r['val_range']} | {disc_str} | {shrink} | {ma_info} | {t0_flag} | {sug} |")

        lines.append("")

    # --- B0 形态到位（有估值但价高） ---
    b0_overvalued = [r for r in unheld_rows if r["b0"] and r["discount"] != "-" and not r["b1"]]
    if b0_overvalued:
        lines.append("## 🟢 B0-形态到位（有估值但价格过高，{}只）".format(len(b0_overvalued)))
        lines.append("")
        lines.append("| 标的 | 现价 | 估值区间 | 折扣率 | 原因 |")
        lines.append("|------|------|----------|--------|------|")
        for r in b0_overvalued:
            pos = r["pos_str"]
            if pos == "远超乐观": reason = "远超乐观估值"
            elif pos == "深度低估": reason = "低于悲观估值，需复核估值假设"
            elif pos == "悲观~基准": reason = "缩量临界或MA偏离"
            else: reason = "高于基准"
            lines.append(f"| **{r['name']}** {r['code']} | {r['close']} | {r['val_range']} | {r['discount']} | {reason} |")
        lines.append("")

    # --- 卖出信号 ---
    sell_stocks = [r for r in held_rows if "🔴S" in r["signal_str"]]
    if sell_stocks:
        lines.append("## 🔴 卖出信号")
        lines.append("")
        for r in sell_stocks:
            code = r["code"]
            detail = None
            for s in data["stocks"]:
                if s["_meta"]["code"] == code:
                    detail = s
                    break
            if not detail:
                continue

            cost = detail["_meta"].get("cost_price", "-")
            close = r["close"]
            hold = r["hold"]

            lines.append(f"### {r['name']} {code} | {hold} | 成本 {cost} | 现价 {close}")
            lines.append("")

            for sig in detail["sell_signals"]:
                if not sig["hit"]:
                    continue
                cond = sig.get("conditions", {})
                lines.append(f"- **{sig['signal']}** | {sig.get('type','')} | 优先级：{sig.get('priority','')}")
                lines.append(f"  - {json.dumps(cond, ensure_ascii=False)}")
                lines.append(f"  - 建议：{sig.get('suggestion','')}")
                lines.append("")

            # batch signals — 始终展示每个批次的止损线和信号状态
            for bs in detail.get("batch_signals", []):
                b_cost = bs.get("cost_price", 0)
                b_shares = bs.get("shares", 0)
                b_pnl = bs.get("pnl_pct")
                pnl_str = f"{b_pnl:+.1f}%" if b_pnl is not None else "-"
                lines.append(f"**批次 {bs['batch_id']}** | {bs.get('entry_logic','')} | {b_shares}股 | 成本 {b_cost} | 浮盈 {pnl_str}")
                lines.append("")

                # 收集止损信息
                triggered = []
                for bsig in bs.get("signals", []):
                    cond = bsig.get("conditions", {})
                    if "硬止损" in bsig["signal"]:
                        tp = cond.get("trigger_price", "-")
                        lines.append(f"- S0硬止损触发价: **{tp}**（成本 {b_cost} × {cond.get('stop_pct','-')}%）— {'⚠️已触发' if bsig['hit'] else '未触发'}")
                    elif "支撑止损" in bsig["signal"]:
                        support = cond.get("support", "-")
                        tp = cond.get("trigger_price", "-")
                        lines.append(f"- S0支撑止损: 支撑位 {cond.get('support_source','')}({support})，触发价 **{tp}** — {'⚠️已触发' if bsig['hit'] else '未触发'}")
                    if bsig["hit"]:
                        triggered.append(f"{bsig['signal']}: {bsig.get('suggestion','')}")

                if triggered:
                    for t in triggered:
                        lines.append(f"- 🟠 {t}")
                lines.append("")
            lines.append("---")
            lines.append("")

    # --- 预警 ---
    warned = [r for r in held_rows if "🟡" in r["signal_str"]]
    if warned:
        lines.append("## 🟡 前瞻预警")
        lines.append("")
        for r in warned:
            lines.append(f"- **{r['name']}** {r['code']}：{r['status_desc']} | {r['hold']} | 现价 {r['close']}")
        lines.append("")

    # --- 手动标记 ---
    tagged = [r for r in held_rows if r["status_icon"] == "🏷️" and r["name"] not in [sr["name"] for sr in warned]]
    # Actually just list all tagged
    all_tagged = [r for r in held_rows if r["status_icon"] == "🏷️"]
    if all_tagged:
        lines.append("## 🏷️ 手动标记（{}只，卖出信号已抑制）".format(len(all_tagged)))
        lines.append("")
        for r in all_tagged:
            code = r["code"]
            detail = None
            for s in data["stocks"]:
                if s["_meta"]["code"] == code:
                    detail = s
                    break
            if not detail:
                lines.append(f"- **{r['name']}** {code} | {r['status_desc']} | {r['hold']} | 现价 {r['close']}")
                continue

            suppressed = []
            for sig in detail["sell_signals"]:
                if sig.get("overridden"):
                    suppressed.append(f"~{sig['signal']}~")
            sup_str = "、".join(suppressed) if suppressed else "无"
            lines.append(f"- **{r['name']}** {code} | {r['status_desc']} | {r['hold']} | 现价 {r['close']} | {sup_str} → 已抑制")
        lines.append("")

    # Footer
    lines.append("---")
    lines.append("")
    lines.append(f"> 生成：{today_str} | 估值覆盖 {val_count}/{total} | 批次监控 {batch_count} 个活跃 | 不构成投资建议")

    return "\n".join(lines)


def main():
    datestr = sys.argv[1] if len(sys.argv) > 1 else datetime.now().strftime("%y%m%d")
    today_full = datetime.now().strftime("%Y-%m-%d")

    data, ranking, core_pool = load_data(datestr)
    held_rows, unheld_rows = build_tables(data, ranking, core_pool)
    md = render_markdown(held_rows, unheld_rows, data, today_full)

    out_path = f"{SIGNALS_DIR}/signals_{datestr}.md"
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)

    # 维护一份最新报告在项目根目录，方便快速查看
    latest_path = f"{PROJECT_ROOT}/SIGNALS.md"
    with open(latest_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(f"✅ 信号报告: {out_path}  → {latest_path}")
    print(f"   持仓 {len(held_rows)} 只 | 未持仓 {len(unheld_rows)} 只")
    b1 = sum(1 for r in held_rows + unheld_rows if r["b1"])
    t0 = sum(1 for r in held_rows + unheld_rows if r["t0"])
    print(f"   B1 {b1} 只 | T0预警 {t0} 只")


if __name__ == "__main__":
    main()
