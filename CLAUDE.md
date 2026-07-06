# CLAUDE.md

## 项目概要

Huaxin Quant，多模型流水线的股票花期发现与跟踪系统。每个筛选模型对应 `instructions/` 下的一个指令文件，由 LLM 读取后自动执行。

项目采用**双目录架构**：本地工作区负责日常运行（Claude Code 工作目录），源码仓库负责 Git 版本管理。指令卡、脚本、配置等源文件可以通过软链在本地编辑，Git 在源码仓库侧追踪实体文件；数据产出（缓存、筛选结果、报告）和本地开发日志不跟 Git。

## ⚠️ 核心规则

### 规则一：Git 操作必须走云盘路径

本地目录没有 `.git`，所有 Git 操作使用 `-C` 指向云盘仓库，避免反复触发授权：

```
git -C <source-repo> status
git -C <source-repo> diff
git -C <source-repo> add <file>
git -C <source-repo> commit -m "..."
```

**禁止**在本地目录执行 `git` 命令（会因找不到 `.git` 而失败或触发额外授权）。

### 规则二：临时脚本统一写到 `.tmp/` 目录

`.tmp/` 目录已在 `settings.json` 中预授权读写编辑（`Read/Write/Edit(.tmp/**)`），写入和执行不会触发授权弹窗。脚本放 `.tmp/scripts/` 子目录下：

```
✅ Write(.tmp/scripts/calc.py)  →  Bash(python3 .tmp/scripts/calc.py)
❌ Write(calc.py)              →  Bash(python3 calc.py)           ← 触发授权
❌ Bash(python3 -c "...")      →  单行 python3 -c 无法预授权       ← 触发授权
❌ Bash(python3 << *)          →  多行 heredoc 匹配不了            ← 反复弹授权
```

用完清理：`rm -rf .tmp/scripts/`（保留 `.tmp/` 目录本身）。

### 规则三：脚本始终走本地工作区 symlink 路径调用

脚本通过 `quant_lab/scripts/`（symlink → 云盘）调用，`PROJECT_ROOT` 自然指向本地工作区：

```bash
# ✅ 正确 — PROJECT_ROOT = /Users/neil/ai/quant_lab
python3 /Users/neil/ai/quant_lab/scripts/quant_filter.py --pool pool/pool_260706.csv

# ❌ 错误 — PROJECT_ROOT = 云盘，读不到数据目录
python3 ~/Library/CloudStorage/OneDrive-个人/huaxin_quant/scripts/quant_filter.py ...
```

## 🧭 模块使用指南

> 以下只列常用命令，详细规则见对应指令卡。

### 模型一：海选初筛（Pool）

全市场基本面过滤 + 行业排除 + 软标签评分。执行方式参考 `instructions/01-pool.md`。

```bash
python3 scripts/run_pool.py                  # 端到端
python3 scripts/run_pool.py --skip-fetch     # 复用 xuangu 缓存
python3 scripts/run_pool.py --force-refresh  # 强制重拉
```

### 模型二：VCP 精筛（Quant）

逐只识别 VCP 收缩结构、量能趋势、风险标记。执行方式参考 `instructions/02-quant.md`。

```bash
python3 scripts/quant_filter.py                          # 全量
python3 scripts/quant_filter.py --code 603444            # 单只
python3 scripts/quant_filter.py --codes 300442,688676    # 多只
```

### Bloom 信号层

消费模型二 JSON，维护跨日信号生命周期，LLM 解读重点观察标的。执行方式参考 `instructions/signal-bloom.md`。

```bash
python3 scripts/bloom.py [--date 260706]
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

### 日常完整链路

```bash
python3 scripts/run_pool.py --skip-fetch
python3 scripts/quant_filter.py
python3 scripts/bloom.py
cat bloom/bloom_<YYMMDD>.md
```

## 目录架构

本地工作区 `quant_lab/`（`🔗` = symlink → 云盘 Git 管理）：

| 目录 | 用途 | 来源 |
|------|------|------|
| `instructions/` | 模型执行指令卡 | 🔗 云盘 |
| `scripts/` | 辅助 Python 脚本 | 🔗 云盘 |
| `strategies/` | 策略 JSON 配置 | 🔗 云盘 |
| `CLAUDE.md` | 本文件 | 🔗 云盘 |
| `cache/` | 缓存：daily/ xuangu/ financial/ research/ 等 | 本地 |
| `pool/` | 模型一输出 | 本地 |
| `quant/` | 模型二输出 | 本地 |
| `bloom/` | Bloom 信号报告 + state/ | 本地 |
| `reports/` | 估值报告 + indexes/ | 本地 |
| `position/` | 持仓账本 | 本地 |
| `.tmp/` | 临时脚本（预授权，用完即删） | 本地 |
| `.env` | 环境变量（不入 Git） | 本地 |

> 云盘 `source repo/` 只放 Git 管理源码，数据产出全部在本地。

## 开发约定

- **指令文件是源头**，脚本是指令的配套实现。改逻辑先改指令，再改脚本；脚本与指令同提交更新
- **指令卡保持精简**：只保留 LLM 执行所需内容（流程、规则、约束）。公式速查、报告模板、字段定义等放入配对 `*-ref.md`，按需查阅
- **固定文件名**：模型主指令卡固定为 `01-pool.md`、`02-quant.md`、`03-valuation.md`、`03-valuation-ref.md`、`04-tracker.md`、`04-tracker-ref.md`；模型四内部信号模块使用 `signal-` 前缀，如 `signal-bloom.md`
- **分支开发**：较大改动在 Git feature 分支上直接修改活跃文件；稳定后 commit/merge 保留历史，不靠复制文件发版
- **收尾更新**：每次完成一组规则变更后，更新 `TODO.md` 勾掉已完成项
- **数据口径统一**：模型一和模型三使用相同报告期数据，避免跨模型数据口径不一致
- **Agent 执行后清理**：mx-search 等 skill 并行执行后可能遗留 `tmp_*/` 目录，每次批量估值或搜索完成后清理

## 缓存策略

| 目录 | 有效期 | 清理策略 |
|------|--------|----------|
| `cache/xuangu/` | 5 天 | `process_pool.py` 自动跳过超期文件。财报指标季度更新，无需每日重拉 |
| `cache/daily/` | 5 天 | 文件名含日期（`<code>_<YYMMDD>.pkl`），跨天自动不命中。模型二+四共享，每次运行清理超期文件 |
| `cache/financial/` | 90 天 | `valuate.py` 超期警告，自动清理 .xlsx/.txt（仅保留 _raw.json）。同一报告期内数据不变 |
| `cache/briefing/` | — | 文件名含日期，`--no-cache` 清除旧简报后重新生成 |
| `cache/calc_params/` | 按需 | 估值引擎输入参数 JSON，文件名含日期 |
| `cache/calc_results/` | 按需 | 估值引擎输出结果 JSON，文件名含日期 |

## 数据输出规范

- 模型输出放对应目录（`pool/`、`quant/`、`reports/`）
- CSV 编码 UTF-8 BOM
- 股票代码公式文本 `="000001"`
- 数值保留 2 位小数

## 自选股监控池

东方财富自选股的"全部"分组作为监控池权威列表。mx-zixuan 增删操作仅对"全部"分组生效。入池 = add，出池 = delete。
