# 模型四：择时跟踪参考手册

> 执行指令见 `04-tracker.md`。本文件为按需查阅的参考内容，**不在每次执行时加载**。
> 以下规格足以从零重建 `scripts/tracker.py` 和 `scripts/generate_report.py`。

---

## 信号条件常量速查

### 买入信号

| 信号 | 条件 | 阈值 |
|------|------|------|
| B0a-缩量回调 | 缩量程度 + 近MA | shrink<0.8, 距MA20∈[-5%,+3%] 或 距MA60∈[-5%,+3%] |
| B0b-箱体突破 | 箱体盘整 + 放量突破 | box_consolidation, box_breakout, vol_ratio>1.5 |
| B1-估值买点 | 价格区间 + 缩量 + 均线 | 悲观≤现价≤基准, shrink<0.8, 距MA∈[-5%,+3%] |
| B2-突破买点 | 价格区间 + 箱体 + 放量 | 悲观≤现价≤乐观, box_consolidation, box_breakout, vol_ratio>1.5 |

### 卖出信号

| 信号 | 优先级 | 触发条件 |
|------|--------|---------|
| S0-支撑止损 | 最高 | 现价 < 支撑位 × (1-容忍度) |
| S0-硬止损 | 最高 | 现价 < 成本价 × (1+止损比例) |
| S1-估值卖点 | 最高 | 现价 > 乐观估值 × 1.1 |
| S5-加速赶顶 | 高 | 近5日涨幅>20%, 量比>2.5 |
| S4-均线破位 | 中 | 现价<止盈均线, MA20走平或向下 |
| S2-移动止盈 | 中 | 现价<止盈均线, MA20上升, 浮盈>5% |
| S3-量价背离 | 低 | 近5日涨幅>阈值, 缩量程度<0.6 |

### 仓位分档参数

| 参数 | 轻仓 | 标准仓 | 重仓 |
|------|------|--------|------|
| 仓位定义 | ≤50%目标 | 50%-75%目标 | ≥75%目标 |
| S0 支撑止损容忍度 | 5% | 3% | 2% |
| S0 硬止损比例 | -12% | -10% | -8% |
| S2 止盈均线(默认) | MA20 | MA10 | MA5 |
| S2 止盈均线(C类) | MA10 | MA5 | MA5 |
| S4 止盈均线 | MA20 | MA10 | MA5 |
| S3 涨幅阈值(默认) | >5% | >3% | >0% |
| S3 涨幅阈值(A类) | >3% | >2% | >0% |

### S0 支撑位定义（按买入形态）

| 买入形态 | 支撑位 | 逻辑 |
|----------|--------|------|
| A 类 | entry_ma 对应的均线(MA20/MA60) | A类买入逻辑就是回踩这条均线不破 |
| C 类 | MA20 | C类买的是上升趋势，MA20是趋势载体 |
| B 类/空值 | 近20日最低价 | 默认逻辑 |
| 估值兜底 | 支撑位不低于 悲观估值×0.8 | 所有形态统一 |

### ETF 简化信号

仅适用：S0-硬止损、S4-均线破位(仅MA20)、S2-移动止盈(仅MA20)、W1/W2。其余信号全部跳过。

### 买入建议分档（B1）

| 折扣率 | 建议 |
|--------|------|
| > 0.8 | 建议建仓 |
| 0.65-0.8 | 可轻仓 |
| 0.5-0.65 | 等回调 |
| < 0.5 | 观望 |

### 预警阈值

| 预警 | 条件 |
|------|------|
| W1-价格预警 | 距S0支撑<2%, 或 近5日涨幅>15%+量比>2.0 |
| W2-量价预警 | 缩量程度<0.5, 或 近3日量比均值<0.6 |
| W3-催化预警 | 催化事件在未来2天内（需 valuation_ranking.csv 含催化字段） |

---

## 技术指标计算公式

### 均线系统
```
MA5 = close.rolling(5).mean()
MA10 = close.rolling(10).mean()
MA20 = close.rolling(20).mean()
MA60 = close.rolling(60).mean()
```

### 成交量指标
```
vol_ma5 = volume.rolling(5).mean()
vol_ma20 = volume.rolling(20).mean()
量比 = volume / vol_ma5.shift(1)
缩量程度 = vol_ma5 / vol_ma20
量比_3d_avg = 量比.rolling(3).mean()
```

