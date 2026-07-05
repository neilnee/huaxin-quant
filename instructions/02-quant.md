# 模型二：量价精筛模型（自执行指令）

- **版本管理**: 由 Git 分支与提交历史管理，文件名不再携带版本号
- **最近更新**: 2026-07-05
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
python3 scripts/quant_filter.py --pool pool/pool_260703.csv
python3 scripts/quant_filter.py --code 300604 --name 长川科技
python3 scripts/quant_filter.py --codes 300604,300442
python3 scripts/quant_filter.py --code 300604 --with-llm
python3 scripts/quant_filter.py --code 300604 --json
```

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
- `PULLBACK_BUY` / `RETEST_BUY` 触发信号参数。
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

### RETEST_BUY：突破后回踩确认

`RETEST_BUY` 是 VCP 突破后的确认买点。

```text
先放量突破箱体上沿/收敛上沿/近 60 日高点
随后 3-10 个交易日内缩量回踩
回踩不有效跌破突破位
重新站回突破位或 MA10/MA20
```

`RETEST_BUY` 买的是确定性。价格通常高于 `PULLBACK_BUY`，但突破已经发生并经过回踩验证。

仓位建议：

```text
加至 60%-80%
```

### 交易触发仓位路径

```text
PULLBACK_BUY 买入 20%-30%
→ 若直接上涨但不给 RETEST_BUY：不追满，只持有已有仓位
→ 若突破后回踩确认：RETEST_BUY 加至 60%-80%
→ 若跌破失效位：减仓或退出
```

---

## 三、数据与缓存

每只股票拉取近 200 个交易日：

```text
date, open, high, low, close, volume, turnover
```

日线数据统一通过 `scripts.shared.fetch_daily()` 获取，链路为：

```text
当天缓存 → 最近可用缓存（需最新 K 线不早于预期交易日）
→ 妙想 API → 通达信 mootdx 备用源
```

日线行情数据源实现位于 `scripts/data/market_data.py`，`scripts.shared.fetch_daily()` 负责统一缓存、主备源降级和返回标准 OHLCV 结构。

妙想 API 是主数据源；当妙想限流、返回空数据、结构异常、异常抛出，或本地未配置 `MX_APIKEY` 时，脚本必须尝试通达信备用源。通达信通过 mootdx 获取日线 `frequency=9`，客户端使用内置 HQ 候选服务器、短超时和失败切换，避免批量运行长时间阻塞。通达信数据同样标准化为上述 OHLCV 结构并写入 `cache/daily/`。通达信不提供换手率，`turnover` 填 `0.0`；模型二判定不得依赖 `turnover`。

缓存目录：

```text
cache/daily/<code>_<YYMMDD>.pkl
```

缓存只保存原始日线，不保存指标列。指标每次实时计算，避免规则变更后旧缓存污染。

缓存命中必须同时满足：

```text
文件名日期 = 当前运行日期；或当天缓存未命中时，为该股票最近可用缓存
缓存内最后一条 K 线日期 >= 当前应有交易日
```

若当天盘中或盘后早期生成的缓存仍停留在前一交易日（例如文件为 `*_260703.pkl`，但最后 K 线是 `2026-07-02`），脚本必须视为过期并重新拉取。已拉到当前交易日的缓存继续复用，避免重复调用接口。

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
| `RETEST_BUY` | 突破后回踩确认买点，确认度高于 PULLBACK_BUY |

`setup_signal` 必须建立在 `structure_stage` 之上。它不是独立形态，而是“结构阶段 + 当日量价触发条件”的结果。

```text
setup_signal = structure_stage + trigger_conditions
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
- 触发条件：缩量回踩 MA20 / MA60 / 收敛下沿，最近收缩低点不破，MA20 斜率未明显走坏，短期不过热，无放量长上影。
- 交易含义：低吸试探，风险收益比优先，确定性低于 RETEST_BUY。
- 模型二量价侧建议：BUY_LIGHT，参考仓位 20%-30%。

RETEST_BUY
- 突破后回踩确认买点。
- 前提形态：近期存在有效 VCP / 箱体突破。
- 触发条件：先放量突破关键位，随后 3-10 个交易日内缩量回踩，回踩不跌破突破位，最新收盘重新站回突破位或 MA10。
- 交易含义：突破已经发生并经回踩确认，确定性高于 PULLBACK_BUY。
- 模型二量价侧建议：BUY_STANDARD，参考仓位 60%-80%。
```

模型二只判断量价触发是否成立；模型三估值是否支持、模型四是否实际给买入建议，需要在模型四中决定。

### 0.4 action_hint：量价侧动作提示

| action_hint | 说明 |
|-------------|------|
| `WATCH` | 只观察，不给买入动作 |
| `BUY_LIGHT` | 缩量回踩触发，量价侧允许轻仓试探 |
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

在最近 80-120 个交易日中识别局部高点和后续局部低点。一轮 contraction 定义为：

```text
从局部高点回撤到后续局部低点
回撤幅度 >= 4%
持续时间 3-45 个交易日
低点后有一定修复，不能是单边下跌未止
```

每轮 contraction 记录：

```text
start_date / end_date
high_price / low_price
pullback_pct
duration_days
avg_volume
recovery_pct
```

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

失效原因：

| invalid_reason | 说明 |
|----------------|------|
| structure_too_old | 最后一轮收缩距当前太久 |
| far_below_structure_pivot | 当前价距离结构 pivot 过远 |
| old_structure_broken_out | 后续市场高点显著超过结构 pivot |
| post_structure_extended | 结构后涨幅过大，旧 VCP 已完成 |
| post_structure_drawdown | 结构后再度深回撤，需要重新形成 |

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

```text
volume_dry_up < 0.80
distance_ma20 在 [-4%, +3%]，或 distance_ma60 在 [-5%, +5%]
close > 最近一轮 contraction low × 1.02
MA20_slope >= -0.03%/日
近 5 日涨幅 < 12%
无放量长阴
```

### RETEST_BUY：突破后回踩确认

突破识别：

```text
breakout_level = 最近 60 日箱体上沿/突破前高
突破日收盘价 > breakout_level × 1.01
突破日成交量 > vol_ma20 × 1.5
突破日无明显长上影
```

回踩确认：

```text
突破后 3-10 个交易日内
回踩低点 >= breakout_level × 0.97
回踩期缩量
最新收盘重新站回 breakout_level 或 MA10
```

---

## 六、评分与风险事实

```text
structure_score = stage_score
                + volume_score
                + trend_score
                + position_score
