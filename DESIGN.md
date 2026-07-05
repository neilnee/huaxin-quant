# Huaxin Quant Design

最近更新：2026-07-05

本文记录 Huaxin Quant 当前的模型边界、分层原则和策略配置化方向。指令卡仍是每个模型的执行口径来源；本文用于统一架构理解，避免后续重构时把数据、策略、状态和交易动作混在一起。

## 1. 总体目标

Huaxin Quant 是多模型流水线的股票发现与跟踪系统：

```text
模型一 Pool       → 基础股票池初筛
模型二 Quant      → VCP 量价结构扫描
模型三 Valuation  → 估值锚点与安全边际
模型四 Bloom      → 生命周期跟踪、持仓管理和交易建议
```

设计原则：

- 模型一负责“哪些股票值得进入量价扫描”。
- 模型二负责“当天量价结构事实、触发信号和结构评分”。
- 模型三负责“估值锚点和安全边际”。
- 模型四 / Bloom 负责“候选池、持仓池、观察期、交易建议和风控动作”。
- 每个模型只输出自己职责范围内的事实或判断，不越界替其他模型决策。

## 2. 分层原则

模型一、模型二、模型四都按三层理解：

```text
共享数据层 data
  读取、缓存、数据源降级、字段标准化。

策略层 strategy
  阈值、权重、分层标准、状态映射、保留期、风险标记。

实现逻辑层 runner/app
  CLI、流程编排、文件读写、输出报告。
```

共享数据层可以跨模型复用；策略层由各模型自己管理，避免不同模型的规则混成一团。

已落地的共享数据能力：

- `scripts/data/market_data.py`
  - `DataSource`
  - `MiaoxiangSource`
  - `TDXSource`
- `scripts/data/pool_data.py`
  - `PoolSegmentCache`
  - `XuanguSource`
  - xuangu raw JSON 读取、缓存匹配、字段解析和合并去重
- `scripts/shared.py`
  - `DailyCache`
  - `fetch_daily()`
  - `RateLimiter`
  - 项目路径、报告期推断、估值索引路径等公共能力

模型一通过 `pool_data.py` 共享股票池 raw 数据接入能力；模型二和模型四共享 `fetch_daily()` 与日线缓存。

## 3. 策略配置化

策略参数放在独立目录：

```text
strategies/
  01-pool.json
  02-quant.json
  04-bloom.json   # 规划中
```

本地运行目录的 `strategies/` 是指向云盘源码仓库的软链，便于运行和版本管理使用同一份配置。

统一加载器：

```text
scripts/strategy_config.py
```

策略配置只承载“可调参数”：

- 阈值
- 权重
- 分层标准
- 状态映射
- 风险分数
- 保留期天数
- 行业排除列表
- 查询模板和输出字段模板

策略配置不承载算法：

- 数据源调用流程
- 指标计算公式
- swing high / swing low 识别算法
- VCP 收缩轮次扫描流程
- 文件读写和 CLI 编排
- 复杂状态机执行代码

每个模型输出都应写入 `strategy_version`。JSON 类输出还应写入 `strategy_file`。这样历史结果可以追溯生成口径。

## 4. 模型一：Pool

模型一目标：从全市场筛出基本面候选池，供模型二扫描。

入口：

```bash
python3 scripts/run_pool.py
python3 scripts/process_pool.py
```

策略配置：

```text
strategies/01-pool.json
strategy_version = model1_pool_v1
```

已配置化内容：

- xuangu 查询过滤词。
- xuangu 输出字段模板。
- 市值分段。
- 缓存有效天数。
- 分段调用间隔。
- 截断阈值和 PE 正序/倒序补充查询。
- 市值、上市天数、净利润、OCF/NP、负债率、毛利率等硬过滤阈值。
- 半导体高增长豁免阈值。
- 行业排除关键词。
- 软标签触发阈值和扣分。

模型一输出：

```text
pool/pool_<YYMMDD>.csv
```

CSV 增加：

```text
strategy_version
```

模型一不负责量价结构、不负责估值、不负责交易建议。

## 5. 模型二：Quant

模型二目标：对模型一候选池做每日 VCP 结构扫描，输出当天结构事实和触发信号。

