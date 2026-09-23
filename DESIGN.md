# Huaxin Quant Design

最近核对：2026-09-08。本文描述当前实现；未实现的改进见 [TODO](TODO.md) 和 [工程与策略改进路线图](docs/IMPROVEMENT_ROADMAP.md)。模块阈值以配套指令卡和策略 JSON 为准，本文件不重复维护阈值表。

## 1. 目标与当前边界

Huaxin Quant 是面向 A 股的候选发现、信号跟踪和研究复盘系统。日常链路已经覆盖数据准备、双池筛选、VCP、生命周期、次日计划、市场与资金环境、回测及页面发布。它尚未形成账户级自动交易系统，也未完成当前版本买点的独立样本外验证。

脚本决定结构、信号、评分、状态和计算结果；LLM 解释受约束的事实，并完成按需机构共识研究。LLM 失败不能补造数据；不同模块的发布门禁见 WORKFLOW。

## 2. 源码与运行实例

| 层 | 职责 | 典型内容 |
|---|---|---|
| source-repo | Git 管理的源码与文档 | instructions、scripts、strategies、dashboard 页面源码、docs |
| runtime-workspace | 日常执行与数据积累 | .venv、.env、cache、pool、quant、bloom、signal_plan、market、capital、backtest、reports、position、.tmp |
| 人工记录 | 研究讨论与工程复盘 | daily_research、dev_logs，默认不纳入 Git |

本地实例通过软链访问源码。Git 使用 `git -C <source-repo>`，项目脚本始终在运行实例通过 `.venv/bin/python scripts/...` 调用。仓库独立部署可在检出目录初始化；已有双目录实例不得因此改变执行根目录。

物理存放位置与 Git 跟踪范围是两回事：本实例的 dashboard、daily_research 和 dev_logs 也通过软链访问云盘目录，其中生成数据及人工记录仍默认被 Git 忽略。

## 3. 当前模块与依赖

| 模块 | 主要输入 | 主要职责与下游 |
|---|---|---|
| Market Data | 通达信、妙想备用数据 | 原始日线、证券母体、板块快照、独立公司行为；供 Market、Pool、Quant 等复用 |
| Pool | 财务筛选缓存、全 A 日线 | 核心质量池与 RS 扩展池合并，供 Quant 扫描；扩展独有标的财务未核验 |
| Quant | 当日 Pool、时点前复权行情 | 单日 VCP 结构、三类买点、评分、风险和突破后上下文 |
| Bloom | Quant、既有生命周期 | 跨日状态、事件、重点观察与估值候选标记 |
| Signal Plan | Quant 的 setup_plan_inputs | 次日条件计划；不补造模型二当日信号 |
| Market Regime | 共享原始行情与板块快照 | 宽基、广度、板块阶段、主线解释；不回写 Quant |
| Capital Observer | 成交额与资金数据 | 独立资金事实和解释；不参与模型二评分 |
| Signal Fundamentals | 当日信号、计划和 Pool | 补查扩展独有股票财务，提供非阻断风险提示 |
| Dashboard Signals | Quant、Plan、市场/板块及旁路提示 | 展示当日买点与次日计划；当前环境仓位提示在此适配器计算 |
| Backtest | Bloom/Quant、前日 Plan、后续行情与环境 | VCP 首次入选和 Plan 兑现的事件研究、总回报与生命周期 |
| Valuation | 用户指定标的、财务与证据 | 独立机构共识研究和三情景估值；不由 Quant/Bloom 自动触发 |
| Position | 用户确认的真实交易及计划 | FIFO 批次、持仓状态和年度账本重建；策略监控待建 |
| Global Macro | 官方 API/RSS | 独立观测、事件和信源健康；未接入每日主链路 |
| AI Daily Report | 已发布市场/资金/VCP/信号包 | 确定性汇总 JSON，不调用新 LLM |
| Watchlist Sync | Bloom 与模型二买点、受管自选账本 | 可选外部自选同步，保留手工自选 |

`daily.py` 是每日总控，直接调用 Bloom 和 Plan。`tracker.py` 是按需生成两者合并日报的工具；不调度估值或持仓，不替代 daily。

