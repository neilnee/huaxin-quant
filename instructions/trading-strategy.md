# Huaxin Quant 交易策略文档

最近更新：2026-07-10（v1.7）

本文记录当前交易策略口径，作为后续持续迭代的基础。内容只描述策略、指标和评分规则，不描述工程执行流程。

---

## 一、总体策略

Huaxin Quant 采用两层发现逻辑：

```text
模型一：基本面海选
→ 先排除明显不符合质量底线的公司，形成候选池

模型二：VCP 结构与买点
→ 在候选池中识别当前有效 VCP 结构
→ 在结构内识别 PULLBACK / BREAKOUT / RETEST 三类买点
→ 根据结构、动作和风险给出买点等级
```

核心原则：

```text
基本面决定是否值得观察
结构决定买点上限
动作决定买点是否出现
风险决定买点是否降级
```

---

## 二、模型一：基本面海选策略

### 2.1 目标

模型一从全市场 A 股中筛选基本面候选池。它只判断公司是否达到基本面硬底线，不判断价格、形态、估值和买点。

### 2.2 API 侧初筛

API 查询时先要求以下条件同时满足：

| 条件 | 阈值 |
|------|------|
| 营收同比增长率 | > 15% |
| 归母净利润同比增长率 | > 20% |
| 毛利率 | > 15% |
| 研发费用占营收比例 | > 2.5% |
| ST 状态 | 排除 ST |
| A 股 | 仅 A 股 |

市值分段拉取：

| 分段 | 条件 |
|------|------|
| 50-100 亿 | 总市值 > 50 亿且 < 100 亿 |
| 100-200 亿 | 总市值 > 100 亿且 < 200 亿 |
| 200-500 亿 | 总市值 > 200 亿且 < 500 亿 |
| 500 亿以上 | 总市值 > 500 亿 |

50 亿以下标的不进入候选池。

### 2.3 自算硬过滤

API 初筛后，模型一继续执行硬过滤。

| 条件 | 规则 |
|------|------|
| 总市值 | >= 50 亿 |
| 上市时长 | >= 365 天；上市日期缺失时保留 |
| 经营现金流 / 归母净利润 | 年报口径 > 0.50；季度口径使用周期感知阈值 |
| 归母净利润 | 年报 >= 5000 万；季度口径使用周期感知阈值 |
| 资产负债率 | < 70% |
| 毛利率 | 非半导体 >= 20%；半导体高增长 >= 15% |

季度口径阈值：

| 数据周期 | 净利润门槛 | OCF/NP 门槛 |
|----------|------------|-------------|
| Q1 | 500 万 | > 0 |
| H1 | 2000 万 | > 0.10 |
| Q3 | 3500 万 | > 0.30 |
| annual | 5000 万 | > 0.50 |
| unknown | 5000 万 | > 0.50 |

半导体高增长豁免：

```text
is_semi = 行业字段包含“半导体”
rev_growth > 25%

若满足：
  OCF/NP 硬过滤豁免
  毛利率底线使用 15%
```

### 2.4 行业排除

模型一使用行业黑名单做减法，不使用行业白名单。

排除关键词：

```text
银行、保险、证券、多元金融、
白酒、食品饮料、服装家纺、家用电器、旅游零售、农林牧渔、商业物业经营、
房地产开发、房地产服务、水泥、建筑装饰、
煤炭、钢铁、石油石化、航运港口、航空机场、
燃气、水务、环保、
医药生物
```

匹配口径：

```text
优先使用 SW_INDUSTRY 三级行业路径
行业字段包含任一排除关键词即剔除
未命中排除关键词则保留
```

### 2.5 软标签与质量评分

通过硬过滤的股票不再因软标签剔除。软标签只用于排序和人工复核。

质量评分：

```text
base_quality_score = 100
quality_score = max(0, 100 - 标签扣分合计)
```

