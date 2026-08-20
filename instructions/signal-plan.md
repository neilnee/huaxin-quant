# Signal Plan: 次日信号计划层指令卡

- **版本管理**: 由 Git 分支与提交历史管理
- **最近更新**: 2026-08-20（model4_signal_plan_v7）
- **所属模型**: 模型四 Tracker
- **策略配置**: `strategies/04-signal-plan.json`
- **核心目标**: 基于模型二已经识别出的有效 VCP 结构和当日买点事实，生成下一交易日可执行的量价触发计划，提前标出 A/B 类买点所需的收盘价区间、成交量区间和失效位。

---

## 一、职责边界

Signal Plan 只做次日量价计划，不重新识别 VCP，不替代模型二买点判定，不输出最终交易动作。

Signal Plan 负责：

- 消费模型二结构化 JSON。
- 消费模型二 `setup_plan_inputs` 中的结构事实；RETEST 次日执行价区由 Signal Plan 按 NEW / FOLLOW 配置计算，不把模型二的结构回踩容忍下沿直接当作次日买入下沿。
- 选择允许形成模型二买点的有效 VCP，或当日已触发买点的标的。
- 计算下一交易日可能触发的买点计划。
- 输出普通买点触发区、A 类买点量价区、最高潜在等级和失效价。
- 按 PULLBACK / BREAKOUT / RETEST 三类买点统计和展示，区分首次触发计划和已触发后的延续计划。
- 记录成熟结构被排除的原因，例如价格已超过突破计划上沿。
- 为下一有效交易日保留可复用的量价阈值；回测层使用统一兑现判定评价计划命中情况。

Signal Plan 不负责：

- 重新计算 `structure_stage` / `setup_signal`。
- 重新定义买点硬条件或替代模型二评分。
- 判断估值安全边际。
- 读取或修改持仓账本。
- 给出最终买入、卖出或仓位建议。
- 盘中实时跟踪；本模块按收盘后数据生成下一交易日计划。

### 前一交易日 Plan 命中审计

前一有效交易日 `D` 发布的 Plan 只允许在 `D+1` 验收。回测层必须复用同一套价格、量能、失效位、结构轮次和生命周期判定，分别记录 A 类条件与常规条件是否命中，用于评价 Plan 的预测效果。

当日买点事实只能来自模型二 `setup_signal`。Plan 命中不得补造当日信号、不得改变 VCP 列表资格、不得覆盖 Bloom 状态，也不得参与次日 Plan、估值候选、自选同步、仓位或 Bloom 冷却计数。

当模型二当日买点与前日 Plan 的股票和买点类型一致时，Dashboard 只附加“前日计划命中”审计标签，并可标记来源为 `MODEL2_AND_PLAN`；没有 Plan 不影响当日买点成立。Plan 的 `A / REGULAR` 分层仅保留在回测数据中，不作为 Dashboard 的买点等级或评分展示。

### Dashboard 市场环境提示

信号发现页仅在信号详情中展示同一交易日的市场环境上下文，不增加列表列，也不改变 Signal Plan、模型二、Bloom、自选或交易状态。详情标题区域增加市场状态提醒标签，并在量价与评分信息之前展示一段固定规则建议：

| `market_state.confirmed_state` | 提醒标签 | 展示建议 |
|---|---|---|
| `OFFENSIVE` | 广泛参与 | 趋势与广度支持更广泛的板块机会，但仍须等待个股量价条件成立并遵守失效位。 |
| `SELECTIVE` | 聚焦强势 | 市场机会偏结构化，只对转强和主线板块计算条件仓位，避免只凭 VCP 形态执行。 |
| `RECOVERY_WATCH` | 只选主线 | 市场处于修复期，只有主线板块保留低仓条件预案，其余板块继续观察。 |
| `CONSOLIDATING` | 暂停参与 | 市场方向尚未明确，所有板块条件仓位归零，VCP 只用于建立观察顺序。 |
| `DEFENSIVE` | 防守观望 | 市场处于弱势环境，所有板块条件仓位归零，等待波动、广度和趋势修复。 |