```

`structure_score` 只评价结构形态质量，分数越高，说明 VCP 越标准、越紧致、量能越健康、趋势越配合、位置越合理。它不直接决定最终买卖，模型四需结合估值、持仓和风险管理使用。

结构风险不折进 `structure_score`，单独输出：

```text
structure_risk_score
structure_risk_flags
```

模型二风险只描述量价结构事实，如过热、距离均线过远、长上影、放量滞涨、假突破、结构过期、突破后延伸等。模型四负责解释这些风险对未持仓和已持仓分别意味着什么。

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
volume_dry_up
distance_ma20 / distance_ma60 / distance_high_60
reason
run_date
strategy_version
```

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

## 九、每日花期复盘层

模型一、模型二完成后，运行每日复盘层，把脚本结果沉淀为 Huaxin Quant 的花期观察数据层和人类可读复盘。

入口：

```bash
python3 scripts/daily_review.py
python3 scripts/daily_review.py --date 260703
python3 scripts/daily_review.py --date 260703 --with-llm
```

输入：

```text
pool/pool_<YYMMDD>.csv
quant/quant_<YYMMDD>.csv
cache/quant_runs/quant_<YYMMDD>.json
bloom/bloom_state.csv（如存在，用于状态延续）
bloom/bloom_events.jsonl（如存在，用于去重追加）
```

输出：

```text
bloom/bloom_events.jsonl
bloom/bloom_state.csv
cache/reviews/review_input_<YYMMDD>.json
reports/daily/review_<YYMMDD>.md
```

职责边界：

| 模块 | 职责 |
|------|------|
| `daily_review.py` | 确定性汇总、状态 diff、事件追加、当前状态表更新、生成 LLM 输入包和基础 Markdown |
| LLM | 可选解释、复盘措辞、重点样本点评，不改变模型一/二判定 |

`bloom/` 是模型二之后、模型三/四之前的花期观察数据层：

- `bloom_events.jsonl`：追加式事件流水。重复跑同一天时先删除同日事件再重写，保持幂等。
- `bloom_state.csv`：当前观察状态表。每天全量模式运行后覆盖更新。

状态映射：

| 脚本状态 | bloom 状态 | 含义 |
|----------|------------|------|
| `structure_stage=VCP_EARLY` | `early` | 早期花蕾，刚出现收缩过程 |
| `structure_stage=VCP_FORMING` | `forming` | 花期形成中，重点观察 |
| `structure_stage=VCP_TIGHT` / `VCP_MATURE` | `mature` | 结构更完整或更紧致 |
| `setup_signal=RETEST_BUY` | `retest` | 突破后回踩确认 |
| `POST_BREAKOUT` | `breakout` | 历史结构已走完，不再算当前形成期 |
| `TREND_REBUILD` | `invalid` | 历史结构失效，等待重建 |
| `DATA_ISSUE` / API 缺失 | `data_issue` | 数据不足或接口异常 |
| `action_hint=REJECT` | `rejected` | 当前不进入观察 |

事件类型：

```text
new_entry   昨日不存在/非观察 → 今日 early/forming/mature/retest
upgrade     观察状态升级，例如 early → forming
downgrade   观察状态降级，例如 forming → early
invalidated 观察状态 → rejected/invalid/breakout/data_issue
continued   观察状态延续
data_issue  今日数据不足或缺失
```

LLM 解释层不得：

- 推翻脚本的 `structure_type` / `structure_stage` / `setup_signal`
- 自造价格、成交量、财务数据
- 把 `REJECT` / `DATA_SKIP` 改成观察或买点
- 替代模型三估值或模型四交易信号

---

## 十、验收标准

- 长川科技这类强基本面但技术偏高的标的，应识别为观察状态或 `AVOID_CHASE`，而不是 `PULLBACK_BUY` / `RETEST_BUY`。
- `PULLBACK_BUY` 必须依赖有效 VCP 结构，不能变成下跌趋势抄底。
- `RETEST_BUY` 必须是突破后的回踩确认，不能变成突破当天追涨。
- 过热、放量滞涨、长上影等风险必须进入 `structure_risk_flags`。
- 脚本可批量运行，也可 `--code` 单股运行。
- 所有核心判断可从 CSV/JSON 中复盘，不依赖对话上下文。

待优化项见 `TODO.md`。历史版本由 Git 追溯，复盘记录见 `dev_logs/`。