| 标签 | 触发条件 | 扣分 |
|------|----------|------|
| LOW_ROE | ROE < 5% | 15 |
| WEAK_ROE | 5% <= ROE < 10% | 8 |
| WEAK_CASHFLOW | OCF/NP < 0.5 | 15 |
| HIGH_DEBT_EDGE | 60% <= 资产负债率 < 70% | 10 |
| HIGH_VALUATION | PE_TTM > 100 或 PB > 10 | 8 |
| NEGATIVE_PE_TTM | PE_TTM <= 0 | 12 |
| LOW_BASE_REBOUND | 净利增速 > 100% 且 ROE < 5%，或净利润低于年报基准 | 12 |
| MID_SMALL_CAP_50_100 | 50 亿 <= 总市值 < 100 亿 | 5 |
| SEMI_CASHFLOW_RELAX | 半导体现金流豁免生效 | 5 |
| DATA_PERIOD_QUARTERLY | NP/OCF 使用 Q1/H1/Q3 数据 | 5 |
| MISSING_KEY_DATA | 关键字段缺失 | 8 |

---

## 三、模型二：VCP 结构策略

### 3.1 目标

模型二只判断量价结构和买点，不判断公司估值。

它寻找的是：

```text
基本面候选池内
+ 曾经有资金参与
+ 当前出现波动收敛
+ 卖压逐步变轻
+ 价格没有明显失控
+ 后续有清晰买点和失效位
```

### 3.2 基础趋势条件

模型二识别 VCP 前，先要求趋势背景不能明显破坏：

| 条件 | 规则 |
|------|------|
| 有效交易日 | >= 80 |
| 趋势基础 | close > MA60，或 MA20 >= MA60 |
| MA60 斜率 | >= -0.03%/日 |
| 近 120 日最大回撤 | 不超过 -35% |

MA120 趋势背景：

```text
close < MA120 → 触发 BELOW_MA120 风险标记

若收缩低点停止下移：
  结构降级，不直接拒绝

若收缩低点持续下移：
  趋势背景不成立，硬拒绝
```

### 3.3 收缩轮次定义

一轮 contraction 定义为：

```text
从收盘价局部高点回撤到后续收盘价局部低点
回撤幅度 >= 4%
持续时间 3-45 个交易日
低点后有一定修复，不能是单边下跌未止
```

收缩的转折定位、回撤幅度和修复均使用收盘价 Swing，且必须 `end_close < start_close`。日内 high/low 不参与收缩轮次或递减判定，只用于 Pivot、失效位和影线风险审计；避免长影线把并非收盘回撤的波动误判为 VCP 收缩。

每轮 contraction 记录：

```text
start_date / end_date
start_close / end_close / close_pullback_pct
intraday_high / intraday_low / intraday_pullback_pct
pullback_pct（兼容字段，等同于 close_pullback_pct）
duration_days
avg_volume
recovery_pct
```

### 3.4 收缩递减

明显递减：

```text
abs(C2.pullback) <= abs(C1.pullback) * 0.90
abs(C3.pullback) <= abs(C2.pullback) * 0.90
```

接近递减：

```text
abs(Cn.pullback) <= abs(Cn-1.pullback) * 1.05
```

明显扩张重置：

```text
abs(Cn.pullback) > abs(Cn-1.pullback) * 1.50
且 abs(Cn.pullback) - abs(Cn-1.pullback) >= 5pct
```

含义：

```text
小幅扩张只降低结构质量
大幅扩张说明旧收敛结构被破坏，后一轮应视为新结构起点
```

### 3.5 当前有效结构组

模型二只识别当前正在形成的 VCP，不追认已经走完或已经大幅突破的历史结构。

当前结构组选择优先级：

```text
收缩幅度递减或接近递减
量能逐轮下降或近期 drying
当前价格接近 structure_pivot
最后一轮收缩距离当前更近
早期噪声回调不得污染主收缩序列
```

当前有效性：

| 条件 | 阈值 |
|------|------|
| structure_age_days | <= 45 |
| pivot_distance | >= -18% |
| market_pivot | <= structure_pivot * 1.10 |
| post_structure_gain | <= 25% |
| post_structure_drawdown | >= -18% |

时间连续性约束：

```text
相邻 contraction 间隔 > 25 个交易日 → 切分为新 cluster
当前 VCP 只能从最新 cluster 选取
当前 contraction_group 首尾跨度 <= 60 个交易日
```

旧 cluster 保留在 `contractions` 供审计和趋势背景参考，但不得计入当前 `contraction_group`、收缩递减、结构阶段或买点评分。这样不会将数月前的旧基底与当期整理机械拼接成 MATURE VCP。

