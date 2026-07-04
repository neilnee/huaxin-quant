# Huaxin Quant

Huaxin Quant is a multi-stage stock screening and tracking workflow for identifying companies that pass fundamental filters and are developing actionable price-volume patterns.

The project is script-driven. Markdown files under `instructions/` define model rules and operating constraints; Python scripts under `scripts/` execute the deterministic parts of the workflow.

## Pipeline

```text
Pool screening
  -> Quant pattern filtering
  -> Bloom state tracking
  -> Daily review
  -> Optional valuation and timing tracker
```

Core stages:

- Model 1 Pool: fundamental and industry screening.
- Model 2 Quant: VCP/P2/P3 price-volume state detection.
- Bloom: cross-day state persistence for candidates in formation.
- Daily Review: concise review layer for human follow-up.
- Model 3 Valuation: optional valuation reports and ranking index.
- Model 4 Tracker: optional timing signals for selected names.

## Repository Layout

```text
instructions/      model rule cards
scripts/           executable pipeline scripts
WORKFLOW.md        daily operating workflow
AGENTS.md          Codex project guidance
CLAUDE.md          Claude project guidance
```

Runtime outputs are intentionally not committed:

```text
cache/
pool/
quant/
bloom/
reports/
signals/
refer/
tmp/
```

## Requirements

- Python 3.10+
- Packages in `requirements.txt`
- Access to the financial data tools used by your environment
- Optional DeepSeek-compatible API for LLM review output

Install Python dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Create local environment variables:

```bash
cp .env.example .env
```

Then fill in the values required by your local data provider.

Prepare local runtime directories:

```bash
python3 scripts/init_runtime.py
```

## Environment

Common variables:

```text
MX_APIKEY=...
HUAXIN_XUANGU_SCRIPT=/path/to/mx_xuangu.py
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`MX_APIKEY` and `HUAXIN_XUANGU_SCRIPT` are required for live Model 1 data fetching. `DEEPSEEK_*` variables are optional unless you enable LLM review generation.

## Quick Start

Check script syntax:

```bash
python3 -m py_compile scripts/*.py
```

Run Model 1:

```bash
python3 scripts/run_pool.py
```

Run Model 2 with the latest pool file:

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
```

Generate Bloom state and daily review:

```bash
python3 scripts/daily_review.py --date <YYMMDD>
```

Run a single-stock quant check:

```bash
python3 scripts/quant_filter.py --code 300604 --name 长川科技
```

See `WORKFLOW.md` for the daily operating sequence.

## Data And Secrets

Do not commit `.env` or runtime outputs. The repository only stores source code, rule cards, and project documentation.

Before publishing or sharing a fork, scan for local paths, credentials, runtime CSV/JSON files, and private research notes.

---

# Huaxin Quant 中文说明

Huaxin Quant 是一个多阶段股票筛选与跟踪系统，用来发现通过基本面筛选、并逐步形成可关注量价形态的股票。

项目以脚本驱动为主：`instructions/` 中的 Markdown 定义模型规则和执行约束，`scripts/` 中的 Python 脚本负责确定性计算、缓存、输出和状态维护。

## 流水线

```text
股票池初筛
  -> 量价形态筛选
  -> Bloom 状态跟踪
  -> 每日复盘
  -> 可选：估值 / 择时跟踪
```

核心阶段：

- 模型一 Pool：基本面和行业初筛。
- 模型二 Quant：VCP/P2/P3 等量价状态识别。
- Bloom：记录候选股票跨日状态变化。
- Daily Review：生成简短复盘，辅助人工跟踪。
- 模型三 Valuation：可选估值报告和估值排序。
- 模型四 Tracker：可选择时信号跟踪。

## 仓库结构

```text
instructions/      模型规则卡
scripts/           流水线执行脚本
WORKFLOW.md        日常执行工作流
AGENTS.md          Codex 项目说明
CLAUDE.md          Claude 项目说明
```

以下运行产物不进入 Git：

```text
cache/
pool/
quant/
bloom/
reports/
signals/
refer/
tmp/
```

## 环境要求

- Python 3.10+
- `requirements.txt` 中的 Python 依赖
- 本地金融数据工具或等价数据接口
- 可选：DeepSeek 兼容 API，用于 LLM 复盘输出

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

创建本地环境变量文件：

```bash
cp .env.example .env
```

然后按你的数据接口配置填写 `.env`。

初始化本地运行目录：

```bash
python3 scripts/init_runtime.py
```

## 环境变量

常用变量：

```text
MX_APIKEY=...
HUAXIN_XUANGU_SCRIPT=/path/to/mx_xuangu.py
DEEPSEEK_API_KEY=...
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

`MX_APIKEY` 和 `HUAXIN_XUANGU_SCRIPT` 用于模型一实时拉取数据。`DEEPSEEK_*` 只在启用 LLM 复盘时需要。

## 快速开始

检查脚本语法：

```bash
python3 -m py_compile scripts/*.py
```

运行模型一：

```bash
python3 scripts/run_pool.py
```

使用模型一结果运行模型二：

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
```

生成 Bloom 状态和每日复盘：

```bash
python3 scripts/daily_review.py --date <YYMMDD>
```

单股量价检查：

```bash
python3 scripts/quant_filter.py --code 300604 --name 长川科技
```

日常执行顺序见 `WORKFLOW.md`。

## 数据和密钥

不要提交 `.env` 或运行产物。本仓库只保存源代码、模型规则卡和项目文档。

公开 fork 或发布前，建议再次扫描本地路径、密钥、运行 CSV/JSON 文件和私人研究资料。