页面适配器只能读取 `market/data/market_context_<YYMMDD>.json` 的同日数据，并以 `market_state.confirmed_state` 作为仓位计算状态；尚在多日确认中的 `candidate_state` 只作潜在变化提示，不得提前改变仓位。不得回退到其他日期。文件缺失、状态缺失或状态未知时，必须显示“市场状态待确认 / 环境待确认”，并提示执行前先核对市场环境，不得静默省略。

### Dashboard 来源与财务提示

信号发现页的详情标题区域同时展示同日模型一来源和基础财务提示，不增加信号列表列，不改变模型一、模型二、Bloom、Signal Plan、回测或自选结果，也不调用 LLM。

- 来源只读取同日 `pool/pool_<YYMMDD>.csv` 的 `pool_channel`：`CORE_QUALITY` 显示“核心质量池”，`EXPANSION_RS` 显示“RS扩展池”，`BOTH` 显示“双通道”。
- `CORE_VERIFIED` 标的按模型一已有字段生成确定性提示，包括净利润为负、高负债、经营现金流为负、营收明显下滑、利润明显下滑、毛利率偏低，以及模型一已有的低 ROE、现金流偏弱、负债率偏高和估值偏高标签；没有风险时显示“财务硬筛通过”。
- `FUNDAMENTAL_UNVERIFIED` 标的优先读取 `cache/signal_fundamentals/<code>.json` 中同日可用的成功快照，并展示规则生成的基础风险；缓存缺失、查询失败或字段不足时分别显示“财务未查询”“财务查询失败”或“财务数据不足”，不得把这些状态解释为基本面差。
- 同日 Pool 文件或股票记录缺失时显示“来源待确认 / 财务待验证”，不得回退到其他日期，避免历史页面使用未来信息。
- 市场环境、发现来源、财务提示和技术风险分为四组换行展示；标签较多时组内自动换行，不得混成一条长标签流。
- 技术风险在 Dashboard 中必须按标准枚举去重后显示中文名称；同一枚举同时来自 `setup_risk_flags` 与 `structure_risk_flags` 时只显示一次。底层数据继续保留原始枚举供审计。
- 这些标签只做风险提醒，不参与信号评分、计划等级、候选排序或交易判断。财务补查规则见 `instructions/signal-fundamentals.md`。

### Dashboard 板块环境与仓位建议

信号发现页在不改写模型二买点和 Signal Plan 结果的前提下，生成一层确定性的环境仓位建议。该建议不调用 LLM，不读取持仓或估值，只使用同日已经存在的数据：

```text
模型二 setup_signal / setup_quality
market/data/market_context_<YYMMDD>.json
market/stock_strength_<YYMMDD>.csv
market/sector_heat_<YYMMDD>.csv
```

所属板块统一采用 `stock_strength` 的申万二级行业 `sw_l2_name`；板块状态按同名 `industry_sw_l2` 行读取。页面把板块名称、主阶段和带数字的阶段趋势合并为一个标签，固定使用“板块 · 阶段 · +N 状态”格式，整体颜色只跟随主阶段，标签文字必须纵向居中；仓位只读取 `sector_phase`，`sector_health_level/sector_health` 只作阶段内变化提醒，不参与折算。任何输入缺失都不得回退到其他日期，`BUILDING/BACKFILL`、未知市场或未知板块按 0 仓位处理。

仓位计算只允许实际买点等级 A/B；C/D 一律显示“观察”，但仍保留原始信号和回测记录。三类买点的基础仓位为：

| setup_signal | B 级 | A 级 |
|---|---:|---:|
| `PULLBACK_BUY` | 10%-20% | 20%-30% |
| `BREAKOUT_BUY` | 20%-30% | 40%-50% |
| `RETEST_BUY` | 40%-50% | 60%-80% |

市场确认阶段与板块主阶段直接决定联合环境系数，不再把板块转换成 `STRONG/NEUTRAL/BLOCKED` 中间分组：

