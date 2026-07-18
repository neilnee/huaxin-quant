# 模型二：量价精筛模型（自执行指令）

- **版本管理**: 由 Git 分支与提交历史管理，文件名不再携带版本号
- **最近更新**: 2026-07-18（model2_quant_v12）
- **核心目标**: 在模型一基本面候选池中，寻找 VCP 蓄力结构和可交易触发，输出可复现、可回测、可供模型三/四复用的结构化量价结果。
- **核心哲学**: 基本面先过滤烂公司，模型二只判断资金行为和价格位置。脚本负责确定性计算，LLM 只做可选解释，不参与结构阶段或交易触发判定。
- **输入**: `pool/pool_<YYMMDD>.csv`，或命令行指定 `--code/--codes`
- **输出**: `quant/quant_<YYMMDD>.csv` + `cache/quant_runs/quant_<YYMMDD>.json`
- **配套脚本**: `scripts/quant_filter.py`
- **策略配置**: `strategies/02-quant.json`

---

## 一、执行架构

模型二必须由脚本驱动，不能依赖对话上下文逐步执行。

```text
读取输入标的
→ 拉取/读取近 200 个交易日日线
→ 计算技术指标
→ 确定性识别 VCP 结构阶段、交易触发和结构风险事实
→ 计算 structure_score 与 structure_risk_score
→ 输出 CSV + JSON
→ 可选调用 LLM 解释 Top N 或指定个股
```

### CLI

```bash
python3 scripts/quant_filter.py
python3 scripts/quant_filter.py --date 260707
python3 scripts/quant_filter.py --pool pool/pool_260703.csv
python3 scripts/quant_filter.py --code 300604 --name 长川科技
python3 scripts/quant_filter.py --codes 300604,300442
python3 scripts/quant_filter.py --code 300604 --with-llm
python3 scripts/quant_filter.py --code 300604 --json
```

`--date` 用于回测或复盘指定交易日，支持 `YYMMDD` 与 `YYYY-MM-DD` 两种格式；未指定时按共享数据层的预期最近交易日运行。脚本会据此读取 `pool/pool_<YYMMDD>.csv`、统一日线库，并写出同日期的 `quant/` 与 `cache/quant_runs/` 文件。

### 职责边界

| 模块 | 职责 |
|------|------|
| 共享数据层 | 日线拉取、缓存、数据源降级、标准 OHLCV 结构 |
| 策略配置 | 阈值、权重、阶段参数、风险分数、触发信号参数 |
| 脚本 | 指标计算、结构识别算法、评分执行、排序、输出 |
| LLM | 可选解释、异常复核、观察建议 |
| 模型三 | 估值锚点，判断贵便宜 |
| 模型四 | 结合估值和持仓执行交易动作 |

LLM 失败不能影响主流程。默认不调用 LLM。

### 策略配置边界

模型二策略参数统一放在：

```text
strategies/02-quant.json
```

配置文件必须包含：

```text
strategy_version
vcp
base_rules
contraction_rules
stage_rules
risk_rules
setup_rules
scores
classification
```

策略配置只承载“可调参数”，包括：

- VCP 观察窗口、摆动点窗口、回撤幅度和天数上下限。
- 结构有效期、pivot 距离、突破后延伸/回撤失效阈值。
- 收缩递减比例、量能递减/缩量/失败阈值。
- 阶段判定所需收缩轮数、紧致结构阈值。
- `PULLBACK_BUY` / `BREAKOUT_BUY` / `RETEST_BUY` 触发信号参数。
- 结构评分、量能评分、趋势评分、位置评分和风险分数。
- 触发信号对应的模型二建议仓位文本。

策略配置不承载：

- 日线指标计算公式。
- swing high / swing low 识别算法。
- VCP 收缩轮次扫描流程。
- 文件读写、缓存和 CLI 编排。
- 模型四/Bloom 的候选池保留、换出、持仓管理和最终交易建议。

每次调整模型二策略参数时，必须同步更新本指令卡说明，并递增或修改 `strategy_version`。模型二 CSV 和 JSON 必须写入 `strategy_version`，JSON `meta` 必须写入 `strategy_file`，便于历史复盘。

---

## 二、核心模型注释

> 这一节是交易模型的注释性说明，便于人工查看。脚本实现必须以本节定义为准。

### VCP 结构观察

VCP 结构观察用于识别观察对象，不是买点。

它寻找的是：股票已经有资金认可，随后进入波动收敛、成交量下降、抛压变轻的阶段。

```text
第一段上涨或修复：资金开始参与
第一次回撤：幅度较大但趋势未坏
第二次回撤：幅度变小，成交量下降
第三次整理：波动更窄，成交量更低
```

VCP 结构观察的交易含义：

- 不是长期趴着不动。
- 不是短期情绪过热。
- 筹码正在稳定。
- 后续需要等待 `PULLBACK_BUY` 或 `RETEST_BUY`。

> **收缩幅度测量口径**（model2_quant_v8 起）：VCP 收缩的转折点与振幅都使用**收盘价 Swing**。每轮回调从收盘价局部高点到后续收盘价局部低点计算：`close_pullback_pct = (end_close - start_close) / start_close`，且必须 `end_close < start_close`。日内最高/最低价不参与收缩轮次、递减判定或收盘修复；它们只用于 Pivot、失效位和影线风险审计。这样收缩的定位与测量口径一致，排除影线造成的伪收缩。

若同一收缩段的日内振幅比收盘振幅大 8pct 以上，标记 `INTRADAY_CLOSE_DIVERGENCE`：保留收盘结构，但降低买点评分并提示人工复核。

### PULLBACK_BUY：结构内缩量回踩低吸

`PULLBACK_BUY` 是 VCP 未突破前的低吸机会，适合轻仓试探。

```text
VCP 结构已经成立
股价缩量回踩 MA20/MA60/收敛下沿
前低不破
均线不明显下行
价格没有过热
```

`PULLBACK_BUY` 买的是风险收益比。价格较低，失效位清楚，但突破尚未确认，确定性低于 `RETEST_BUY`。

仓位建议：

```text
20%-30%
```

### BREAKOUT_BUY：VCP 枢轴突破参与

`BREAKOUT_BUY` 是 VCP 成熟后收盘有效站上 pivot 的突破参与点。

```text
VCP_MATURE / VCP_TIGHT 结构已经成立
收盘价站上 structure_pivot × 1.01
当日量能恢复，至少不低于 vol_ma20 或 vol_ma5
突破日无明显长上影或放量滞涨
距离 MA20 不过度乖离，短期没有过热
```

