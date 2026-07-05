# Huaxin Quant Design

最近更新：2026-07-05

本文记录 Huaxin Quant 当前的模型边界、分层原则和策略配置化方向。指令卡仍是每个模型的执行口径来源；本文用于统一架构理解，避免后续重构时把数据、策略、状态和交易动作混在一起。

## 1. 总体目标

Huaxin Quant 是多模型流水线的股票发现与跟踪系统：

```text
模型一 Pool       → 基础股票池初筛
模型二 Quant      → VCP 量价结构扫描
模型三 Valuation  → 估值锚点与安全边际
模型四 Tracker     → 信号生命周期、持仓管理和交易建议
```

设计原则：

- 模型一负责"哪些股票值得进入量价扫描"。
- 模型二负责"当天量价结构事实、触发信号和结构评分"。
- 模型三负责"估值锚点和安全边际"。
- 模型四 Tracker 负责"信号生命周期跟踪、持仓管理和交易建议"。
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
  04-bloom.json
```

本地运行目录的 `strategies/` 是指向云盘源码仓库的软链，便于运行和版本管理使用同一份配置。

统一加载器：

```text
scripts/strategy_config.py
```

策略配置只承载"可调参数"：

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

CSV 包含 `strategy_version`。

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

核心结构阶段：

| 阶段 | 含义 |
|------|------|
| `VCP_EARLY` | 早期收缩，1 轮以上 |
| `VCP_FORMING` | 形成中，2 轮以上 near 递减 |
| `VCP_MATURE` | 成熟，3 轮以上 strict 递减 |
| `VCP_TIGHT` | 紧致，MATURE + 最后一轮 < 10% + 量能干燥 |
| `TREND_WATCH` | 强趋势但无收缩轮次 |
| `POST_BREAKOUT` | 结构后涨幅过大（>25%） |
| `TREND_REBUILD` | 结构后深度回撤，需重建 |

触发信号：

| 信号 | 条件 |
|------|------|
| `PULLBACK_BUY` | VCP 结构内缩量回踩 MA20/MA60，前低不破 |
| `RETEST_BUY` | 有效 VCP（MATURE/TIGHT）+ 放量突破 + 3-10天缩量回踩不破位 + 站回突破位/MA10 |

RETEST_BUY 的关键约束：`structure_valid=true`、stage 为 VCP_MATURE/VCP_TIGHT、突破日非长上影、硬风险不阻断。`old_structure_broken_out` 已移除 —— 突破不再因幅度被踢出 VCP 结构。

模型二输出：

```text
quant/quant_<YYMMDD>.csv
cache/quant_runs/quant_<YYMMDD>.json
```

核心输出字段：`structure_type`, `structure_stage`, `setup_signal`, `action_hint`, `suggested_position`, `model2_include`, `structure_score`, `structure_risk_score`, `structure_risk_flags`, `support_price`, `invalid_price`, `breakout_level`, `strategy_version`

模型二不维护跨日状态，不决定最终买卖，不判断估值安全边际，不管理持仓。

## 6. 模型三：Valuation

模型三目标：形成估值锚点和安全边际。相对独立，核心输出供模型四使用。

## 7. 模型四：Tracker

模型四总控名为 Tracker，由多个独立信号模块组成：

```text
Tracker 总控
├── Bloom 信号层     ← 当前已实现
├── 估值触发层        ← 后续
└── 持仓管理层        ← 后续
```

所有模型四内部信号模块指令卡使用 `signal-` 前缀，统一放在 `instructions/` 下。

### 7.1 Bloom 信号层

Bloom 消费模型二 JSON，把单日横截面发现转为跨日信号生命周期。只做信号判断，不读模型三估值，不读持仓，不输出最终交易动作。

**入口：**

```bash
python3 scripts/bloom.py
python3 scripts/bloom.py --date 260705
```

**输入：**

```text
cache/quant_runs/quant_<YYMMDD>.json     ← 权威输入（仅全量模式）
bloom/state/bloom_state.csv              ← 状态延续
bloom/state/bloom_events.jsonl           ← 事件续写、幂等重跑
strategies/04-bloom.json                 ← 策略参数
```

**输出：**

```text
bloom/bloom_<YYMMDD>.md                  Bloom 日报（4 板块）
bloom/state/bloom_state.csv              机器状态表
bloom/state/bloom_events.jsonl           事件流水（幂等：同日重跑先删再写）
bloom/state/bloom_input_<YYMMDD>.json    结构化输入（下游消费）
```

**Bloom 状态（大写枚举）：**

| bloom_status | 含义 |
|-------------|------|
| `EARLY` | 早期结构，低优先级观察 |
| `FORMING` | 结构形成中 |
| `MATURE` | 结构成熟或紧致，重点观察 |
| `TRIGGERED` | 模型二出现 PULLBACK_BUY / RETEST_BUY |
| `RISK_BLOCKED` | 结构存在但风险过高（HIGH/HARD） |
| `COOLDOWN` | 临时出局，保留观察 |
| `INVALID` | 结构失效或等待重建 |
| `EXIT` | 移出 Bloom 池 |
| `DATA_ISSUE` | 数据异常，不改变长期判断 |

**池子决策：** `ADD` / `KEEP_FOCUS`（FORMING 及以上）/ `KEEP_LOW`（EARLY）/ `COOLDOWN` / `EXIT` / `DATA_HOLD`

**信号类型：** `NEW_ENTRY` / `UPGRADE` / `DOWNGRADE` / `SETUP_TRIGGER` / `RISK_BLOCK` / `COOLDOWN` / `EXIT` / `DATA_HOLD` / `CONTINUED`

**日报结构：** 📊 今日概要 → 🔥 重点观察（7 列表格）→ 📋 池子变化（8 列网格）→ 📖 字段说明

### 7.2 估值触发层（后续）

消费 Bloom 输出，判断哪些股票值得进入模型三估值流程。

### 7.3 持仓管理层（后续）

消费持仓数据、Bloom 信号和模型三估值，输出仓位管理建议。

## 8. 目录约定

源码仓库：`instructions/` `scripts/` `strategies/` `DESIGN.md` `README.md` `WORKFLOW.md`

运行目录：`cache/` `pool/` `quant/` `bloom/` `signals/` `reports/` `tmp/`

运行产物不提交 Git。

## 9. 后续演进

1. ~~稳定模型一、模型二策略配置口径~~ ✅
2. ~~重构 Bloom 信号层（bloom.py + signal-bloom.md + 04-bloom.json）~~ ✅
3. 用配置文件持续调参，不修改脚本逻辑
4. 后续新增估值触发层
5. 后续新增持仓管理层