入口：

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
python3 scripts/quant_filter.py --code 300604 --name 长川科技
```

策略配置：

```text
strategies/02-quant.json
strategy_version = model2_quant_v1
```

已配置化内容：

- VCP 观察窗口、摆动点窗口、回撤幅度和天数。
- 结构有效期、pivot 距离、突破后延伸/回撤失效阈值。
- 收缩递减比例、量能递减/缩量/失败阈值。
- `VCP_EARLY / VCP_FORMING / VCP_MATURE / VCP_TIGHT` 阶段参数。
- `PULLBACK_BUY / RETEST_BUY` 触发信号参数。
- 结构评分、量能评分、趋势评分、位置评分。
- 风险标记阈值和风险分数。
- 模型二建议仓位文本。

模型二输出：

```text
quant/quant_<YYMMDD>.csv
cache/quant_runs/quant_<YYMMDD>.json
```

核心输出字段：

```text
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
strategy_version
```

JSON `meta` 写入：

```text
schema
strategy_version
strategy_file
```

模型二不维护跨日状态，不决定最终买卖，不判断估值安全边际，不管理持仓。

模型二评分分为 `structure_score` 和 `structure_risk_score` 两条线：前者描述结构质量，后者描述当前量价风险。权威参数见 `strategies/02-quant.json`，解释性评分表见 `instructions/02-quant.md` 的“评分与风险事实”章节。

## 6. 模型三：Valuation

模型三目标：形成估值锚点和安全边际。

模型三相对独立，核心输出供模型四使用：

- 保守 / 基准 / 乐观估值。
- 估值假设。
- 安全边际。
- 估值研究状态。

模型二的触发信号是否能变成真实交易建议，需要模型四结合模型三安全边际决定。

## 7. 模型四：Bloom

模型四别名定为 Bloom。

Bloom 不是模型二后面的独立中间层，而是完整的生命周期跟踪系统：

```text
候选池同步
→ 信号跟踪
→ 持仓管理
→ 渲染输出
```

### 7.1 候选池同步

来源：

- 模型二发现的 `model2_include=True` 标的。
- 当前持仓标的。
- 人工维护或自选股来源。

职责：

- 新股票进入观察池。
- 已在观察池的股票更新状态。
- 模型二临时出局后不立刻删除，而是进入观察保留期。
- 连续多日无结构、结构失效或风险过高后换出。
- 持仓股票即使模型二不覆盖，也必须进入持仓管理分支。

### 7.2 信号跟踪

Bloom 消费模型二字段：

```text
structure_type
structure_stage
setup_signal
action_hint
structure_score
structure_risk_score
structure_risk_flags
support_price
invalid_price
breakout_level
```

Bloom 再结合：

- 模型三估值安全边际。
- 是否持仓。
- 当前仓位。
- 批次止损线。
- 历史观察状态。

生成最终交易建议。

### 7.3 持仓管理

持仓股票和模型二候选股票是两个数据来源。

- 模型二覆盖持仓股票：Bloom 需要结合模型二信号，但不能简单套用候选股逻辑。
- 模型二未覆盖持仓股票：Bloom 仍要基于持仓策略、成本、止损、估值和趋势管理。
- 持仓管理优先保护已有仓位风险，不以模型二是否入选作为唯一依据。

### 7.4 现有 bloom 层

当前 `bloom/` 和 `scripts/daily_review.py` 已经实现部分跨日观察状态：

- `bloom/bloom_state.csv`
- `bloom/bloom_events.jsonl`

它们应被理解为 Bloom 的早期状态机实现和历史数据来源，后续应逐步并入模型四，而不是作为模型二和模型四之间的独立层。

## 8. 目录约定

源码仓库维护：

```text
instructions/
scripts/
strategies/
DESIGN.md
README.md
WORKFLOW.md
```

运行目录维护：

```text
cache/
pool/
quant/
bloom/
signals/
reports/
tmp/
```

运行产物不提交 Git，除非明确需要。

## 9. 后续演进

建议顺序：

1. 稳定模型一、模型二策略配置口径。
2. 用配置文件调参，而不是改脚本逻辑。
3. 梳理模型四 Bloom 的候选池同步、观察保留期和换出规则。
4. 新增 `strategies/04-bloom.json`。
5. 将 `daily_review.py` 中有价值的状态机能力迁入 Bloom。
6. 按模块拆分模型四：数据读取、候选池策略、持仓策略、信号渲染。

当前阶段的重点是：把数据层共享、策略层配置化、模型边界固定下来，再逐步重构脚本实现。