### 价格位置指标
```
距MA20 = (close - MA20) / MA20 × 100
距MA60 = (close - MA60) / MA60 × 100
近5日涨幅 = close.pct_change(5) × 100
```

### 滚动高低点
```
high20 = high.rolling(20).max()
low20 = low.rolling(20).min()
```

### 箱体指标
```
range20 = high20 - low20
avg20 = close.rolling(20).mean()
range_ratio = range20 / avg20
box_consolidation = range_ratio < 0.15
box_breakout = close >= high20 × 0.98
```

### MA20 斜率（线性回归，%/天）
```
取最近5个交易日的 MA20 值，做线性回归 y = slope × x + intercept
MA20_slope = slope / y[-1] × 100
abs(slope) < 0.3%/天 → 走平
slope < -0.2%/天 → 向下
slope > 0.3%/天 → 向上
```

---

## `maintain_pool()` 函数规格

### 签名
```
maintain_pool(pool, positions, rankings, today_str, interactive=True)
  → (new_pool, changelist)
```

### 输入
- `pool`: [{code, name, first_added, last_quant_date, consecutive_miss, priority, has_valuation, status, entry_pattern, entry_ma}]
- `positions`: {code: {actual_shares, asset_type, ...}}
- `rankings`: {code: {valuation fields}} — 用于判断 has_valuation
- `today_str`: "YYMMDD"
- `interactive`: bool — 是否等待用户输入确认

### 输出
- `new_pool`: 更新后的池子列表
- `changelist`: {added: [{code, name, priority}], removed: [{code, name, consecutive_miss, has_valuation}], updated: [{code, name, change}], missing_valuation: [{code, name}], stale_quant: bool}

### 调入伪代码
```
quant_stocks = read_latest_quant()  # 读 quant/ 下最新 CSV
for qs in quant_stocks:
    if qs.code not in pool:
        priority = "优先" if qs.量比 > 2.0 else "正常"
        pool.append({code, name, first_added=today, last_quant_date=today,
                     consecutive_miss=0, priority, has_valuation=code in rankings,
                     status="跟踪中", entry_pattern=qs.通过类别, entry_ma=qs.entry_ma})
    else:
        existing.consecutive_miss = 0
        existing.last_quant_date = today
        if existing.status == "已移出":
            existing.status = "跟踪中"
```

### 调出伪代码
```
for s in pool:
    if s.code in quant_codes: continue
    if positions[s.code].actual_shares > 0: continue  # 持仓保留
    s.consecutive_miss += 1
    if s.consecutive_miss >= 5:
        if s.has_valuation == "TRUE":
            s.status = "观察中"  # 估值保护
        else:
            s.status = "已移出"
    else:
        s.status = "观察中"
```

### 边界情况
- 首次运行无 core_pool.csv → 创建空文件，全部 quant 标的视为首次调入
- quant/ 目录为空 → 仅做调出判断；距今>5天打印提醒
- ETF: has_valuation="N/A", status="ETF跟踪", 持仓永久保留
- 量比列缺失 → 默认 priority="正常"
- 交互确认 n → 不写 CSV，打印提示

---

## `check_signals()` 函数规格

### 签名
```
check_signals(code, name, df, pos_info, val_info, entry_pattern, entry_ma, batches)
  → {_meta, indicators, buy_signals, sell_signals, batch_signals, warnings, valuation_review}
```

### B1 预检伪代码
```
if has_val and close and conservative and base_val:
    b1_price_ok = conservative <= close <= base_val
    b1_shrink = shrink < 0.8
    b1_near_ma = (-5 <= dist_ma20 <= 3) or (-5 <= dist_ma60 <= 3)
    b1_hit = b1_price_ok and b1_shrink and b1_near_ma
```

### B2 预检伪代码
```
if has_val and close and conservative and optimistic:
    b2_price_ok = conservative <= close <= optimistic
    b2_box = box_consolidation and box_breakout
    b2_vol = vol_ratio > 1.5
    b2_hit = b2_price_ok and b2_box and b2_vol
```

### B0 预检伪代码
```
if not is_etf and not has_position:
    B0a: shrink < 0.8 and near_ma
    B0b: box_consolidation and box_breakout and vol_ratio > 1.5
```

### S0 支撑止损伪代码
```
support = low20  # 默认
if entry_pattern == "A" and entry_ma:
    support = MA20 or MA60  (whichever matches entry_ma)
elif entry_pattern == "C":
    support = MA20
if downside: support = max(support, downside)  # 估值兜底
tolerance = {轻仓:0.05, 标准仓:0.03, 重仓:0.02}
trigger = close < support × (1 - tolerance)
```