失效状态：

| 原因 | 含义 |
|------|------|
| structure_too_old | 最后一轮收缩距当前太久 |
| far_below_structure_pivot | 当前价距离结构 pivot 过远 |
| post_structure_extended | 结构后涨幅过大，旧 VCP 已完成 |
| post_structure_drawdown | 结构后再度深回撤，需要重新形成 |

### 3.6 量能状态

| volume_pattern | 定义 |
|----------------|------|
| decreasing | contraction 均量逐轮下降 |
| drying | 当前量能明显缩量，或中期量能低于长期量能 |
| mixed | 量能不稳定 |
| failed | 最近收缩量能明显放大，收缩质量差 |

量能判断指标：

```text
C2.avg_volume < C1.avg_volume * 0.95
C3.avg_volume < C2.avg_volume * 0.95
volume_dry_up < 0.85
vol_ma20 < vol_ma60
```

结构量能与 RETEST 的联动：

| structure volume_pattern | RETEST 处理 |
|---|---|
| decreasing / drying | 原有 RETEST 评分不变 |
| mixed | 允许 RETEST，但最高质量为 B |
| failed | 仅当其他 RETEST 回踩条件已满足时，禁止 RETEST，输出 WAIT_REBUILD |

该联动只约束 RETEST；PULLBACK 和 BREAKOUT 的触发逻辑不变。

### 3.7 结构阶段

| structure_stage | 定义 | 交易含义 |
|-----------------|------|----------|
| VCP_EARLY | 1 轮有效收缩 | 只观察 |
| VCP_FORMING | 至少 2 轮收缩，后一轮小于或接近前一轮 | 可触发试错型买点 |
| VCP_MATURE | 至少 3 轮收缩，幅度明显递减 | 标准 VCP 结构 |
| VCP_TIGHT | VCP_MATURE + 最后一轮 <= 10% + pivot_distance >= -8% + 量能健康 | 稀缺临界结构 |
| TREND_WATCH | 趋势强但未形成有效收缩轮次 | 趋势观察，不归入 VCP |
| POST_BREAKOUT | 历史 VCP 已明显突破并延伸 | 不追高 |
| TREND_REBUILD | 旧结构突破后深回撤或失效 | 等待重建 |

### 3.8 结构分

结构分只评价 VCP 结构质量，不直接等同买点质量。

```text
structure_score = stage_score
                + volume_score
                + trend_score
                + position_score
```

stage_score：

| 阶段 | 分数 |
|------|------|
| VCP_EARLY | 18 |
| VCP_FORMING | 32 |
| VCP_MATURE | 45 |
| VCP_TIGHT | 55 |
| TREND_WATCH | 12 |
| POST_BREAKOUT | 8 |
| TREND_REBUILD | 8 |

volume_score：

| 条件 | 分数 |
|------|------|
| volume_pattern = decreasing | +15 |
| volume_pattern = drying | +8 |
| volume_pattern = failed | -10 |
| volume_dry_up < 0.8 | +10 |
| vol_ma20 < vol_ma60 | +5 |

trend_score：

| 条件 | 分数 |
|------|------|
| MA20 >= MA60 | +7 |
| MA20_slope >= 0 | +4 |
| MA60_slope >= -0.03 | +4 |

position_score：

| 条件 | 分数 |
|------|------|
| distance_ma20 在 [-4%, +3%] | +10 |
| distance_ma20 <= 10% | +5 |
| pivot_distance 在 [-8%, 0%] | +12 |
| pivot_distance 在 [-15%, 0%] | +6 |

---

## 四、模型二：买点策略

### 4.1 买点类型

模型二定义三类买点：

| 买点 | 交易含义 | 确认度 |
|------|----------|--------|
| PULLBACK_BUY | 结构内缩量回踩低吸 | 低 |
| BREAKOUT_BUY | 枢轴突破参与 | 中 |
| RETEST_BUY | 突破后回踩确认 | 高 |

买点必须建立在有效结构之上：

```text
structure_stage ∈ VCP_FORMING / VCP_MATURE / VCP_TIGHT
VCP_EARLY 只观察，不触发买点
```

### 4.2 PULLBACK_BUY 触发

前提：