`BREAKOUT_BUY` 买的是启动确认，确定性低于突破后回踩确认，但能避免强势股突破后不回踩导致完全踏空。

仓位建议：

```text
40%-50%
```

### RETEST_BUY：突破后回踩确认

`RETEST_BUY` 是 VCP 突破后的确认买点。

```text
先放量突破箱体上沿/收敛上沿/近 60 日高点
随后 3-10 个交易日内缩量回踩
回踩不有效跌破突破位
重新站回突破位或 MA10
```

`RETEST_BUY` 买的是确定性。价格通常高于 `PULLBACK_BUY`，但突破已经发生并经过回踩验证。

仓位建议：

```text
加至 60%-80%
```

### 交易触发仓位路径

```text
PULLBACK_BUY 买入 20%-30%
→ 若直接突破且触发 BREAKOUT_BUY：加至 40%-50%
→ 若突破后回踩确认：RETEST_BUY 加至 60%-80%
→ 若跌破失效位：减仓或退出
```

---

## 三、数据与缓存

每只股票拉取近 200 个交易日：

```text
date, open, high, low, close, volume, turnover
```

日线数据统一通过 `scripts.data.market_data_service.MarketDataService` 获取，链路为：

```text
SQLite 日线库 → 缺口检测与补数 → 通达信 TDX/mootdx → 妙想 API 备用源 → 写回 SQLite
```

统一数据服务先按 `run_date` 从 `cache/market_data/market_data.sqlite` 读取所需窗口；仅当目标日缺失或历史不足时补取。主源失败、返回空数据或未覆盖目标日时，才使用妙想 API 备用源；两者均标准化为 OHLCV 后写回数据库并记录来源。

模型二必须把本次 `run_date` 传入统一数据服务。未指定 `--date` 时，`run_date` 由共享数据层按 15:00 分隔线确定：15:00 前取前一交易日，15:00 后取当日，周末回退到周五。指定 `--date` 时，数据库覆盖校验、缺口补数和回源后数据截断都以该指定交易日为准。

通达信 mootdx 是主数据源；当通达信限流、返回空数据、结构异常、异常抛出或未覆盖目标交易日时，脚本才尝试妙想 API。若本地未配置 `MX_APIKEY`，则通达信失败会直接返回取数失败。通达信数据统一写入 SQLite；模型二判定不得依赖 `turnover`。

数据库只保存原始日线，不保存指标列；指标每次实时计算，避免规则变更后旧指标污染。数据库命中必须同时满足：

```text
文件名日期 = 当前运行日期；或运行日缓存未命中时，为该股票不晚于运行日的最近可用缓存
缓存内最后一条 K 线日期 >= 目标交易日
```

若数据库中目标标的未覆盖运行日，脚本必须补数；若回源返回了目标交易日之后的数据，数据层必须先截断到 `<= as_of_date` 再写入，防止复盘指定日期时混入未来 K 线。

---

## 四、指标体系

### 均线与趋势

```text
MA5, MA10, MA20, MA60, MA120
MA20_slope, MA60_slope, MA120_slope
```

斜率用最近 N 日均线做线性回归，转成每日百分比。

### 波动收敛

```text
range_10 = (high_10 - low_10) / low_10 × 100
range_20
range_60
ATR14_pct
```

VCP 关注：

```text
range_10 < range_20 < range_60
ATR14_pct 处于下降或低位
最近回撤小于前一次回撤
```

### 量能

```text
vol_ma5, vol_ma20, vol_ma60
量比 = volume / vol_ma5.shift(1)
volume_dry_up = vol_ma5 / vol_ma20
volume_dry_up_60 = vol_ma10 / vol_ma60
```

### 价格位置

```text
distance_ma20
distance_ma60
distance_high_60
distance_low_60
chg_5
chg_20
chg_60
```

### 风险识别

```text
过热：短期涨幅过大、距离 MA20 过远
长上影：放量冲高回落
放量滞涨：量放大但价格不涨
趋势破坏：MA20 明显下行或跌破关键支撑
假突破：突破后跌回箱体
```

---

## 五、模型二输出契约与确定性规则

模型二只负责量价结构发现，不负责最终入池、估值过滤、持仓管理或交易风控。模型四负责将模型二输出与估值、持仓、批次和人工标记合并，形成最终跟踪和交易建议。

模型二输出分为四类：

| 字段 | 职责 |
|------|------|
| `structure_type` | 当前主要量价形态类型 |
| `structure_stage` | 形态发展阶段，只描述结构，不代表买点 |
| `setup_signal` | 当日是否触发交易形态 |
| `action_hint` | 模型二基于量价侧给出的动作提示，不考虑估值和持仓 |
| `dashboard/data/<YYYYMM>/signals_context_<YYMMDD>.js` | 只读信号发现页面数据包，展示当日触发与次日计划，不改变模型二判定 |

### 0.1 structure_type：形态类型

| structure_type | 说明 |
|----------------|------|
| `VCP` | 波动收缩结构，包括正在形成、成熟、紧致、突破后或重建中的 VCP 相关结构 |
| `TREND` | 趋势较强但未形成标准 VCP 收缩轮次，只观察趋势 |
| `NONE` | 没有有效量价结构 |
| `DATA_ISSUE` | 行情数据不足或异常，无法判断 |

### 0.2 structure_stage：结构阶段

| structure_stage | 说明 |
|-----------------|------|
| `VCP_EARLY` | 识别到 1 轮有效收缩，VCP 刚开始形成 |
| `VCP_FORMING` | 至少 2 轮有效收缩，后一轮小于或接近前一轮 |
| `VCP_MATURE` | 至少 3 轮有效收缩，幅度整体递减 |
| `VCP_TIGHT` | VCP_MATURE 且最后一轮收缩较窄，价格接近 pivot |
| `TREND_WATCH` | 趋势强但未形成有效 VCP 收缩轮次 |
| `POST_BREAKOUT` | 历史 VCP 已明显突破并延伸，旧结构不再作为当前 VCP 观察结构 |
| `TREND_REBUILD` | 历史结构突破后深回撤，需等待重新形成 |
| `STRUCTURE_INVALID` | 结构已破坏或过期，不适合按当前结构交易 |
| `NONE` | 没有可识别结构 |
| `DATA_ISSUE` | 数据不足或异常 |

