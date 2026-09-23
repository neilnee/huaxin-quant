# Huaxin Quant 策略阅读指南

最近核对：2026-09-08。本文件解释策略使用顺序与边界，不再复制各模块的阈值、公式和评分表。当前规则以对应指令卡与策略 JSON 为准；旧版本内容由 Git 追溯。

## 1. 从候选到观察

Pool 采用核心质量池和 RS 扩展池双通道。核心池使用财务门槛和软标签，行业不再整体排除；扩展池扩大强势股票覆盖，不宣称基本面核验通过。重合标记 BOTH。

候选池资格不等于买入资格；核心池部分关键字段缺失仍可能按既有规则保留。详见 [Pool](01-pool.md)。

## 2. 结构与动作分别判断

Quant 识别当前 VCP 组，区分形成阶段、收缩确认、突破后上下文与风险，再判定 PULLBACK_BUY、BREAKOUT_BUY、RETEST_BUY。结构分、买点分和买点等级不能互相替代；当前新结构与前序突破结构各自使用其时间锚点。

收缩轮次、量能、突破质量、RETEST 窗口及风险硬门槛只在 [Quant](02-quant.md) 和 strategies/02-quant.json 维护。量能冲突等尚未实现的改进见 [路线图](../docs/IMPROVEMENT_ROADMAP.md)，不能提前应用到正式结果。

## 3. 跨日状态与次日计划

[Bloom](signal-bloom.md) 维护结构生命周期，不读取实际持仓。退出观察池不等于卖出真实持仓。

[Signal Plan](signal-plan.md) 把模型二已有阈值转为下一交易日的条件计划。target_quality 是潜在质量，当日实际质量只认模型二 setup_quality。前日 Plan 兑现只作审计，不补造当天买点。

## 4. 环境与风险提示

[Market Regime](market-regime.md) 提供同日市场确认状态与板块阶段，候选状态不提前替代确认状态。[Capital Observer](capital-observer.md) 和 [Signal Fundamentals](signal-fundamentals.md) 提供独立证据，不更改模型二信号或评分。

Dashboard 当前按实际 A/B 等级及市场、板块条件折算量价仓位提示；C/D 为观察。该适配器不读取账户资金、实际持仓或估值，也不按失效距离计算账户风险预算。模型二原始 suggested_position 与页面环境折算不是同一字段。

现有百分比提示尚未形成完整账户分母和组合约束契约，不能解释为已经通过账户风险核验的配置结果。具体百分比表只在 Signal Plan 指令及配置维护，本文件不另造仓位规则。

## 5. 估值与持仓独立运行

[Valuation](03-valuation.md) 当前是主动触发的机构共识研究。机构利润和目标估值来自同份研报组合；公司业务地图用于解释，不强制拆分公司整体利润。三情景是机构估值组合的区间，不是经过概率校准的收益承诺。Bloom 候选标记不自动触发估值。

[Position](signal-position.md) 记录用户确认的真实交易，重建批次和状态。止盈止损、加减仓、行业集中度和组合风险监控尚待独立实现。信号及研究观点不得写成真实成交。

## 6. 如何解释回测

[Backtest](backtest.md) 当前并列观察 VCP 首次入选与 Plan 次日兑现。后者的 BUY_POINT、A/REGULAR 不等于页面的模型二 setup_signal、A/B/C/D。

5/10/20 日总回报和买点后 R 路径是事件研究，不是账户净值。未模拟真实成交、仓位、费用、滑点与退出规则；不同策略版本及非点时回填样本也不能未经分组就证明当前版本有效。

后续先建立独立模型二信号集合、基准超额收益和样本外跟踪，再评价评分和环境过滤的增量。评估与修改顺序见 [TODO](../TODO.md)。

## 7. 阅读入口

| 任务 | 文档 |
|---|---|
| 理解工程依赖和权威数据 | [DESIGN](../DESIGN.md) |
| 运行、恢复与发布 | [WORKFLOW](../WORKFLOW.md) |
| 模块导航与文档维护 | [文档索引](../docs/README.md) |
| 优先级与验收标准 | [改进路线图](../docs/IMPROVEMENT_ROADMAP.md) |
