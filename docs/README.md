# 文档索引与维护约定

最近核对：2026-09-08。

## 按任务阅读

| 任务 | 入口 |
|---|---|
| 项目介绍与独立安装 | [README](../README.md)、[English](../README.en.md) |
| 架构、模块依赖和权威数据 | [DESIGN](../DESIGN.md) |
| 每日执行、估值恢复、异常处理 | [WORKFLOW](../WORKFLOW.md) |
| Agent 工程规范 | [AGENTS](../AGENTS.md)、[CLAUDE](../CLAUDE.md) |
| 策略理解 | [策略阅读指南](../instructions/trading-strategy.md) |
| 开发任务与验收方案 | [TODO](../TODO.md)、[改进路线图](IMPROVEMENT_ROADMAP.md) |
| 公开截图与脱敏 | [截图说明](SCREENSHOT_PLAN.md) |

## 模块指令卡

| 层 | 指令 |
|---|---|
| 数据 | [市场数据](../instructions/market-data.md)、[策略存储](../instructions/strategy-data.md)、[资金数据](../instructions/capital-data.md) |
| 发现 | [Pool](../instructions/01-pool.md)、[Quant](../instructions/02-quant.md) |
| 信号 | [Bloom](../instructions/signal-bloom.md)、[Signal Plan](../instructions/signal-plan.md)、[Tracker](../instructions/04-tracker.md) |
| 环境 | [市场与板块](../instructions/market-regime.md)、[资金观测](../instructions/capital-observer.md)、[全球宏观](../instructions/global-macro.md) |
| 研究与账本 | [估值](../instructions/03-valuation.md)、[持仓](../instructions/signal-position.md)、[信号财务](../instructions/signal-fundamentals.md) |
| 评估与发布 | [回测](../instructions/backtest.md)、[AI 日报](../instructions/ai-daily-report.md)、[自选同步](../instructions/sync-zixuan.md) |

## 内容职责

- 主指令卡保存当前流程、规则和约束；配对 ref 保存按需查阅的字段、公式和内部接口。
- 架构与 README 不复制细颗粒阈值；TODO 保存任务状态，不作为运行时策略。
- 模型三历史协议见 [legacy reference](../instructions/03-valuation-legacy-ref.md)，仅用于旧运行包审计；当前分析遵循 V4。
- 修改文档时区分“实现现状”“历史口径”“待实现方案”。发现文档与代码不一致，应校正描述或单独提出规则修改，不在文档整理中静默改变业务规则。
- 策略修改同步维护指令、JSON、脚本和必要测试；文档校正不递增策略版本。
- 核心模块变更时检查 DESIGN、WORKFLOW、双语 README 和 TODO 的相关描述，不要求无关文档重复改日期。
- 原始运行数据和人工讨论不进入公开文档；daily_research 按日期保存研究推理，dev_logs 保存工程复盘，均默认不纳入 Git。

## 文档校验

检查相对链接、脚本/指令路径、代码块闭合和 Git 空白差异。命令示例从运行实例调用；独立安装示例适用于新检出目录，不改变已有双目录项目的 Git 和 Python 约束。