## 4. 数据、规则、状态与展示

| 层 | 当前实现 |
|---|---|
| 数据 | scripts/data 下行情、财务筛选、资金、宏观、公司行为适配与 SQLite 存储 |
| 规则 | strategies/*.json 与模块内确定性算法 |
| 状态 | 策略数据库中的 Quant/Plan/Bloom 修订、事件和生命周期；真实交易另存 Position 账本 |
| 编排 | daily、run_pool、run_valuation 及独立模块 CLI |
| 发布 | strategy_publish、dashboard_* 和各模块发布函数 |

策略配置承载阈值、权重和枚举，算法仍由代码实现。当前加载器校验 JSON 和 strategy_version，尚无全量字段类型、范围和跨参数约束校验。

环境仓位计算目前位于 `dashboard_signals.py`，因此“页面只读”指浏览器消费已发布包，并不意味着所有发布适配器都只搬运字段。独立决策模块是后续改进，不是当前能力。

## 5. 权威数据与兼容发布

| 数据 | 权威来源 | 兼容/展示产物 |
|---|---|---|
| 原始日线与公司行为 | cache/market_data/market_data.sqlite | 按需行情窗口及审计字段 |
| Quant、Plan、Bloom、兑现与生命周期 | cache/strategy/strategy_data.sqlite | quant CSV、quant_runs JSON、Bloom 状态文件、Plan JSON/Markdown |
| 市场派生状态 | cache/market_regime/market_regime.sqlite 及模块发布产物 | market/、Dashboard 市场包 |
| 资金 | cache/capital_flow/capital_data.sqlite | capital/、Dashboard 资金包 |
| 宏观 | cache/global_macro/global_macro.sqlite | macro/ 健康与采集摘要 |
| 真实交易 | position/trades/ 与 position_plan.csv | lots_current、日快照、年度表现 |
| 估值研究 | cache/valuation_runs/<run_id>/ 已验证运行包 | 报告、索引、排名和公司页 |

核心策略模块先提交数据库，再发布兼容文件；同日内容变化保留修订，current_documents 指向当前版本。旧文件只作兼容或迁移回退。历史修订存在，不等于已冻结回测使用的全部代码、配置和输入版本。

## 6. 日期与价格口径

- 原始 OHLCV 不被复权结果覆盖；公司行为独立存储。
- Quant 显式使用不晚于 run_date 的公司行为计算时点前复权 OHLC，量额保持原始口径。
- Pool 扩展 RS 当前直接使用原始收盘价；Market 未显式申请复权的计算也使用原始价格。不能把 Quant 的复权能力描述为所有消费者已统一。
- Backtest 的 5/10/20 日收益采用包含公司行为的持有期总回报。
- 默认日期函数按 15:00 和周末回退，尚不包含完整节假日日历；日期有效性仍需行情与交易日记录核验。
- 历史板块/行业回填须保留来源和 history_basis，不得解释为当时可得事实；指定 --date 不自动保证全部输入严格点时。

## 7. 三种不同的机会记录

| 记录 | 当前定义 | 不能替代 |
|---|---|---|
| VCP_SELECTION | 同股票同结构首次进入 VCP active 展示列表 | 买点触发或真实成交 |
| 模型二 setup_signal | 当日规则触发，质量为 A/B/C/D | 前日计划等级 |
| 回测 BUY_POINT | 前日 Plan 次日量价兑现，等级为 A/REGULAR | 模型二当日全部买点 |

目前第三类允许当天模型二没有同名信号。页面仅在实际信号与计划命中一致时增加审计标签。独立的模型二信号研究集合与版本隔离尚待补齐。

## 8. 验证与后续演进

现有离线检查涵盖语法、单元测试、策略 JSON、JavaScript 语法及 Git 空白；CI 使用 Python 3.11/3.12。测试通过证明覆盖场景下的实现行为，不证明盈利能力、严格点时或完整灾难恢复能力。

后续按顺序推进：事件口径与数据正确性 → 当前版本评估和样本外跟踪 → 账户风险与持仓监控 → 运维恢复与适度模块拆分。验收细则集中维护在 [改进路线图](docs/IMPROVEMENT_ROADMAP.md)。
