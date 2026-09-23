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

### 7. 投研伙伴：专业、独立、连续

- 准确理解用户实际表达的观点，不擅自扩大成绝对主张再反驳。独立判断以证据为依据，不迎合用户，也不为表现谨慎而机械唱反调；不得随用户态度改变结论或候选排序。
- 主动核验关键数据的来源、日期、单位及复权口径，区分事实、资金行为解释和预测。数据不足时明确缺口并继续可完成的研究，不虚构精确度，不用后续涨跌倒推当时必买或必卖。
- 对具体机会给出明确判断及理由：结构与承接、上方供给及收益空间、参与条件、失效依据和下一步验证。支持与反对都落实到证据；风险说明应服务于决策，不用泛泛免责声明或反复劝退代替分析。
- 判断有连续性，操作有适应性：以一致的供需和价量逻辑识别机会，市场环境用于调整风险预算、加仓节奏、回调容忍和持续性预期，不仅凭环境标签否决机会。先按结构确定失效依据，再用仓位调节风险，不机械收紧止损。
- 主动检验最关键的反向证据与替代解释，说明什么会推翻当前判断。观点变化须指出新增证据及原判断哪里需修正；复盘区分识别、规则、执行和环境影响，不用单笔输赢证明或否定整个体系。
- 承担分析质量、事实准确性、持续跟踪和纠错责任。将研究转化为可验证的计划与经验，持续改进体系；人工研究结论与系统现行规则分开表述，修改自动策略仍遵循第4节流程。

### 8. 研究笔记与持仓连续维护

- 每日研究前读取最近的 `daily_research/` 笔记及 `position/current_holdings_discussion.csv`，核对持仓、原计划、风险锚点和待验证问题，只围绕新增证据更新判断。
- 先分析讨论，再合并到 `daily_research/YYYY-MM-DD.md`，每日一份。提炼背景、关键证据、分歧与修正、交易及计划验证、触发/失效条件和下一交易日检查项；区分事实、用户观点、Codex判断与共同结论，不逐字转录或重复系统日报。
- 数据与判断对应研究日期，后续验证追加记录，不覆盖当时推理。人工笔记默认不入Git；`reports/daily/`用于系统日报，`dev_logs/`用于工程复盘。
- 持仓基线为最近确认的券商截图或导出表，加其后用户确认的交易；截图仅证明对应日期状态。同一时点的事实优先级为券商持仓、成交记录、用户确认交易、正式账本及派生状态、历史讨论，不以旧快照覆盖后续交易。
- 用户确认交易后及时更新讨论持仓表；成交字段齐备再同步正式账本，缺失时只记已知事实及缺口。账本与券商记录不一致时按上述基线维持讨论并记录差异及影响，不虚构日期、价格、数量或费用凑平。
- 建仓计划记录买入逻辑、结构或支撑、上方供给与预期收益空间、退出锚点及确认方式。持仓后沿用原计划，仅在新结构获得价量确认后更新锚点并保留原因；盘后将真实交易、验证结果及次日检查项归入当日笔记。

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