### S4 均线破位伪代码
```
ma = {轻仓:MA20, 标准仓:MA10, 重仓:MA5}
ma_flat = abs(MA20_slope) < 0.3
ma_decline = MA20_slope < -0.2
trigger = close < ma and (ma_flat or ma_decline)
```

### 手动标记覆盖伪代码
```
if manual_tag and has_position:
    for each sell_signal:
        if sell_signal.hit:
            sell_signal.hit = False
            sell_signal.overridden = True
            sell_signal.override_reason = f"手动标记「{manual_tag}」，卖出信号已抑制"
```

### 批次卖出信号（每批次独立检查）
- S0-批次支撑止损：支撑位按 batch.entry_logic/entry_ma 确定
- S0-批次硬止损：用 batch.cost_price
- S2-批次移动止盈：用 batch.cost_price，close > cost × 1.05
- 不重复检查: S3/S4/S5（股票级别量价信号）

---

## CSV 文件规格

### `core_pool.csv`
```
code,name,first_added,last_quant_date,consecutive_miss,priority,has_valuation,status,entry_pattern,entry_ma
```
- code: `="300442"` 格式
- priority: `正常` / `优先`
- has_valuation: `TRUE` / `FALSE` / `N/A`
- status: `跟踪中` / `观察中` / `已移出` / `ETF跟踪`
- entry_pattern: `A` / `B` / `C` / 空
- entry_ma: `MA20` / `MA60` / 空

### `positions.csv`
```
code,name,asset_type,target_type,target_value,actual_shares,cost_price,stop_loss_override,manual_tag
```
- asset_type: `个股` / `ETF`
- target_type: `shares` / `amount`
- stop_loss_override: 空=默认, 否则如 `-0.15`
- manual_tag: `长期持仓` / `磨底持有` / 空

### `batches.csv`
```
batch_id,code,name,entry_date,entry_logic,entry_ma,shares,cost_price,stop_loss_override
```
- batch_id: `{code}-{seq}` 如 `300442-1`
- entry_logic: `B1` / `B2` / `手动`

### `trade_log.csv`
```
trade_id,code,name,entry_date,entry_logic,shares,entry_price,exit_date,exit_price,exit_reason,pnl_pct
```
- 批次买入→卖出闭环后从 batches.csv 删除，写一条到此
- 只增不删，不参与信号计算

### `reports/indexes/valuation_ranking.csv`（模型三输出 → 模型四读取）
```
股票代码,股票名称,当前股价_元,下行风险价_元,悲观估值_元,基准估值_元,乐观估值_元,安全边际折扣率,隐含PE_基准_倍,主估值方法,报告日期
```
- tracker.py 读取字段: 下行风险价_元, 悲观估值_元, 基准估值_元, 乐观估值_元

---

## `generate_report.py` 规格

### B0/B1/T0 独立计算

```python
# B1: 悲观估值 ≤ 现价 ≤ 基准估值, shrink < 0.8, near_ma
conserv = float(ranking["悲观估值_元"])
base = float(ranking["基准估值_元"])
price_ok = conserv <= close <= base
shrink_ok = shrink < 0.8
near_ma = (-5 <= dist_ma20 <= 3) or (-5 <= dist_ma60 <= 3)
b1 = price_ok and shrink_ok and near_ma

# B0: shrink + near_ma, but not B1
b0 = shrink_ok and near_ma and not b1

# T0: B1 triggered + entry_pattern check
if b1 and entry_pattern == "A" and entry_ma == "MA60":
    t0 = close < MA60
elif b1 and entry_pattern == "A" and entry_ma == "MA20":
    t0 = close < MA20
elif b1 and entry_pattern == "C":
    t0 = MA20_slope < -0.2
```

### 报告渲染规则
- 持仓/未持仓分表，按 B1→B0→无信号排序
- B1 聚焦表：排名、现价、估值区间、折扣率、缩量、均线、T0、建议
- B0 清单：有估值(✗贵/⚡低价/形态到位) / 缺估值 分类
- 卖出信号详情：含触发条件和操作建议
- 批次止损线独立展示
- 手动标记抑制信号用 ~~划线~~ 展示

### B1 建议分档
```
if T0: "⚠️等企稳"
elif discount > 0.8: "建议建仓"
elif discount > 0.65: "可轻仓"
elif discount > 0.5: "等回调"
else: "观望"
```