| `market_state.confirmed_state` | 观察 `NONE` | 转强 | 主线 | 退潮 |
|---|---:|---:|---:|---:|
| `OFFENSIVE`（趋势扩散） | 50% | 80% | 100% | 0% |
| `SELECTIVE`（结构行情） | 0% | 60% | 100% | 0% |
| `RECOVERY_WATCH`（修复期） | 0% | 0% | 50% | 0% |
| `CONSOLIDATING`（弱势震荡） | 0% | 0% | 0% | 0% |
| `DEFENSIVE`（防御期） | 0% | 0% | 0% | 0% |

```text
最终建议仓位 = 买点基础仓位 × 市场确认阶段与板块主阶段联合系数
```

结果按 5% 档位保守向下取整。上界不足 5% 时显示“观察”；下界为 0、上界至少 5% 时显示“≤上界%”。页面必须同时展示买点基础仓位、环境系数和折算结果，不能只展示一个无法复盘的仓位文本。

次日 Plan 尚未形成实际 `setup_quality`，不得把 `target_quality` 当成实际买点等级。页面分别展示“若 A 级触发”和“若 B 级触发”的当日环境预案；下一交易日真正触发后，再使用触发日的市场、板块和实际买点等级重算正式建议。

仓位建议只影响 Dashboard 交易计划提示，不改变模型二 `suggested_position`、Signal Plan JSON、Bloom 生命周期、回测样本或自选同步。

模型四当前信号层结构：

```text
模型四 Tracker
├── Bloom：跨日生命周期和估值候选
├── Signal Plan：次日量价触发计划
└── Position：持仓账本和后续持仓策略
```

---

## 二、输入

Signal Plan 以模型二 JSON 为权威输入：

```text
cache/quant_runs/quant_<YYMMDD>.json
```

核心消费字段：

```text
code
name
model2_include
structure_type
structure_stage
setup_signal
action_hint
suggested_position
structure_score
structure_risk_score
structure_risk_flags
setup_score
setup_quality
setup_plan_inputs
support_price
invalid_price
breakout_level
structure_pivot
market_pivot
pivot_distance
last_contraction_low
structure_valid
structure_invalid_reason
contraction_count
contraction_pcts
contraction_days
contraction_group
volume_pattern
close
MA20
MA60
MA120
distance_ma20
volume
vol_ma5
vol_ma20
vol_ma60
vol_ratio
volume_dry_up
chg_5
chg_20
run_date
strategy_version
post_breakout_state
contraction_group
```

若模型二 JSON 缺少 `volume` / `vol_ma20` 等量能字段，Signal Plan 不得输出抽象公式作为执行计划；对应标的必须降级为 `DATA_ISSUE` 或跳过，并在 summary 中记录原因。若缺少 `setup_plan_inputs`，Signal Plan 可以使用兼容回退逻辑，但新版本模型二应提供该字段。

每条 Plan 必须保存同一轮 VCP 的 `structure_anchor`。优先取 `contraction_group` 第一段的 `start_date`；缺失时依次使用收缩段开始日、结构突破日和兼容占位。该字段只用于下一交易日事实匹配和历史去重，不改变模型二结构判断。

---

## 三、候选范围

NEW Plan 以 `VCP_MATURE`、`VCP_TIGHT` 为常规候选。`VCP_FORMING` 只有达到近成熟高质量门槛时才允许提前生成 NEW Plan，避免两轮收缩刚形成就大范围预测；`VCP_EARLY` 不进入计划。

纳入条件：

```text
model2_include = true
structure_type = VCP
structure_valid = true
且满足以下之一：
  structure_stage in {VCP_FORMING, VCP_MATURE, VCP_TIGHT}
  setup_signal in {PULLBACK_BUY, BREAKOUT_BUY, RETEST_BUY}
```

排除条件：

```text
structure_stage in {VCP_EARLY, TREND_WATCH, TREND_REBUILD, STRUCTURE_INVALID, NONE, DATA_ISSUE}
structure_valid = false
structure_risk_score >= risk_block_min_score
structure_risk_flags 命中 hard_risk_flags
缺少 close / structure_pivot / volume / vol_ma20 等必要字段
```