```text
structure_stage ∈ VCP_FORMING / VCP_MATURE / VCP_TIGHT
无 DOWNTREND / DEEP_FALL
```

硬触发：

```text
价格靠近 MA20 或 MA60
close > 最近一轮 contraction low * 1.02
MA20_slope >= -0.03
无 LONG_UPPER_SHADOW
最终 setup_score >= 买点触发阈值
```

交易含义：

```text
突破尚未确认
优点是价格低、止损近、盈亏比好
缺点是失败概率高
```

### 4.3 BREAKOUT_BUY 触发

前提：

```text
structure_valid = true
structure_stage ∈ VCP_FORMING / VCP_MATURE / VCP_TIGHT
无 DOWNTREND / DEEP_FALL
```

硬触发：

```text
close > structure_pivot * 1.01
close <= structure_pivot * 1.08
当日成交量 > vol_ma20 或 当日成交量 > vol_ma5
不是长上影
无 LONG_UPPER_SHADOW / VOLUME_STALL
最终 setup_score >= 买点触发阈值
```

最终确认日还必须通过 `FAILED_RETEST_SELLING` 检查。该规则只针对已具备其他 RETEST 回踩条件的标的，以下条件同时成立即硬阻断 RETEST：

```text
close < open
当日跌幅 <= -max(板块基础阈值, min(ATR14_pct × 2, 涨跌停幅度 × 75%))
当日量 >= 前 5 日均量 × 1.20（前 5 日不含当天）
且当日量 >= 突破日量 × 0.80，或 >= 突破后前 5 日均量 × 1.35
```

板块基础阈值：主板 7%、创业板/科创板 10%、北交所 15%。命中时写入 `setup_risk_flags = FAILED_RETEST_SELLING`，只否决 RETEST_BUY，不改变 VCP 结构、PULLBACK_BUY 或 BREAKOUT_BUY。

交易含义：

```text
方向开始确认
能避免强势股突破后不回踩导致完全踏空
但存在假突破和追高风险
```

### 4.4 RETEST_BUY 触发

前提：

```text
structure_valid = true
structure_stage ∈ VCP_FORMING / VCP_MATURE / VCP_TIGHT
无 DOWNTREND / DEEP_FALL
最近 12 日内曾经有效突破
```

有效突破定义：

```text
突破日 close > 最近 60 日高点 * 1.01
突破日 volume > vol_ma20
突破日不是长上影
```

回踩硬触发：

```text
突破后 1-15 个交易日内
回踩低点 >= breakout_level * 0.97
回踩低点 <= breakout_level * 1.005
回踩期均量 < 突破日成交量
最新 close >= breakout_level 或 close >= MA10
确认日不是长上影
无 LONG_UPPER_SHADOW / VOLUME_STALL
最终 setup_score >= 买点触发阈值
```

交易含义：

```text
突破已经发生，并且回踩确认有效
确定性最高
但价格通常高于 PULLBACK_BUY，盈亏比未必最好
```

---

## 五、最终买点评分

### 5.1 评分公式

目标买点评分采用四段相加：

```text
setup_score = stage_base
            + action_type_base
            + action_quality_score
            + risk_adjust
```

分值限制：

```text
setup_score = clamp(setup_score, 0, 100)
```

其中：

```text
stage_base：结构阶段基础分
action_type_base：买点类型确认度基础分
action_quality_score：动作本身质量分
risk_adjust：风险标识修正分
```

### 5.2 结构阶段基础分

| 结构阶段 | 分数 | 含义 |
|----------|------|------|
| VCP_FORMING | 30 | 结构刚成立，可试错 |
| VCP_MATURE | 50 | 标准 VCP 已完成 |
| VCP_TIGHT | 60 | 成熟且压紧，稀缺形态 |
| VCP_EARLY | 不触发买点 | 只观察 |

### 5.3 买点类型基础分

| 买点 | 分数 | 含义 |
|------|------|------|
| PULLBACK_BUY | 6 | 结构内低吸，确认度最低 |
| BREAKOUT_BUY | 10 | 突破参与，确认度居中 |
| RETEST_BUY | 15 | 突破后回踩确认，确认度最高 |

### 5.4 动作质量分

动作质量分总分 0-15。三类买点使用不同指标。