结构阶段只回答“形态发展到哪里”，不回答“今天能不能买”。例如 `VCP_TIGHT` 说明结构已经紧致、接近变盘窗口，但如果没有缩量回踩或突破后回踩确认，`setup_signal` 仍应为 `NONE`。

各阶段含义：

```text
VCP_EARLY
- 仅识别到第一轮有效收缩。
- 说明资金结构可能刚开始沉淀，但样本不足，不能证明收缩递减。
- 模型四可低优先级记录，不应作为交易依据。

VCP_FORMING
- 至少两轮有效收缩，后一轮小于或接近前一轮。
- 说明波动开始变窄，筹码可能进入整理过程。
- 进入观察池，等待第三轮收缩、量能改善或回踩触发。

VCP_MATURE
- 至少三轮有效收缩，收缩幅度整体递减。
- 说明 VCP 结构较完整，形态质量已经值得重点跟踪。
- 仍不是买点，需要等待 PULLBACK_BUY 或 RETEST_BUY。

VCP_TIGHT
- VCP_MATURE 之后，最后一轮收缩较窄，价格接近 pivot。
- 说明结构已经进入紧致区，后续容易出现突破或方向选择。
- 高优先级观察；若同时出现缩量回踩，可触发 PULLBACK_BUY。

TREND_WATCH
- 趋势较强，但没有形成标准多轮 VCP 收缩。
- 说明股票强，但不按 VCP 买点处理。
- 模型四可以观察趋势，不应直接按 VCP 交易触发处理。

POST_BREAKOUT
- 历史 VCP 已明显突破并延伸，旧结构已走完。
- 说明不适合追高，后续要么等待回踩确认，要么等待新结构。
- 模型四对未持仓标的应避免追买；对持仓标的可转入趋势止盈管理。

TREND_REBUILD
- 旧结构突破后又深回撤，或结构失效后重新整理。
- 说明旧 VCP 不再有效，需要重新形成收缩组。
- 模型四应等待重建，不应沿用旧支撑或旧 pivot 做买入依据。
```

### 0.3 setup_signal：交易触发

| setup_signal | 说明 |
|--------------|------|
| `NONE` | 没有交易触发 |
| `PULLBACK_BUY` | VCP 结构内缩量回踩买点，适合轻仓试探 |
| `BREAKOUT_BUY` | VCP 成熟后枢轴突破参与点，适合半仓参与 |
| `RETEST_BUY` | 突破后回踩确认买点，确认度高于 PULLBACK_BUY |

`setup_signal` 必须建立在 `structure_stage` 之上。它不是独立形态，而是“结构阶段 + 当日量价触发条件”的结果。
模型二使用四段式买点评分：硬条件只判断买点形态是否成立；`setup_pattern_score` 表示动作分；`setup_score` 是结构基础分、动作分和风险修正后的最终买点分。

```text
setup_signal = structure_stage + trigger_conditions
setup_pattern_score = action_type_base + action_quality_score
setup_score = stage_base + setup_pattern_score + risk_adjust
setup_quality = setup_signal + setup_score
```

触发定义：

```text
NONE
- 没有出现可交易触发。
- 可能是好结构但未到买点，例如 VCP_MATURE / VCP_TIGHT 只是观察。
- 也可能是结构失效、趋势观察、数据不足。

PULLBACK_BUY
- 结构内缩量回踩买点。
- 前提阶段：VCP_FORMING / VCP_MATURE / VCP_TIGHT。
- 硬条件：回踩 MA20 / MA60 / 收敛下沿，具备基础缩量，最近收缩低点不破，MA20 斜率未明显走坏，无放量长上影，无趋势硬风险。
- 评分项：买点类型基础分、回踩位置、缩量质量、前低确认。
- 缩量确认：`volume_dry_up < 0.80`，或收缩段均量逐轮递减且当前 1-3 日量能仍处于最近收缩段低量区。单日地量只能作为确认，不得单独触发买点。
- 交易含义：低吸试探，风险收益比优先，确定性低于 RETEST_BUY。
- 模型二量价侧建议：BUY_LIGHT，参考仓位 20%-30%。

BREAKOUT_BUY
- VCP 枢轴突破参与点。
- 前提形态：当前存在有效 VCP 结构，`structure_stage` 至少为 `VCP_FORMING`，且两轮以上主收缩结构质量需由评分确认。
- 硬条件：最新收盘站上 `structure_pivot × 1.01`，当日成交量高于 `vol_ma20` 或 `vol_ma5`，突破日无明显长上影/放量滞涨，无趋势硬风险。
- 初始突破边界：最新收盘不得高于 `structure_pivot × 1.08`，否则视为突破后延伸，不再触发 `BREAKOUT_BUY`。
- 评分项：买点类型基础分、突破幅度、突破量能、K线确认。
- 交易含义：突破正在发生，可以参与但尚未经过回踩验证，确定性低于 RETEST_BUY。
- 模型二量价侧建议：BUY_BREAKOUT，参考仓位 40%-50%。

RETEST_BUY
- 突破后回踩确认买点。
- 前提形态：当前存在有效 VCP 结构，`structure_stage` 至少为 `VCP_FORMING`。
- 排除条件：`structure_valid=false`、`POST_BREAKOUT`、`TREND_REBUILD`、`TREND_WATCH`、`NONE`、`DATA_ISSUE`，以及趋势硬风险标记（`DOWNTREND`、`DEEP_FALL`）均不得触发 `RETEST_BUY`；短期过热只影响 `setup_score`。
- 硬条件：先有效突破关键位（突破日不能是放量长上影），随后缩量回踩，回踩不有效跌破突破位，最新收盘重新站回突破位或 MA10，无趋势硬风险。
- 评分项：买点类型基础分、回踩位置、回踩量能、重新确认。
- 交易含义：突破已经发生并经回踩确认，确定性高于 PULLBACK_BUY。
- 模型二量价侧建议：BUY_STANDARD；A 级参考仓位 60%-80%，B 级参考仓位 40%-50%，C 级轻仓或观察。
```

最终买点分公式：

```text
setup_score = stage_base
            + action_type_base
            + action_quality_score
            + risk_adjust
```

交易含义：结构阶段决定买点基础上限，买点类型决定天然确认度，动作质量决定当天执行质量，风险标识负责加分或降级。

模型二只判断量价触发是否成立；模型三估值是否支持、模型四是否实际给买入建议，需要在模型四中决定。

### 0.4 action_hint：量价侧动作提示