`VCP_FORMING` 生成 NEW Plan 必须同时满足：`structure_score ≥ 80`、`structure_risk_score ≤ 15`、`volume_pattern in {decreasing,drying}`、`pivot_distance ≥ -8%`、`post_breakout_state=PRE_BREAKOUT`。它只覆盖虽然仍为两轮收缩、但量能和位置已经接近成熟的少数结构。未达门槛的 FORMING 继续观察；当日已经触发买点时仍可按普通分支生成 FOLLOW Plan。风险硬阻断、结构有效性和生命周期门槛继续生效。

成熟结构若价格已经超过突破计划上沿，不输出追高计划，需进入 `excluded` 并在 summary 中计数。

### 突破后生命周期约束

`post_breakout_state` 是模型二对同一轮 VCP 首次有效突破后的状态判定。Signal Plan 不得把突破前的收缩结构跨越突破日重复用于新买点：

| 模型二状态 | 允许的 Signal Plan |
|---|---|
| `PRE_BREAKOUT`（或旧版数据缺失该字段） | `PULLBACK`、`BREAKOUT`，沿用常规成熟结构规则 |
| `POST_BREAKOUT_RETEST` | 仅在模型二当日确认 `RETEST_BUY` 时生成 `RETEST_FOLLOW`；未确认时只观察，不生成 `RETEST` 的 `NEW` 计划 |
| `POST_BREAKOUT_HOT` | 不生成计划，避免追高 |
| `POST_BREAKOUT_CONSOLIDATING` | 不生成计划，等待新的结构或有效回踩状态 |
| `POST_BREAKOUT_FAILED` / `POST_BREAKOUT_EXPIRED` | 不生成计划，等待 VCP 重新构建 |

因此，突破后状态不得输出旧结构的 `PULLBACK` 或 `BREAKOUT` 计划。`POST_BREAKOUT_RETEST` 本身只表示可继续评估的窗口，不等同于可执行买点；它必须先通过模型二的缩量、价格确认、长上影/风险和卖压阻断检查。这项约束优先于成熟阶段和当日信号的普通分支。

`RETEST_FOLLOW` 复用模型二的统一回踩时间窗：突破后第 1-2 日为 `FAST`，仅标记时间、不限制潜在等级；第 3-10 日为 `STANDARD`；第 11-15 日为 `LATE`，最高 C；超过第 15 日不再生成次日计划。计划面向下一交易日，因此第 10 日生成的次日计划按 `LATE` 最高 C 处理，第 15 日即使当日仍有 `RETEST_BUY`，也不得为第 16 日续发计划。

同一只股票在允许窗口内连续满足模型二 `RETEST_BUY` 时，可以连续生成 `RETEST_FOLLOW`。每一天都必须按当日量价重新确认，不因前一天已提示而禁止，也不因连续提示自动升级质量。该计划只提示下一交易日仍可能成立的机会条件，不读取或推断持仓，不表达“应加仓”，是否执行由用户自行判断。

---

## 四、计划类型

Signal Plan 只输出三类买点计划，不输出等待突破或等待回踩等非执行观察条件；这类观察由 Bloom 负责。Signal Plan 是模型二买点逻辑的前置分析层，原则上只把模型二输出的阈值转化为次日执行区间和统计展示。

`setup_family` 使用大写枚举：

| setup_family | 含义 |
|--------------|------|
| `PULLBACK` | 缩量回踩类买点 |
| `BREAKOUT` | 枢轴突破类买点 |
| `RETEST` | 突破后回踩确认类买点 |

`plan_action` 使用大写枚举：

| plan_action | 含义 |
|-------------|------|
| `NEW` | 下一交易日可能首次触发的买点计划 |
| `FOLLOW` | 当日已经触发买点，下一交易日仍可延续参与的条件计划 |

`setup_type` 使用大写枚举：

