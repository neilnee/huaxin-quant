# AGENTS.md

## 项目概要

Huaxin Quant，多模型流水线的股票花期发现与跟踪系统。Codex 在本项目中负责阅读指令卡、维护配套脚本、执行数据流水线、验证输出结果，并在需要时提交代码。

项目采用双目录架构：

- `<runtime-workspace>`：本地工作区，作为 Huaxin Quant 的运行实例，负责日常运行、缓存、输出、报告。
- `<source-repo>`：云盘 Git 仓库，负责 Huaxin Quant 源代码、指令卡和开发文档版本管理。

`instructions/`、`scripts/`、`strategies/`、`CLAUDE.md`、`AGENTS.md`、`TODO.md` 在本地工作区中可以是指向源码仓库的软链。缓存、输出目录、持仓账本和本地开发日志默认不进 Git。

## 核心规则

### 1. Git 操作必须走云盘路径

本地工作区没有 `.git`。所有 Git 命令必须显式使用云盘仓库路径：

```bash
git -C <source-repo> status
git -C <source-repo> diff
git -C <source-repo> add <file>
git -C <source-repo> commit -m "..."
```

禁止在 `<runtime-workspace>` 直接执行普通 `git status`、`git diff`、`git add`、`git commit`。

### 2. 脚本始终走本地 symlink 路径调用

运行项目脚本时必须使用本地工作区路径，例如：

```bash
python3 scripts/run_pool.py
python3 scripts/quant_filter.py
python3 scripts/bloom.py
python3 scripts/position.py
```

`quant_lab/scripts/` 是指向云盘源码仓库的 symlink。通过本地 symlink 调用时，脚本内的 `PROJECT_ROOT` 自然指向本地工作区，可以正确读取 `cache/`、`pool/`、`quant/`、`bloom/`、`reports/`、`position/` 等运行数据。不要用云盘真实路径直接调用脚本。

### 3. 源文件走 Git，数据产物不提交

纳入 Git 的内容：

- `CLAUDE.md`
- `AGENTS.md`
- `TODO.md`
- `instructions/*.md`
- `scripts/*.py`
- `strategies/*.json`
- 必要的项目配置和开发文档

默认不纳入 Git 的内容：

- `cache/`
- `pool/`
- `quant/`
- `bloom/`
- `reports/`
- `daily_research/`
- `position/`
- `dev_logs/`
- `.env`
- `.tmp/`
- `tmp_*/`

运行模型产生的 CSV、JSON、PKL、报告文件只作为本地结果使用，除非用户明确要求提交。

### 4. 改规则先改指令卡，再改脚本

每个模型由 `instructions/` 下的指令卡定义规则，`scripts/` 下的脚本负责稳定执行。

修改筛选逻辑时顺序如下：

1. 更新对应指令卡，说明规则、阈值、字段口径和输出结构。
2. 更新配套脚本。
3. 运行语法检查和必要的脚本验证。
4. 对结果做摘要说明。
5. 代码和指令卡同一次提交。

不要只改脚本不改指令卡，也不要只改指令卡不更新脚本。

指令卡保持精简：只保留 LLM 执行所需内容（流程、规则、约束）。公式速查、报告模板、字段定义等放入配对 `*-ref.md`，按需查阅。

### 5. 临时脚本放 `.tmp/`

需要临时分析或一次性脚本时，统一写到：

```text
.tmp/scripts/
```

优先使用项目已有脚本和标准命令。临时脚本用完后清理 `.tmp/scripts/`，保留 `.tmp/` 目录本身。

Codex 执行时优先用 `rg`、`sed`、`python3 -m py_compile`、项目脚本等稳定命令。不要用 ad-hoc 命令污染项目根目录。

### 6. Python 使用项目虚拟环境

项目要求 Python 3.11+。本地运行实例使用根目录 `.venv/`，执行脚本和检查时优先显式调用：

```bash
.venv/bin/python scripts/check.py
.venv/bin/python scripts/daily.py
```

也可以先执行 `source .venv/bin/activate`，再使用文档中的 `python3 scripts/...` 命令。不要使用 macOS 自带的 `/usr/bin/python3`（Python 3.9 / LibreSSL）。

### 7. 每日投研笔记统一放入 `daily_research/`

每日人工投研讨论统一写入：

```text
daily_research/YYYY-MM-DD.md
```

