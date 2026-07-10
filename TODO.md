# TODO

---

## 模型四：Tracker 完善

Bloom 信号层已完成（发现侧），下阶段补齐持仓管理和估值触发，形成完整的"发现 → 跟踪 → 估值 → 交易"闭环。

### P0 — 持仓管理模块

持仓管理同时覆盖个股和 ETF，两者策略独立、同在一个模块内统一调度。

**个股持仓：**
- [ ] 消费 Bloom 信号 + 模型三估值结果 + 实际持仓成本
- [ ] 止损/止盈/加仓/减仓 建议，基于估值安全边际 + 量价结构变化
- [ ] 持仓股票无论模型二是否覆盖，都独立管理；优先保护已有仓位风险
- [ ] 分批建仓跟踪：批次成本、止损线上移、仓位占比

**ETF 持仓：**
- [ ] ETF 独立策略，不依赖 Bloom 信号（ETF 无 VCP 形态概念）
- [ ] 消费：板块热度 + 指数估值分位 + 折溢价 + 持仓成本
- [ ] 定投/网格/趋势 等多策略支持，初始先落地一种
- [ ] ETF 与个股共享统一的持仓状态表结构

**产出：**
- [ ] **instructions/signal-position.md**：持仓管理指令卡（个股 + ETF 双轨）
- [ ] **scripts/position.py**：持仓管理脚本，统一调度个股和 ETF
- [ ] **strategies/04-position.json**：持仓管理策略配置

### P0 — 策略验证

模型四是每日持续跟踪，从跟踪结果反向验证策略有效性。

- [ ] **信号胜率统计**：Bloom 信号出现后 N 日走势 vs 基准
  - TRIGGERED / MATURE / FORMING 各自统计
  - 不管是否实际买入，纯策略角度回看
- [ ] **持仓表现跟踪**：实际持仓的盈亏、最大回撤、持仓天数
- [ ] **定期复盘报告**：周/月维度汇总策略表现，指导参数调整

### P0 — 估值触发层

- [ ] **instructions/signal-valuation-queue.md**：估值触发指令卡
  - 消费 Bloom 输出（valuation_candidate + valuation_priority）
  - 判断哪些股票值得进入模型三估值流程
  - 已有估值是否需要复核（过期、价格偏离过大等）
- [ ] 估值触发逻辑集成到 Tracker 主流程

### P1 — Tracker 总控

- [ ] **scripts/tracker.py 重构**：统一调度 Bloom → 估值触发 → 持仓管理
- [ ] 总控指令卡 `instructions/04-tracker.md` 对齐三模块完整流程

---

## 模型一：板块热度跟踪

在模型一新增板块热度子模块，辅助模型二量价筛选，同时服务于 ETF 持仓管理。

- [ ] **板块分类体系**：东方财富行业/概念板块 + 自定义赛道分组（半导体链、AI算力、新能源等）
- [ ] **热度指标**：
  - 板块内上涨家数/下跌家数比
  - 板块成交额变化（放量/缩量）
  - 板块指数 vs 大盘相对强度
  - 板块内涨停家数、连板高度
- [ ] **热度分层**：冰点 / 冷清 / 正常 / 活跃 / 过热
- [ ] **输出**：`pool/sector_heat_<YYMMDD>.csv`，供模型二参考（如在热点板块中的 VCP 加分）和 ETF 决策
- [ ] **instructions/01-pool.md** 追加板块热度章节

---

## 模型二增强：买点预判模块

在模型二 VCP 结构识别基础上，新增**次日买点预判**能力。不改变模型二现有的结构/买点判断逻辑，而是向前推一步：对于已经成熟的 VCP 结构，提前算清楚"明天量价走到哪里会触发哪种买点"。

### P0 — RETEST 放量阴线硬阻断

- [ ] RETEST_BUY 不得仅以突破后 5 日平均量判断缩量；当日出现显著下跌且成交量相对前 5 日均量放大、接近突破日量能时，标记 `FAILED_RETEST_SELLING` 并硬阻断买点。
- [ ] 将“当前收缩段量能 failed”与 RETEST 买点评分/阻断规则联动，避免供给持续扩张的结构获得 A 级买点。

### 核心逻辑

```
输入：模型二 quant JSON（结构阶段、pivot、MA20/MA60、当前收盘、量能状态、收缩段数据）
输出：每只 VCP_MATURE / VCP_TIGHT 标的的次日触发条件清单

for each stock in quant_output where structure_stage in {VCP_MATURE, VCP_TIGHT}:
    计算次日触发三种买点各自需要的量价区间：
      PULLBACK_BUY 触发区间：回踩 MA20/MA60/收敛下沿的位置 + 缩量要求
      BREAKOUT_BUY 触发区间：站上 pivot × 1.01 的价格 + 量能放大要求
      RETEST_BUY 触发区间：先突破再回踩（两段式，次日可能出现的第一段）
    对每种潜在买点预计算预期评分区间
    标注最近的失效位（止损参考）
```