| setup_type | 含义 |
|------------|------|
| `PULLBACK_BUY` | 成熟结构内缩量回踩计划 |
| `BREAKOUT_BUY` | 成熟结构枢轴突破计划 |
| `RETEST_BUY` | 已突破后的缩量回踩确认计划 |
| `PULLBACK_FOLLOW` | 当日回踩买点后的次日延续计划 |
| `BREAKOUT_FOLLOW` | 当日突破买点后的次日延续计划 |
| `RETEST_FOLLOW` | 当日回踩确认买点后的次日延续计划 |

当日 `setup_signal=PULLBACK_BUY` 时，Signal Plan 必须输出 `PULLBACK_FOLLOW`。若该股票同时属于成熟结构且收盘价仍低于突破触发价，可以额外输出 `BREAKOUT_BUY` 的 `NEW` 计划，用于描述次日可能发生的枢轴突破买点。

---

## 五、执行区间输出原则

Markdown 主表必须给出已经计算好的具体数值，不得展示乘法公式。

允许：

```text
普通买点触发价：332.00-355.10
A类价格区：335.30-345.20
普通触发量能：128万手以上
A类量能：166万手以上
失效价：316.40
```

禁止：

```text
普通买点触发价：pivot * 1.01 到 pivot * 1.08
普通触发量能：vol_ma20 * 1.0 以上
```

JSON 可以保留公式来源，放在 `formula_ref` 中，供复盘和调参使用。

价格保留 2 位小数；成交量 JSON 保留原始数据源数值，Markdown 统一展示为“万手”。当上限为空时，Markdown 展示为“以上”。

---

## 六、买点计划规则

### 6.1 BREAKOUT_BUY

适用对象：

```text
structure_stage in {VCP_MATURE, VCP_TIGHT}
close 未明显突破延伸
```

区间规则：

```text
普通买点触发价 = [structure_pivot × close_buffer_ratio, structure_pivot × max_close_extension_ratio]
A类价格区 = [structure_pivot × ideal_low_ratio, structure_pivot × ideal_high_ratio]
B 类量能 = min(vol_ma20 × volume_ma20_ratio, vol_ma5 × volume_ma5_ratio) 以上
A 类量能 = vol_ma20 × ideal_volume_min_ratio 以上
```

等级判定：

| 等级 | 条件 |
|------|------|
| A | 价格落在 A 类价格区，成交量达到 A 类量能，结构分和风险满足 A 类阈值 |
| B | 价格落在普通买点触发价区，成交量达到 B 类量能，结构分和风险满足 B 类阈值 |
| C | 价格或量能只满足最低边界，或结构分不足 A/B |

超过 `structure_pivot × overextended_ratio` 的计划不得输出买点计划；是否等待回踩由 Bloom 观察项处理。

### 6.2 PULLBACK_BUY

适用对象：

```text
structure_stage in {VCP_MATURE, VCP_TIGHT}
价格仍在结构内，未突破延伸
```

回踩支撑优先级：

```text
MA20
MA60
last_contraction_low
support_price
```

区间规则：

```text
普通买点触发价 = [support_anchor × pullback_low_ratio, support_anchor × pullback_high_ratio]
A类价格区 = [support_anchor × ideal_low_ratio, support_anchor × ideal_high_ratio]
B 类缩量 = vol_ma20 × volume_max_ratio 以下
A 类缩量 = vol_ma20 × ideal_volume_max_ratio 以下
失效价 = 低于触发区下沿的最近有效失效位，优先取 last_contraction_low × invalid_low_ratio、支撑锚点 × invalid_anchor_ratio、模型二 invalid_price 中低于触发区下沿的最高值
```

等级判定：

| 等级 | 条件 |
|------|------|
| A | 价格落在 A 类价格区，成交量低于 A 类缩量上限，未破失效价，结构分和风险满足 A 类阈值 |
| B | 价格落在触发区，成交量低于 B 类缩量上限，未破失效价 |
| C | 回踩位置或缩量等级不足，只作观察 |

### 6.3 RETEST_BUY

仅对已经突破过的结构生成。