#### PULLBACK_BUY 动作质量

| 维度 | 分数 |
|------|------|
| 位置质量 | distance_ma20 在 [0%, +2%] 得 5；[-3%, 0%) 或 (+2%, +3%] 得 2；只靠近 MA60 得 2；其他 0 |
| 量能质量 | volume_dry_up < 0.80 得 5；收缩段低量确认得 4；volume_dry_up < 0.90 得 2；其他 0 |
| 确认质量 | close > last_low * 1.05 且 MA20_slope >= 0 得 5；close > last_low * 1.02 且 MA20_slope >= -0.03 得 2；其他 0 |

#### BREAKOUT_BUY 动作质量

| 维度 | 分数 |
|------|------|
| 突破幅度 | close / pivot 在 [1.02, 1.05] 得 5；[1.01, 1.02) 或 (1.05, 1.08] 得 2；其他 0 |
| 量能质量 | volume > vol_ma20 * 1.5 得 5；> 1.2 得 3；> 1.0 得 1；其他 0 |
| K 线确认 | 收盘接近日高、实体强、无明显上影得 5；站上 pivot 且无长上影得 2；其他 0 |

K 线确认参考：

```text
close_position = (close - low) / (high - low)
body_ratio = abs(close - open) / (high - low)
upper_shadow_ratio = (high - max(open, close)) / (high - low)

高质量：
close_position >= 0.75
body_ratio >= 0.45
upper_shadow_ratio <= 0.25
```

#### RETEST_BUY 动作质量

| 维度 | 分数 |
|------|------|
| 回踩位置 | pullback_low / breakout_level 在 [0.99, 1.003] 得 5；[0.97, 0.99) 或 (1.003, 1.005] 得 2；其他 0 |
| 回踩量能 | 回踩期均量 < 突破日量 * 0.70 得 5；< 0.85 得 3；< 1.0 得 1；其他 0 |
| 重新确认 | close >= breakout_level 且收盘强得 5；close >= breakout_level 或 close >= MA10 得 2；其他 0 |

在上述动作分和风险分计算后，仍应用结构量能准入：`mixed` 的 RETEST 即使分数达到 A 阈值也封顶为 B；`failed` 不产生 RETEST 买点。

收盘强参考：

```text
close_position >= 0.65
upper_shadow_ratio <= 0.30
```

### 5.5 风险修正分

风险修正使用模型二结构风险标识；RETEST 另有买点级 `setup_risk_flags`。其中 `FAILED_RETEST_SELLING` 是硬阻断而不是扣分项，因此不与结构风险混合计分。

```text
risk_adjust = clamp(sum(flag_adjustments), -20, +10)
```

| 风险标识 | 修正 |
|----------|------|
| 无风险标识 | +10 |
| EXTENDED_FROM_MA20 | -5 |
| MA20_DECLINE | -6 |
| BELOW_MA120 | -6 |
| FAR_ABOVE_MA20 | -8 |
| LONG_UPPER_SHADOW | -10 |
| VOLUME_STALL | -10 |
| OVERHEAT_CHG5 | -12 |
| OVERHEAT_CHG20 | -12 |
| DOWNTREND | -20 |
| DEEP_FALL | -20 |

多风险累计扣分，但最低不低于 -20。

### 5.6 买点等级

| 等级 | 分数 |
|------|------|
| A | >= 80 |
| B | 65-79 |
| C | 55-64 |
| D | < 55 |

等级含义：

```text
A = 成熟/紧致结构 + 强动作 + 风险干净
B = 成熟结构有轻风险，或 FORMING 中非常好的动作
C = 买点成立但质量一般，只能轻仓或观察
D = 触发条件勉强，不建议作为买点
```

### 5.7 仓位语义

仓位不是最终交易指令，只是模型二量价侧建议。

| 买点 | A | B | C |
|------|---|---|---|
| PULLBACK_BUY | 20%-30% | 10%-20% | 观察或极轻仓 |
| BREAKOUT_BUY | 40%-50% | 20%-30% | 观察或极轻仓 |
| RETEST_BUY | 60%-80% | 40%-50% | 20%-30% 或观察 |

最终是否买入、是否加仓、是否降仓，需要结合估值、持仓和模型四风险管理。