| action_hint | 说明 |
|-------------|------|
| `WATCH` | 只观察，不给买入动作 |
| `BUY_LIGHT` | 缩量回踩触发，量价侧允许轻仓试探 |
| `BUY_BREAKOUT` | 枢轴突破触发，量价侧允许半仓参与 |
| `BUY_STANDARD` | 突破回踩确认，量价侧允许标准仓位 |
| `AVOID_CHASE` | 结构已突破延伸或位置过热，不追高 |
| `WAIT_REBUILD` | 旧结构失效，等待重新形成 |
| `REJECT` | 不进入模型二有效结构 |
| `DATA_SKIP` | 数据不足，跳过 |

`action_hint` 是模型二基于量价侧的动作提示，不考虑估值锚点、当前持仓、批次成本和人工标记。

```text
WATCH
- 结构值得观察，但没有交易触发。
- 常见于 VCP_EARLY / VCP_FORMING / VCP_MATURE / VCP_TIGHT。
- 模型四可加入观察池，并根据 structure_score 排序。

BUY_LIGHT
- setup_signal=PULLBACK_BUY。
- 说明量价侧出现结构内缩量回踩，适合小仓位试探。
- 模型四仍需检查估值安全边际和账户已有仓位。

BUY_BREAKOUT
- setup_signal=BREAKOUT_BUY。
- 说明量价侧出现 VCP 枢轴突破，但尚未经过回踩确认。
- 模型四可在估值支持时给出半仓参与建议；若已持有 PULLBACK_BUY 仓位，则可考虑加至 40%-50%。

BUY_STANDARD
- setup_signal=RETEST_BUY。
- 说明量价侧出现突破后回踩确认，买点确定性更高。
- 模型四可在估值支持时给出标准仓位建议；若已持仓，则可能转化为加仓或继续持有。

AVOID_CHASE
- 结构已经突破延伸、位置过高或短期过热。
- 对未持仓标的表示不追买；对持仓标的不是卖出结论，只提示模型四转入风控/止盈观察。

WAIT_REBUILD
- 旧结构失效或深回撤，需要等待新一轮 VCP 形成。
- 模型四不应沿用旧结构买点。

REJECT
- 没有有效结构，不进入模型二有效结构输出。

DATA_SKIP
- 数据不足或异常，无法判断。
```

### 0.5 模型二与模型四边界

模型二输出的是“形态事实”和“量价侧提示”：

```text
structure_type / structure_stage / setup_signal
structure_score / structure_risk_flags / structure_risk_score
setup_pattern_score / setup_score / setup_quality / setup_reasons / setup_misses
support_price / invalid_price / breakout_level
```

模型四负责：

```text
是否进入跟踪池
是否需要估值
模型三安全边际是否足够
未持仓是否可以买
已持仓是否加仓、减仓、止损或止盈
最终报告渲染和交易建议
```

### VCP 结构过程监控

基础条件：

```text
有效交易日 >= 80
close > MA60 或 MA20 >= MA60
MA60_slope >= -0.03%/日
近 120 日最大回撤不超过 35%
```

趋势背景过滤器（VCP 要求上升趋势前提）：

```text
close < MA120 → 触发 BELOW_MA120 风险标记（+20 风险分）
  ├─ 收缩低点停止下移 → 结构降级（MATURE→FORMING, FORMING→EARLY），不拒绝
  └─ 收缩低点持续下移 → 硬拒绝（趋势背景不成立，不纳入 VCP）
close >= MA120 → 不受影响
```

MA120 作为长线趋势锚点，确保 VCP 整理发生在上升趋势背景下，
而非下跌趋势中的反弹或筑底。

VCP 不再使用 `range_10/range_20/range_60` 等截面指标做 `6选3` 判定。VCP 的主判定改为识别形成过程：

```text
右侧修复或上涨后
→ 出现第 1 轮收缩
→ 出现第 2 轮更小的收缩
→ 出现第 3 轮更小的收缩
→ 量能逐轮下降或最后一轮明显缩量
→ 股价靠近 pivot / 前高附近窄幅整理
```

### 1.1 收缩轮次识别

在最近 80-120 个交易日中识别收盘价局部高点和后续收盘价局部低点。一轮 contraction 定义为：

```text
从收盘价局部高点回撤到后续收盘价局部低点
回撤幅度 >= 4%
持续时间 3-45 个交易日，按包含首尾的 K 线数量计算
低点后有一定修复，不能是单边下跌未止
```

每轮 contraction 记录：

```text
start_date / end_date
start_close / end_close / close_pullback_pct（VCP 判定口径）
intraday_high / intraday_low / intraday_pullback_pct（审计口径）
high_price / low_price（兼容字段，等同于 intraday_high / intraday_low）
pullback_pct（兼容字段，等同于 close_pullback_pct）
duration_days
avg_volume
recovery_pct
```

`duration_days = low_idx - high_idx + 1`，与 `avg_volume` 的取样区间一致，均包含局部高点日和局部低点日。

### 1.2 收缩递减

核心条件：

```text
abs(C2.pullback) <= abs(C1.pullback) * 0.90   # 明显递减
abs(C3.pullback) <= abs(C2.pullback) * 0.90
```

允许轻微容差：

```text
abs(Cn.pullback) <= abs(Cn-1.pullback) * 1.05
```

满足容差但未明显递减时，不剔除，但降低阶段和评分。

### 1.3 当前有效性

模型二只识别**当前正在形成**的 VCP，不追认已经走完或已经被大幅突破的历史结构。收缩轮次必须组成一个当前有效的 contraction group。

当前 VCP 结构组不能机械取最近三轮 contraction。脚本必须枚举最近候选组，并优先选择更符合当前主结构的 group：

```text
收缩幅度递减或接近递减
量能逐轮下降或近期 drying
当前价格接近 structure_pivot
最后一轮收缩距离当前更近
组内轮次足够，但早期噪声回调不得污染主收缩序列
```

收缩先按时间切分为独立 cluster：相邻两段之间超过 **25 个交易日**，即视为新的底部结构。当前 VCP 只从最新 cluster 选择，组内首尾跨度最多 **60 个交易日**。旧 cluster 仍保留在 `contractions` 供审计，但不得参与当前 `contraction_group` 的轮次、递减判定、阶段和买点评分。

### 突破后生命周期

原 VCP 的 `price_breakout` 发生在最后一轮收缩后，收盘价首次站上 `structure_pivot × 1.01`。突破并不立即删除原结构：它仍用于记录完整的“收缩 → 突破 → 跟随/回踩”质量，但买点权限转入突破后状态管理。