### 买点预判计算规则

**PULLBACK_BUY 预判：**
- [ ] 预判价格区间：`[max(MA20, 收敛下沿) × 0.98, MA20 × 1.02]`
- [ ] 预判量能条件：缩量至 `vol_ma20 × 0.80` 以下，或收缩段低量区
- [ ] 预判评分：基于当前结构阶段 + 回踩位置偏移量 + 缩量程度，给出 A/B/C 级区间
- [ ] 失效位：最近一轮收缩低点

**BREAKOUT_BUY 预判：**
- [ ] 预判价格区间：`[pivot × 1.01, pivot × 1.08]`，细分最佳突破区 `[pivot × 1.02, pivot × 1.05]`
- [ ] 预判量能条件：`volume > vol_ma20 × 1.0`，最佳 `> 1.5×`
- [ ] 预判 K 线条件：收盘接近日高、实体强、无明显上影
- [ ] 预判评分：基于突破幅度 + 量能倍数 + K 线质量，给出 A/B/C 级区间
- [ ] 失效位：pivot 下方或最近收缩低点

**RETEST_BUY 预判（两段式）：**
- [ ] 先判断当前是否已经突破（close > pivot × 1.01）
- [ ] 若已突破：预判回踩位 `[pivot × 0.99, pivot × 1.005]`，回踩缩量条件
- [ ] 若未突破：标注"需先出现 BREAKOUT，再等待回踩"，不生成 RETEST 预判
- [ ] 预判评分：基于回踩位置精度 + 缩量程度 + 重新确认强度
- [ ] 失效位：突破日低点或 pivot 下方

### 产出

- [ ] **instructions/02-quant-prep.md**：买点预判指令卡
- [ ] **scripts/quant_prep.py**：买点预判脚本，消费 `quant/quant_<YYMMDD>.json`，产出预判清单
- [ ] **strategies/02-quant-prep.json**：预判阈值与评分参数配置
- [ ] **输出文件**：`quant/prep_<YYMMDD>.json` 和 `quant/prep_<YYMMDD>.md`（人类可读的次日交易计划）

### 与现有模块的关系

- 买点预判**不替代**模型二和 Bloom 的每日买点判断，而是**辅助次日盘前准备**
- 次日盘后：模型二正常跑出当日实际买点 → 与前一日的预判对比 → 命中/未命中/超预期，记录到回测模块
- Bloom 的 `next_watch_point` 字段当前是自然语言，预判模块上线后可改为结构化触发条件引用

---

## 策略回测模块

以模型二每日 `--date` 回放能力为基础，构建**历史信号回测系统**。核心目标：量化买点评分体系的实际预测能力，用数据驱动策略参数迭代。

### 回测维度

**信号胜率统计（按买点类型）：**
- [ ] PULLBACK_BUY 出现后 N 日（5/10/20/40 日）涨跌幅分布
- [ ] BREAKOUT_BUY 出现后 N 日涨跌幅分布
- [ ] RETEST_BUY 出现后 N 日涨跌幅分布
- [ ] 各买点 A/B/C 质量分级后的胜率分化（验证评分体系是否有效）
- [ ] 对比基准：同期中证 500 / 沪深 300 涨跌幅

**结构阶段预测能力：**
- [ ] VCP_MATURE 出现后，N 日内实际触发买点的比例
- [ ] VCP_TIGHT 出现后，N 日内实际触发买点的比例
- [ ] 结构分与后续买点触发概率的相关性

**风险标识有效性：**
- [ ] 有风险标识的买点 vs 无风险标识的买点，后续胜率差异
- [ ] 各类风险标识（EXTENDED_FROM_MA20 / LONG_UPPER_SHADOW 等）的独立预测能力

**Bloom 信号生命周期：**
- [ ] TRIGGERED 信号 N 日后状态分布（仍有效 / COOLDOWN / EXIT）
- [ ] MATURE → TRIGGERED 的平均等待天数
- [ ] COOLDOWN 后重新激活 vs 最终退出的比例

### 技术路线

- [ ] **回放范围**：从 `quant/` 和 `bloom/state/` 最早可用日期起，逐日回放至最近交易日
- [ ] **数据来源**：优先复用缓存中的历史日线数据；缺失部分用 `--date` 回放补全
- [ ] **统计输出**：`backtest/signal_performance_<YYYYMMDD>.csv`（每次运行覆盖更新）
- [ ] **可视化**：买点胜率曲线（按类型/等级的 N 日收益分布箱线图）、信号生命周期桑基图