若当日 `setup_signal=BREAKOUT_BUY`，下一交易日只生成 `BREAKOUT_FOLLOW`；不会输出 `RETEST_BUY`，因为模型二 RETEST 需要已经发生有效突破后的回踩确认，第一版不凭空生成等待项。

普通成熟结构不得因为 `close >= structure_pivot × close_buffer_ratio` 自动生成 `RETEST_BUY` 的 `NEW` 计划。`RETEST` 只在模型二已经识别出 `RETEST_BUY` 时输出 `FOLLOW`。

区间规则：

```text
回踩收盘价 = [structure_pivot × retest_low_ratio, structure_pivot × retest_high_ratio]
确认价 = max(structure_pivot, MA10 或 close)
B 类缩量 = 当前突破日 volume × pullback_volume_max_ratio 以下
A 类缩量 = 当前突破日 volume × ideal_pullback_volume_max_ratio 以下
失效价 = structure_pivot × invalid_ratio
```

RETEST 的 `structure_pivot`、突破量和 `invalid_price` 继续优先继承模型二事实；次日 `trigger_price_low/high` 与 `ideal_price_low/high` 必须由 Signal Plan 当前 `retest_buy` / `retest_follow` 配置计算，不得用模型二 `setup_plan_inputs.retest.price_low/high` 或 `ideal_price_low/high` 覆盖。模型二的 `price_low` 是当日结构回踩容忍边界，不等同于次日执行下沿。计划必须满足 `trigger_price_low > invalid_price`，使“未失效但未重新确认”的价格区间保持为等待状态。

若模型二未输出 MA10，Signal Plan 第一版用 `structure_pivot` 作为确认价，不自行重新拉行情计算 MA10。

---

## 七、输出

文件输出：

```text
signal_plan/signal_plan_<YYMMDD>.json
signal_plan/signal_plan_<YYMMDD>.md
```

Signal Plan 必须先写入 `cache/strategy/strategy_data.sqlite`，再从数据库当前版本发布上述文件；输入 Quant 也优先读取同库快照。该存储顺序不改变任何计划生成条件，详细契约见 `instructions/strategy-data.md`。

每条计划标准字段：

```text
code
name
plan_type
setup_family
plan_action
setup_type
setup_signal
target_quality
plan_priority
model2_stage
model2_setup_signal
post_breakout_state
structure_score
structure_risk_score
structure_risk_flags
close
volume
vol_ma20
trigger_price_low
trigger_price_high
ideal_price_low
ideal_price_high
volume_min
volume_max
ideal_volume_min
ideal_volume_max
invalid_price
formula_ref
plan_reason
risk_note
strategy_version
```

Markdown 报告分区：

1. 汇总统计
2. PULLBACK
3. BREAKOUT
4. RETEST
5. 字段说明

主表列：

```text
代码 | 名称 | 动作 | 最高 | 触发价 | A级价 | 触发量 | A级量 | 失效 | 说明
```

---

## 八、验收标准

- 不处理普通 `VCP_FORMING` 的首次计划。
- 不输出 `WAIT_*` 非执行等待条件。
- 同一股票可以出现在多个买点类型下，统计必须同时提供计划数和去重股票数。
- A/B 类计划必须展示具体价格和具体成交量范围。
- 公式不得出现在 Markdown 主表的执行区间中。
- 缺少量能字段时不得输出伪区间。
- 高风险或硬风险标的不得进入 A/B 主表。
- 突破后生命周期必须限制计划类型：`POST_BREAKOUT_RETEST` 仅可在模型二已输出 `RETEST_BUY` 时生成 `RETEST_FOLLOW`；其余突破后状态不得沿用旧 VCP 输出 `PULLBACK` / `BREAKOUT`。
- `FOLLOW_SETUP_PLAN` 不等同于昨日买点自动顺延，必须重新计算次日可参与区间。
- RETEST 次日执行区间必须使用 Signal Plan 的 NEW / FOLLOW 配置，且触发下沿严格高于继承的失效价；模型二结构容忍下沿不得直接成为执行下沿。
- Signal Plan 不更新 Bloom 状态、不写持仓账本、不输出最终交易建议。