| post_breakout_state | 含义 | 买点权限 |
|---|---|---|
| `PRE_BREAKOUT` | 尚未发生价格突破 | PULLBACK / BREAKOUT |
| `POST_BREAKOUT_HOT` | 突破后快速上冲 | 不追高 |
| `POST_BREAKOUT_RETEST` | 突破后 15 日内受控回踩 Pivot | 仅 RETEST |
| `POST_BREAKOUT_CONSOLIDATING` | 突破后 16-20 日仍未深度失守 | 观察新 base，不沿用旧买点 |
| `POST_BREAKOUT_FAILED` | 收盘跌破 Pivot × 0.97，或突破后回撤过深 | WAIT_REBUILD |
| `POST_BREAKOUT_EXPIRED` | 突破后超过 20 日，旧买点窗口结束 | WAIT_REBUILD |

硬边界：一旦进入任何 `POST_BREAKOUT_*` 状态，原 `contraction_group` 永久禁止 `PULLBACK_BUY` 与重复 `BREAKOUT_BUY`。当状态失败或过期后，旧结构仅保留审计；之后必须从突破后开始形成新的 contraction cluster，才能重新产生 PULLBACK / BREAKOUT。

相邻收缩轮次允许轻微扩张，但明显扩张会打断旧 VCP 组，后一轮应视为新结构的起点：

```text
abs(Cn.pullback) > abs(Cn-1.pullback) * 1.50
且 abs(Cn.pullback) - abs(Cn-1.pullback) >= 5pct
```

交易含义：VCP 的核心是波动和抛压逐步收敛。若窄幅整理后突然出现大一级别回撤，说明旧收敛结构被破坏，不能为了凑满三段而把它和前面的窄收缩归为同一组。小幅扩张只降低结构质量，不直接重置。

对每个候选 contraction group 计算：

```text
structure_pivot = group 内 high_price 最大值
market_pivot = 最近 60 日高点
structure_age_days = 当前交易日距离最后一轮 contraction end 的交易日数
post_structure_gain = 最后一轮低点后最高价 / structure_pivot - 1
post_structure_drawdown = 当前价 / 最后一轮低点后最高价 - 1
```

当前有效性规则：

```text
structure_age_days <= 45
当前价距离 structure_pivot 不低于 -18%
market_pivot <= structure_pivot * 1.10
post_structure_gain <= 25%
post_structure_drawdown >= -18%
```

若不满足，说明该结构已经过期、已经突破完成，或突破后又进入重建阶段，不再作为当前 VCP 观察结构。

### 1.3.1 突破前旧组破位（保守失效守卫）

为避免历史上收缩较漂亮、但随后已被破坏的旧组继续被选为当前 VCP，脚本仅对**尚未发生有效突破**的候选组增加以下保守否决条件：

```text
最后一轮收缩结束后的 30 个交易日内：
连续至少 2 个交易日收盘 < 最后一轮收缩低点 × 0.97
且该窗口内至少一个收盘 < 最后一轮收缩低点 × 0.92
```

上述条件必须同时满足。它不因单日盘中下影、轻微收盘跌破或正常回踩而失效；命中后标记 `post_group_support_break`，旧组不得再用于阶段、评分或买点，需从破位后的新低开始重建收缩组。阈值由 `strategies/02-quant.json` 的 `vcp.post_group_reset` 统一配置。

若旧组失效后脚本改选其他候选，而该候选本身未形成有效 VCP 阶段，则不得仅因 `POST_BREAKOUT_RETEST` 生命周期被抬升为 `WATCH`，应保持 `REJECT`。该保护只阻止“否决旧组后意外放宽”，不改变破位后正常形成的早期/形成中 VCP，亦不改变既有的突破后状态管理。

失效原因：

| invalid_reason | 说明 |
|----------------|------|
| structure_too_old | 最后一轮收缩距当前太久 |
| far_below_structure_pivot | 当前价距离结构 pivot 过远 |
| post_structure_extended | 结构后涨幅过大，旧 VCP 已完成 |
| post_structure_drawdown | 结构后再度深回撤，需要重新形成 |
| post_group_support_break | 未有效突破前，旧组随后出现连续且深度收盘跌破最后收缩低点 |

### 1.4 量能确认

量能作为质量分，不作为唯一硬门槛：

```text
C2.avg_volume < C1.avg_volume
C3.avg_volume < C2.avg_volume
最后一轮 volume_dry_up < 0.85
vol_ma20 < vol_ma60
```

量能状态：

| volume_pattern | 说明 |
|----------------|------|
| decreasing | contraction 期间均量逐轮下降 |
| drying | 最后一轮或近期明显缩量 |
| mixed | 量能不稳定 |
| failed | 回撤放量，质量差 |

### 1.5 VCP 结构成熟度

| structure_stage | 说明 |
|-----------------|------|
| VCP_EARLY | 识别到 1 轮有效收缩，VCP 刚开始形成 |
| VCP_FORMING | 至少 2 轮收缩，后一轮小于或接近前一轮 |
| VCP_MATURE | 至少 3 轮收缩，幅度明显递减 |
| VCP_TIGHT | VCP_MATURE 且最后一轮收缩较窄，价格接近 pivot |
| TREND_WATCH | 趋势强但未形成有效收缩轮次，不归入 VCP |
| POST_BREAKOUT | 历史 VCP 已明显突破，不再作为当前 VCP 观察结构 |
| TREND_REBUILD | 历史结构突破后深回撤，需要重新形成 |

### PULLBACK_BUY：结构内缩量回踩

必须先有 `VCP_FORMING`、`VCP_MATURE` 或 `VCP_TIGHT`，`VCP_EARLY` 只观察，不触发 `PULLBACK_BUY`。

且 `post_breakout_state = PRE_BREAKOUT`。已经突破的旧 VCP 即使价格回到 MA20 或旧 Pivot 附近，也不得重新触发 PULLBACK。

```text
volume_dry_up < 0.80
或：收缩段 avg_volume 逐轮下降，且最近 1-3 日均量 <= 最近收缩段 avg_volume × 1.10
distance_ma20 在 [-4%, +3%]，或 distance_ma60 在 [-5%, +5%]
close > 最近一轮 contraction low × 1.02
MA20_slope >= -0.03%/日
无放量长阴
setup_score >= 55
```

### BREAKOUT_BUY：VCP 枢轴突破