- 每个研究日期只保留一份笔记；同日已有文件时在原文件中合并更新，不另建重复文件。
- 每日投研笔记是用户与 Codex 共同讨论、验证和修正判断的思考过程记录，不是系统行情摘要、用户口述转录或只有最终结论的清单。
- 对话中优先完成分析和讨论，再归档双方已经形成的判断；写日志不能代替回应用户，也不能只按用户原话记录。Codex 应补充客观数据、独立判断、不同意见、不确定性和推翻条件。
- 笔记应在适用时记录：当日市场与执行背景、真实交易事实、原计划、关键价量证据、供给与承接解释、讨论中的分歧及修正、最终仓位或候选结论、关键价格区间、触发/失效条件、下一交易日检查项。
- 记录的是提炼后的推理链和决策依据，不逐字保存对话。应明确区分客观事实、用户观点、Codex 补充判断和共同结论，避免把尚未确认的假设写成事实。
- 当用户确认建仓、加减仓或清仓但未提供成交价、数量或时间时，可在研究笔记中记录策略事实并注明信息不完整；不得推断缺失成交字段，也不得据此写入正式交易账本。
- 下一交易日开始相关研究前，优先回顾最近的 `daily_research/` 记录，沿用已有观察框架，只更新新增证据、验证结果和观点变化，避免从头重复讨论。
- `reports/daily/` 保留给流水线或脚本生成的系统日报，不存放人工投研对话归档。
- `dev_logs/` 只记录工程开发、规则调整和故障复盘，不代替每日投研笔记。
- 笔记中的行情、财务数据和判断应注明或隐含对应研究日期，不用后续信息改写当时结论；需要修订时追加后续验证记录，保留观点演变轨迹，便于复盘思考如何成长。
- `daily_research/` 属于本地研究产物，默认不纳入 Git，除非用户明确要求提交。

### 8. 每日研究与持仓连续维护

- 每日讨论开始前，先读取最近的研究笔记和 `position/current_holdings_discussion.csv`，核对上一交易日的持仓、原计划、风险锚点和待验证问题；当天只根据新增证据更新判断。
- 实际持仓以“最近一次用户确认的券商截图或导出表 + 截图之后用户确认的交易”为连续基线。截图只证明其对应日期的账户状态，后续交易按真实日期叠加。
- 事实优先级依次为：券商当前持仓截图或导出表、券商成交记录、用户明确确认的交易事实、正式账本及派生状态、历史讨论记录。较低层来源不得覆盖较高层来源。
- `position/current_holdings_discussion.csv` 用于日常对话中的当前持仓维护；确认建仓、加减仓或清仓后应及时更新。完整成交字段齐备时同步正式交易账本；字段不完整时只记录已确认事实和缺失项。
- 截图与正式账本不一致时，以截图维持当前持仓讨论，并记录差异、数据缺口和影响范围；不得虚构成交日期、价格、数量或费用来凑平账本，取得原始记录后再重建。
- 每次建仓计划应同时记录买入逻辑、结构或支撑依据、上方供给与预期收益空间、退出锚点及确认方式。持仓后沿用原计划验证，只有新结构得到价格和量能确认时才更新锚点，并在笔记中保留修改原因。
- 盘后把真实交易、持仓变化、原计划验证结果、观点修正和下一交易日检查项合并到当日研究笔记；不要用最新结论覆盖早先判断，应保留判断如何被验证或推翻。

## 目录职责

```text
quant_lab/  # Huaxin Quant 本地运行实例
├── instructions/      -> 云盘仓库，模型指令卡
├── scripts/           -> 云盘仓库，模型执行脚本
├── strategies/         -> 云盘仓库，策略 JSON 配置
├── CLAUDE.md          -> 云盘仓库，Claude 工程规范
├── AGENTS.md          -> Codex 工程规范
├── TODO.md            -> 云盘仓库，项目待办
├── dev_logs/          本地开发复盘日志（不纳入公开核心仓库）
├── daily_research/     每日人工投研笔记（按 YYYY-MM-DD.md 归档，不纳入 Git）
├── cache/             本地缓存（daily/xuangu/financial/research 等）
├── pool/              模型一输出
├── quant/             模型二输出
├── bloom/             Bloom 信号报告与 state/
├── macro/             全球宏观信源健康与采集摘要
├── reports/           估值报告与 indexes/
├── position/          本地持仓账本
├── .tmp/              临时脚本和临时文件（用完清理）
└── .env               本地密钥配置，不入 Git
```

## 模块使用指南

以下只列常用命令，详细流程、规则、字段口径和输出结构见对应指令卡、策略 JSON 和脚本实现。Codex 执行或修改某个模块前，先读对应指令卡。

### 模型一：海选初筛（Pool）

基本面核心质量池 + RS 强势扩展池 + 来源与软标签；不再整体排除行业。执行方式参考 `instructions/01-pool.md`。