### 产出

- [ ] **instructions/backtest.md**：回测指令卡
- [ ] **scripts/backtest.py**：回测主脚本，调度历史回放 + 统计计算
- [ ] **strategies/backtest.json**：回测参数（回看天数、N 日窗口、基准指数）
- [ ] **backtest/ 目录**：回测结果（CSV + 图表 + 简报 Markdown）

### 与策略验证的关系

模型四"策略验证"侧重**前向跟踪**（从今天出发，持续观察未来表现），回测模块侧重**后向回测**（回放历史，量化已有信号的实际表现）。两者互补：

| 维度 | 策略验证（前向） | 回测模块（后向） |
|------|-----------------|-----------------|
| 数据来源 | 当日及未来每日运行 | 历史 quant/ + bloom/state/ |
| 时效 | 随交易日累积 | 一次性回放全部历史 |
| 用途 | 跟踪当前策略表现、早期预警 | 验证评分体系、校准参数阈值 |
| 更新频率 | 每日追加 | 按需运行（参数调整后重跑）

---

## 模型三：Valuation 重构

### P0 — LLM 脚本化

- [ ] **valuate.py 阶段一~五脚本化**：共识研报提取、可比公司选择、预期差搜索在 Python 脚本里标准化执行（调 LLM API + 搜索接口），用户只需审阅最终输出，不再手工 orchestrate 每一步
- [ ] **consensus 字段升级**：接入真实分析师一致预期数据源，减少 `auto_generated` 占比
- [ ] **脚本共识质量验证**：补充交叉验证逻辑

### P1 — 参数优化

- [ ] **决策树阈值校准**：Q2（固产/总资产 40%/20%）、Q3（毛利率波动 5pct）分界线基于实际案例回测调整
- [ ] **路线 E/F 标的 Q2 跟踪**：Q2 季报发布后重跑决策树

---

## 自选股同步模块重构

### P0

- [ ] **scripts/sync_zixuan.py 重构**：根据模型四产出维护自选股
  - 入池条件：Bloom KEEP_FOCUS + 模型三估值通过（如有）
  - 出池条件：Bloom EXIT 或连续 COOLDOWN 超期
  - 操作仅对东方财富自选股"全部"分组生效
- [ ] **instructions/sync-zixuan.md**：自选股同步指令卡

---

## 已完成

- [x] 模型二当前 VCP 组选择优化（明显扩张重置旧收敛结构）
- [x] 模型二 VCP 买点优化（PULLBACK 段间缩量确认 + BREAKOUT_BUY）
- [x] Bloom 信号层重构（bloom.py + signal-bloom.md + 04-bloom.json）
- [x] Bloom 日报分层重构（4 板块清晰结构）
- [x] RETEST_BUY 逻辑修复（移除 old_structure_broken_out）
- [x] 数据层统一抽象（DataSource + fetch_daily + TDX 降级）
- [x] 策略配置化（strategies/ + strategy_config.py）
- [x] DESIGN.md + WORKFLOW.md 设计文档
- [x] v1.2 发布
- [x] 数据口径统一（周期感知阈值）
- [x] shared.py 共享模块
- [x] 指令卡 Git 管理（固定文件名）
- [x] v1.3 发布
- [x] v1.4 / v1.4.1 发布（Bloom 信号层稳定）
- [x] 模型二买点评分体系（PULLBACK + RETEST 双买点 + 质量分）
- [x] 模型二四段式买点评分重构（stage_base + action_type_base + action_quality_score + risk_adjust）
- [x] BREAKOUT_BUY 完整体系（硬条件 + 动作质量评分 + K线确认）
- [x] 模型二当前 VCP 组收缩扩张重置逻辑优化
- [x] Bloom 信号层 v2（BREAKOUT_BUY 补全 + 状态字段扩展 + LLM 逐只请求）
- [x] 重点观察智能过滤（结构分门槛）与排序规则（买点优先 + 结构强弱）
- [x] 交易策略参考文档（instructions/trading-strategy.md）
- [x] 日线缓存 30 天保留 + --date 回测参数
- [x] 模型四 Signal Plan v1（次日量价触发计划：signal-plan.md + signal_plan.py + 04-signal-plan.json）
- [x] 模型四 Tracker 总控 v1（编排 Bloom + Signal Plan → 合并日报；清理旧 tracker.py/generate_report.py/signals/）
- [x] v1.5 发布