必须先有 `VCP_FORMING`、`VCP_MATURE` 或 `VCP_TIGHT`，`VCP_EARLY` 只观察，不触发 `BREAKOUT_BUY`。

且 `post_breakout_state = PRE_BREAKOUT`；已发生价格突破的原结构不允许重复触发 BREAKOUT。

```text
structure_valid = true
close > structure_pivot × 1.01
close <= structure_pivot × 1.08
当日成交量 > vol_ma20 × 1.0，或当日成交量 > vol_ma5 × 1.0
无明显长上影
无放量滞涨
无 DOWNTREND / DEEP_FALL
setup_score >= 55
```

且 `post_breakout_state = POST_BREAKOUT_RETEST`。RETEST 统一使用结构层记录的原 Pivot、突破日和突破后天数，不再另行从 60 日高点扫描一个可能与当前 VCP 无关的突破。

### RETEST_BUY：突破后回踩确认

突破识别：

```text
breakout_level = 最近 60 日箱体上沿/突破前高
突破日收盘价 > breakout_level × 1.01
突破日成交量 > vol_ma20 × 1.0
突破日无明显长上影
```

回踩确认：

```text
突破后 1-15 个交易日内；3-10 日为标准时间窗，1-2 日早期回踩降低评分
回踩低点在 breakout_level × 0.97 至 breakout_level × 1.005 区间内
回踩期缩量
最新收盘重新站回 breakout_level 或 MA10
setup_score >= 55
```

### RETEST 的结构量能准入与卖压阻断

RETEST 必须同时确认“当前回踩缩量”和“VCP 各收缩段没有明显供给扩张”：

| structure volume_pattern | 处理 |
|---|---|
| `decreasing` / `drying` | 保持原 RETEST 评分 |
| `mixed` | RETEST 可触发，但最高质量为 B |
| `failed` | 禁止 RETEST，输出 `WAIT_REBUILD` |

最新确认日还会检查 `FAILED_RETEST_SELLING`。仅对已满足其他 RETEST 前提的标的，当以下条件同时成立时硬阻断 RETEST：

```text
close < open
当日跌幅达到 max(板块阈值, min(ATR14_pct × 2, 涨跌停幅度 × 75%))
当日量 >= 前 5 日均量 × 1.20（前 5 日不含当天）
且当日量 >= 突破日量 × 0.80，或 >= 突破后前 5 日均量 × 1.35
```

板块基础跌幅阈值：主板 7%、创业板/科创板 10%、北交所 15%。该信号是 `setup_risk_flags`，只否决 `RETEST_BUY`，不改变 VCP 结构、PULLBACK_BUY 或 BREAKOUT_BUY 的规则。

---

## 六、评分与风险事实

### 6.0 setup_score 买点评分

买点评分公式：

```text
setup_score = stage_base
            + action_type_base
            + action_quality_score
            + risk_adjust
```

结构基础分：

| structure_stage | stage_base |
|-----------------|------------|
| `VCP_FORMING` | 30 |
| `VCP_MATURE` | 50 |
| `VCP_TIGHT` | 60 |
| `VCP_EARLY` | 不触发买点 |

买点类型基础分：

| setup_signal | action_type_base |
|--------------|------------------|
| `PULLBACK_BUY` | 6 |
| `BREAKOUT_BUY` | 10 |
| `RETEST_BUY` | 15 |

动作质量分为 0-15 分，三类买点分别评分。

`PULLBACK_BUY`：

| 维度 | 分数 |
|------|------|
| 位置质量 | `distance_ma20 ∈ [0%, +2%]` 得 5；`[-3%, 0%)` 或 `(+2%, +3%]` 得 2；只靠近 MA60 得 2；其他 0 |
| 量能质量 | `volume_dry_up < 0.80` 得 5；收缩段低量确认得 4；`volume_dry_up < 0.90` 得 2；其他 0 |
| 确认质量 | `close > last_low * 1.05` 且 `MA20_slope >= 0` 得 5；`close > last_low * 1.02` 且 `MA20_slope >= -0.03` 得 2；其他 0 |

`BREAKOUT_BUY`：

| 维度 | 分数 |
|------|------|
| 突破幅度 | `close / pivot ∈ [1.02, 1.05]` 得 5；`[1.01, 1.02)` 或 `(1.05, 1.08]` 得 2；其他 0 |
| 量能质量 | `volume > vol_ma20 * 1.5` 得 5；`> 1.2` 得 3；`> 1.0` 得 1；其他 0 |
| K 线确认 | 收盘接近日高、实体强、无明显上影得 5；站上 pivot 且无长上影得 2；其他 0 |

`RETEST_BUY`：

| 维度 | 分数 |
|------|------|
| 回踩位置 | `pullback_low / breakout_level ∈ [0.99, 1.003]` 得 5；`[0.97, 0.99)` 或 `(1.003, 1.005]` 得 2；其他 0 |
| 回踩量能 | 回踩期均量 `< 突破日量 * 0.70` 得 5；`< 0.85` 得 3；`< 1.0` 得 1；其他 0 |
| 重新确认 | `close >= breakout_level` 且收盘强得 5；`close >= breakout_level` 或 `close >= MA10` 得 2；其他 0 |

风险修正分：

```text
risk_adjust = clamp(sum(flag_adjustments), -20, +10)
无风险标识 = +10
```

| risk_flag | 修正 |
|-----------|------|
| `EXTENDED_FROM_MA20` | -5 |
| `MA20_DECLINE` | -6 |
| `BELOW_MA120` | -6 |
| `FAR_ABOVE_MA20` | -8 |
| `LONG_UPPER_SHADOW` | -10 |
| `VOLUME_STALL` | -10 |
| `OVERHEAT_CHG5` | -12 |
| `OVERHEAT_CHG20` | -12 |
| `DOWNTREND` | -20 |
| `DEEP_FALL` | -20 |

买点等级：

| setup_quality | setup_score |
|---------------|-------------|
| A | `>= 80` |
| B | `65-79` |
| C | `55-64` |
| D | `< 55` |

`setup_signal` 只有在硬条件成立且 `setup_score >= 55` 时触发。

```text
structure_score = stage_score
                + volume_score
                + trend_score
                + position_score
```

`structure_score` 只评价结构形态质量，分数越高，说明 VCP 越标准、越紧致、量能越健康、趋势越配合、位置越合理。它不直接决定最终买卖，模型四需结合估值、持仓和风险管理使用。

### 6.1 structure_score 评分明细

`structure_score` 由四部分相加后限制在 `0~100`：

