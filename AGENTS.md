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
├── cache/             本地缓存（daily/xuangu/financial/research 等）
├── pool/              模型一输出
├── quant/             模型二输出
├── bloom/             Bloom 信号报告与 state/
├── reports/           估值报告与 indexes/
├── position/          本地持仓账本
├── .tmp/              临时脚本和临时文件（用完清理）
└── .env               本地密钥配置，不入 Git
```

## 模块使用指南

以下只列常用命令，详细流程、规则、字段口径和输出结构见对应指令卡、策略 JSON 和脚本实现。Codex 执行或修改某个模块前，先读对应指令卡。

### 模型一：海选初筛（Pool）

全市场基本面过滤 + 行业排除 + 软标签评分。执行方式参考 `instructions/01-pool.md`。

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

### Bloom 信号层

消费模型二 JSON，维护跨日信号生命周期，LLM 解读重点观察标的。执行方式参考 `instructions/signal-bloom.md`。

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

LLM 拆解业务线 + 脚本 DCF/PE 计算，按需手动触发。执行方式参考 `instructions/03-valuation.md`。

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