```bash
python3 scripts/run_pool.py
python3 scripts/run_pool.py --skip-fetch
python3 scripts/run_pool.py --force-refresh
```

### 模型二：VCP 精筛（Quant）

逐只识别 VCP 收缩结构、量能趋势、风险标记。执行方式参考 `instructions/02-quant.md`。

```bash
python3 scripts/quant_filter.py
python3 scripts/quant_filter.py --code 603444
python3 scripts/quant_filter.py --codes 300442,688676
```

### 市场状态与板块热度（Market Regime）

独立的市场环境旁路层：准备共享通达信行情，计算宽基趋势、全 A 广度、板块相对强度与阶段状态；不读取或改写 Pool、Quant、Bloom 结果。执行方式参考 `instructions/market-regime.md`。

```bash
python3 scripts/market_regime.py init --lookback 300  # 首次初始化
python3 scripts/market_regime.py update               # 盘后增量更新
python3 scripts/market_regime.py run                  # 生成市场报告与面板数据
python3 scripts/market_regime.py run --no-llm         # 跳过 LLM 解读
python3 scripts/market_regime.py status               # 检查数据就绪状态
```

### 全球宏观与流动性雷达（Global Macro）

独立的官方信源数据层：记录信源健康，采集美元流动性、利率、汇率、波动率代理变量和央行官方事件；不读取或改写 Pool、Quant、Bloom、Market Regime 或资金观测结果。执行方式参考 `instructions/global-macro.md`。

```bash
python3 scripts/global_macro.py probe
python3 scripts/global_macro.py fetch
python3 scripts/global_macro.py status --days 7
```

### Bloom 信号层

优先消费策略库中的模型二结果（JSON 为兼容入口），维护跨日信号生命周期，LLM 解读重点观察标的。执行方式参考 `instructions/signal-bloom.md`。

```bash
python3 scripts/bloom.py
python3 scripts/bloom.py --date 260706
```

### 持仓管理（Position）

独立账本：交易流水、当前持仓、每日状态快照。执行方式参考 `instructions/signal-position.md`。

```bash
python3 scripts/position.py add-trade --trade-date 2026-07-06 --code 688676 --name 金盘科技 --side BUY --shares 200 --price 83.89
python3 scripts/position.py rebuild --as-of 2026-07-06
```

### 模型三：深度估值（Valuation）

按需机构共识研究与三情景估值，默认使用 `run_valuation.py`；不由 Bloom 自动触发。执行方式参考 `instructions/03-valuation.md`。

```bash
# 详见 instructions/03-valuation.md
```

## 代码编辑规范

- 优先遵循现有脚本风格，不引入无必要的新框架。
- 手工编辑文件使用 `apply_patch`。
- 不用 Python 写文件，除非是批量机械转换且比补丁更安全。
- 默认 ASCII；中文文档和既有中文文件可继续使用中文。
- 注释只解释不直观的业务规则或兼容逻辑。
- 不做无关重构。
- 不回滚用户或其他工具产生的改动。
- 较大改动在 Git feature 分支上直接修改活跃文件；稳定后 commit/merge 保留历史，不靠复制文件发版。
- 每次完成一组规则变更后，更新 `TODO.md` 勾掉已完成项。
- mx-search 等 skill 并行执行后可能遗留 `tmp_*/` 目录，每次批量估值或搜索完成后清理。

## 文档导航与当前实现边界

最近核对：2026-09-08。完整模块导航见 [docs/README.md](docs/README.md)，架构见 [DESIGN.md](DESIGN.md)，执行流程见 [WORKFLOW.md](WORKFLOW.md)，待实现方案见 [docs/IMPROVEMENT_ROADMAP.md](docs/IMPROVEMENT_ROADMAP.md)。

- daily.py 是每日总控；tracker.py 仅按需合并 Bloom 与 Signal Plan 报告。
- cache/strategy/strategy_data.sqlite 是 Quant/Plan/Bloom/兑现与生命周期权威存储，文件是兼容发布；真实交易仍由 Position 流水管理。
- market/、capital/、backtest/、signal_plan/、reports/ai_daily/ 均为运行产物；dashboard 页面源码入 Git，dashboard/data 生成包不入 Git。
- Position 已有账本，账户级风控、独立持仓监控、自动估值队列尚未落地。不得把 TODO 或改进路线图中的建议当作现行规则。
- 当前代码与文档描述不符时，先区分描述过期和规则变更；仅校正文档不更改策略版本，实际规则修改仍按指令卡→配置/脚本→验证执行。