```text
structure_score = stage_score
                + volume_score
                + trend_score
                + position_score
```

#### stage_score：结构阶段基础分

| structure_stage | 分数 | 含义 |
|-----------------|------|------|
| `VCP_EARLY` | 18 | 识别到早期收缩，但结构样本不足 |
| `VCP_FORMING` | 32 | 至少两轮收缩，结构开始形成 |
| `VCP_MATURE` | 45 | 至少三轮收缩，结构较完整 |
| `VCP_TIGHT` | 55 | 结构成熟且最后一轮较窄，接近 pivot |
| `TREND_WATCH` | 12 | 趋势强但不是标准 VCP |
| `POST_BREAKOUT` | 8 | 历史结构已突破延伸，不再作为当前买点 |
| `TREND_REBUILD` | 8 | 旧结构失效后等待重建 |

#### volume_score：量能评分

| 条件 | 分数 | 含义 |
|------|------|------|
| `volume_pattern=decreasing` | +15 | 收缩轮次平均量能递减 |
| `volume_pattern=drying` | +8 | 当前量能处于缩量状态 |
| `volume_pattern=failed` | -10 | 最近量能放大，收缩失败 |
| `volume_dry_up < 0.8` | +10 | 近 5 日均量显著低于近 20 日均量 |
| `vol_ma20 < vol_ma60` | +5 | 中期量能低于长期量能，抛压减轻 |

#### trend_score：趋势评分

| 条件 | 分数 | 含义 |
|------|------|------|
| `MA20 >= MA60` | +7 | 中短期趋势未破坏 |
| `MA20_slope >= 0` | +4 | MA20 没有向下 |
| `MA60_slope >= -0.03` | +4 | MA60 没有明显走弱 |

#### position_score：位置评分

| 条件 | 分数 | 含义 |
|------|------|------|
| `distance_ma20 ∈ [-4%, +3%]` | +10 | 价格靠近 MA20，回踩位置较合理 |
| `distance_ma20 <= 10%` | +5 | 价格未明显远离 MA20 |
| `pivot_distance ∈ [-8%, 0%]` | +12 | 价格接近 pivot 下方，高质量观察区 |
| `pivot_distance ∈ [-15%, 0%]` | +6 | 距 pivot 尚可，仍可观察 |

### 6.2 structure_risk_score 风险评分

结构风险不折进 `structure_score`，单独输出：

```text
structure_risk_score
structure_risk_flags
```

模型二风险只描述量价结构事实，如过热、距离均线过远、长上影、放量滞涨、假突破、结构过期、突破后延伸等。模型四负责解释这些风险对未持仓和已持仓分别意味着什么。

`structure_risk_score` 由风险标记累加后限制在 `0~100`：

| risk_flag | 阈值 | 风险分 | 含义 |
|-----------|------|--------|------|
| `OVERHEAT_CHG5` | `chg_5 > 20%` | +25 | 5 日涨幅过热 |
| `OVERHEAT_CHG20` | `chg_20 > 50%` | +25 | 20 日涨幅过热 |
| `FAR_ABOVE_MA20` | `distance_ma20 > 15%` | +15 | 明显远离 MA20 |
| `EXTENDED_FROM_MA20` | `distance_ma20 > 10%` | +8 | 偏离 MA20，追高风险上升 |
| `LONG_UPPER_SHADOW` | 上影线占日内振幅 >45% 且量比 >1.5 | +18 | 放量冲高回落 |
| `VOLUME_STALL` | 量比 >1.5 且实体涨幅 <1% | +15 | 放量滞涨 |
| `DOWNTREND` | `MA20 < MA60` 且收盘低于 MA60 | +35 | 趋势破坏 |
| `MA20_DECLINE` | `MA20_slope < -0.10` | +15 | MA20 明显下行 |
| `DEEP_FALL` | 距 60 日高点 < -30% 且 20 日跌幅 < -15% | +35 | 深度下跌，结构风险高 |
| `BELOW_MA120` | `close < MA120` | +20 | 价格低于长线均线，趋势背景存疑 |

硬风险标记：

```text
OVERHEAT_CHG5
OVERHEAT_CHG20
DOWNTREND
DEEP_FALL
```

触发硬风险时，模型二不会把风险直接扣进 `structure_score`，而是通过 `structure_risk_score`、`structure_risk_flags` 和 `action_hint` 告诉模型四：结构事实可能仍存在，但当前不适合追买或需要等待重建。

### 6.3 模型四使用建议

模型四/Bloom 消费这两个分数时，应保持语义分离：

| 组合 | 含义 | Bloom 候选池处理建议 |
|------|------|----------------------|
| 高 `structure_score` + 低 `structure_risk_score` | 结构好且风险低 | 重点观察或等待触发 |
| 高 `structure_score` + 高 `structure_risk_score` | 结构好但位置/量价风险高 | 保留观察，避免追买，等待风险释放 |
| 低 `structure_score` + 低 `structure_risk_score` | 可能还在早期或无明显结构 | 低优先级观察或不进入 |
| 低 `structure_score` + 高 `structure_risk_score` | 结构弱且风险高 | 不进入或移出观察 |

具体阈值和权重的权威来源是 `strategies/02-quant.json`。本节用于解释当前策略口径；修改配置时必须同步更新本节。

---

## 七、输出字段

CSV 和 JSON 至少包含：

```text
股票代码
股票名称

structure_type
structure_stage
setup_signal
action_hint
suggested_position
model2_include

structure_score
structure_risk_score
structure_risk_flags
setup_pattern_score
setup_score
setup_quality
setup_reasons
setup_misses

support_price
invalid_price
breakout_level
contraction_count
contraction_pcts
contraction_days
volume_pattern
pivot_price
structure_pivot
market_pivot
pivot_distance
last_contraction_low
structure_age_days
structure_valid
structure_invalid_reason
post_structure_gain
post_structure_drawdown
vcp_quality
close
MA20 / MA60 / MA120
MA20_slope / MA60_slope
range_10 / range_20 / range_60
volume
vol_ma5 / vol_ma20 / vol_ma60
vol_ratio
volume_dry_up
distance_ma20 / distance_ma60 / distance_high_60
setup_plan_inputs
reason
run_date
strategy_version
```

`setup_plan_inputs` 为模型四 Signal Plan 使用的结构化中间阈值，不参与模型二自身排序和买点判定。模型二必须先按原逻辑完成 `setup_signal` 与 `setup_score` 判定，再把判定过程中已经计算出的阈值透出：

