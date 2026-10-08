# 文档索引与维护约定

最近核对：2026-09-08。

## 按任务阅读

| 任务 | 入口 |
|---|---|
| 项目介绍与独立安装 | [README](../README.md)、[English](../README.en.md) |
| 架构、模块依赖和权威数据 | [DESIGN](../DESIGN.md) |
| 每日执行、估值恢复、异常处理 | [WORKFLOW](../WORKFLOW.md) |
| Agent 工程、投研与数据使用规范 | [共用规范 AGENTS](../AGENTS.md)、[Claude 入口](../CLAUDE.md) |
| 盘后人工机会初筛 | [机会初筛指令](../instructions/opportunity-screening.md)、[字段与比较参考](../instructions/opportunity-screening-ref.md) |
| 策略理解 | [策略阅读指南](../instructions/trading-strategy.md) |
| 开发任务与验收方案 | [TODO](../TODO.md)、[改进路线图](IMPROVEMENT_ROADMAP.md) |
| 公开截图与脱敏 | [截图说明](SCREENSHOT_PLAN.md) |

## 模块指令卡

| 层 | 指令 |
|---|---|
| 数据 | [市场数据](../instructions/market-data.md)、[策略存储](../instructions/strategy-data.md)、[资金数据](../instructions/capital-data.md) |
| 发现 | [Pool](../instructions/01-pool.md)、[Quant](../instructions/02-quant.md) |
| 结构评分 | [正式V10参考](../instructions/02-quant-score-ref.md)、[接入验收记录](QUANT_V10_INTEGRATION_PLAN.md) |
| 信号 | [Bloom](../instructions/signal-bloom.md)、[Signal Plan](../instructions/signal-plan.md)、[仓位提示参考](../instructions/signal-plan-ref.md)、[Tracker](../instructions/04-tracker.md) |
| 环境 | [市场与板块](../instructions/market-regime.md)、[资金观测](../instructions/capital-observer.md)、[全球宏观](../instructions/global-macro.md) |
| 研究与账本 | [估值](../instructions/03-valuation.md)、[持仓](../instructions/signal-position.md)、[信号财务](../instructions/signal-fundamentals.md) |
| 评估与发布 | [回测](../instructions/backtest.md)、[AI 日报](../instructions/ai-daily-report.md)、[自选同步](../instructions/sync-zixuan.md) |

## 内容职责

- AGENTS 保存跨任务的共同约束和任务导航；CLAUDE 引导读取 AGENTS，不维护第二套规则。模块命令放 WORKFLOW 或对应指令卡，目录与实现边界放 DESIGN，入口不重复列举。
- 主指令卡保存当前流程、规则和约束；配对 ref 保存按需查阅的字段、公式和内部接口。
- 模型主指令卡保持固定文件名：`01-pool.md`、`02-quant.md`、`03-valuation.md`、`03-valuation-ref.md`、`04-tracker.md`；内部信号模块使用 `signal-` 前缀。
- 架构与 README 不复制细颗粒阈值；TODO 保存任务状态，不作为运行时策略。
- 模型三历史协议见 [legacy reference](../instructions/03-valuation-legacy-ref.md)，仅用于旧运行包审计；当前分析遵循 V4。
- 修改文档时区分“实现现状”“历史口径”“待实现方案”。发现文档与代码不一致，应校正描述或单独提出规则修改，不在文档整理中静默改变业务规则。
- 策略修改同步维护指令、JSON、脚本和必要测试；文档校正不递增策略版本。
- 核心模块变更时检查 DESIGN、WORKFLOW、双语 README 和 TODO 的相关描述，不要求无关文档重复改日期。
- 原始运行数据和人工讨论不进入公开文档；daily_research 按日期保存研究推理，dev_logs 保存工程复盘，均默认不纳入 Git。

## 文档校验

检查相对链接、脚本/指令路径、代码块闭合和 Git 空白差异。命令示例从运行实例调用；独立安装示例适用于新检出目录，不改变已有双目录项目的 Git 和 Python 约束。