```text
setup_plan_inputs.pullback:
  anchor
  support_price
  invalid_price
  ma20_price_low / ma20_price_high
  ma60_price_low / ma60_price_high
  last_low_required_price
  volume_dry_up_threshold
  volume_floor_threshold
  fixed_window_volume_threshold
  ideal_volume_max
  segment_volume_threshold
  current_low_volume_days
  volume_confirmation

setup_plan_inputs.breakout:
  pivot
  trigger_price
  max_price
  ideal_price_low / ideal_price_high
  volume_min
  volume_ma20_threshold
  volume_ma5_threshold
  ideal_volume_min
  invalid_price

setup_plan_inputs.retest:
  recent_breakout
  recent_breakout_date
  recent_breakout_level
  recent_breakout_volume
  days_after_breakout
  price_low / price_high
  ideal_price_low / ideal_price_high
  volume_threshold
  ideal_volume_max
  confirm_price
  invalid_price
```

这些字段只是把模型二现有买点逻辑的中间事实暴露给模型四；新增或修改这些字段不得改变模型二的 `structure_stage`、`setup_signal`、`setup_score`、`action_hint`。

输出文件命名规则：

| 运行模式 | CSV | JSON |
|----------|-----|------|
| 全量 | `quant/quant_<YYMMDD>.csv` | `cache/quant_runs/quant_<YYMMDD>.json` |
| 测试 | `quant/quant_<YYMMDD>_test.csv` | `cache/quant_runs/quant_<YYMMDD>_test.json` |
| 单股 | `quant/single_<code>_<YYMMDD>.csv` | `cache/quant_runs/<code>_<YYMMDD>.json` |
| 多股 | `quant/multi_<YYMMDD>.csv` | `cache/quant_runs/multi_<YYMMDD>.json` |

只有全量模式允许覆盖 `cache/quant_runs/quant_<YYMMDD>.json`。测试、单股、多股模式不得覆盖全量 JSON，避免 Bloom 消费到测试结果。

单股模式必须打印终端摘要，并同样写入 JSON。

---

## 八、DeepSeek LLM 可选解释

`--with-llm` 仅做解释，不做判定。

脚本从环境变量或本地 `.env` 读取配置：

```text
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

本地 `.env` 不进入 Git。默认模型使用 `deepseek-v4-flash`。

DeepSeek 调用使用 OpenAI 兼容的 Chat Completions 接口，并启用 JSON Output：

```text
POST https://api.deepseek.com/chat/completions
response_format = {"type": "json_object"}
```

LLM 输入是脚本生成的结构化结果；输出必须为 JSON：

```json
{
  "code": "300604",
    "pattern_review": "强趋势偏高，暂未形成标准 VCP",
  "risk_notes": ["距离 MA20 偏远"],
  "watch_points": ["等待缩量回踩 MA20", "观察是否形成收敛区间"],
  "confidence": "medium"
}
```

若 LLM 失败：

```text
主流程继续
CSV/JSON 照常输出
llm_status = failed
```

---

## 九、Bloom 信号层

模型一、模型二完成后，运行 Bloom 信号层，把模型二结果沉淀为候选信号生命周期、估值候选和人类可读 Bloom 报告。

入口：

```bash
python3 scripts/bloom.py
python3 scripts/bloom.py --date 260703
```

输入：

```text
cache/quant_runs/quant_<YYMMDD>.json
bloom/state/bloom_state.csv（如存在，用于状态延续）
bloom/state/bloom_events.jsonl（如存在，用于去重追加）
strategies/04-bloom.json
```

输出：

```text
bloom/bloom_<YYMMDD>.md
bloom/state/bloom_state.csv
bloom/state/bloom_events.jsonl
bloom/state/bloom_input_<YYMMDD>.json
```

职责边界：

| 模块 | 职责 |
|------|------|
| `scripts/bloom.py` | 确定性汇总、状态 diff、事件追加、当前状态表更新、生成 Bloom 结构化输入和 Markdown |
| `strategies/04-bloom.json` | 状态映射、风险阈值、保留天数、输出数量等策略参数 |

Bloom 是模型四内部的信号层，只消费模型二结果：

- 不重新识别 VCP。
- 不改写模型二 `structure_stage` / `setup_signal`。
- 不读取模型三估值。
- 不读取持仓。
- 不输出最终买入、卖出或仓位建议。

状态映射：

| 脚本状态 | bloom 状态 | 含义 |
|----------|------------|------|
| `structure_stage=VCP_EARLY` | `EARLY` | 早期结构，低优先级观察 |
| `structure_stage=VCP_FORMING` | `FORMING` | 结构形成中，正常观察 |
| `structure_stage=VCP_TIGHT` / `VCP_MATURE` | `MATURE` | 结构更完整或更紧致 |
| `setup_signal=PULLBACK_BUY` / `RETEST_BUY` | `TRIGGERED` | 模型二候选触发 |
| 高风险分或硬风险 | `RISK_BLOCKED` | 结构存在，但当前风险阻断 |
| `POST_BREAKOUT` / 临时出局 | `COOLDOWN` | 保留观察，不立即删除 |
| `TREND_REBUILD` / `structure_valid=false` | `INVALID` | 结构失效，等待重建 |
| `DATA_ISSUE` / API 缺失 | `DATA_ISSUE` | 数据不足或接口异常 |
| 连续无效达到规则 | `EXIT` | 移出 Bloom 池 |

信号类型：

```text
NEW_ENTRY
UPGRADE
DOWNGRADE
SETUP_TRIGGER
RISK_BLOCK
COOLDOWN
EXIT
DATA_HOLD
CONTINUED
```

Bloom 详细规则见 `instructions/signal-bloom.md`。

---

## 十、验收标准

- 长川科技这类强基本面但技术偏高的标的，应识别为观察状态或 `AVOID_CHASE`，而不是 `PULLBACK_BUY` / `RETEST_BUY`。
- `PULLBACK_BUY` 必须依赖有效 VCP 结构，不能变成下跌趋势抄底。
- `RETEST_BUY` 必须是突破后的回踩确认，不能变成突破当天追涨。
- 过热、放量滞涨、长上影等风险必须进入 `structure_risk_flags`。
- 脚本可批量运行，也可 `--code` 单股运行。
- 所有核心判断可从 CSV/JSON 中复盘，不依赖对话上下文。

待优化项见 `TODO.md`。历史版本由 Git 追溯，复盘记录见 `dev_logs/`。
