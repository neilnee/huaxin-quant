# CLAUDE.md

## 项目概要

Huaxin Quant，多模型流水线的股票花期发现与跟踪系统。每个筛选模型对应 `instructions/` 下的一个指令文件，由 LLM 读取后自动执行。

项目采用**双目录架构**：本地工作区负责日常运行（Claude Code 工作目录），源码仓库负责 Git 版本管理。指令卡、脚本、配置等源文件可以通过软链在本地编辑，Git 在源码仓库侧追踪实体文件；数据产出（缓存、筛选结果、报告）和本地开发日志不跟 Git。

## ⚠️ 核心规则

### 规则一：Git 操作必须走云盘路径

本地目录没有 `.git`，所有 Git 操作使用 `git -C <云盘路径>`，**禁止**在本地目录直接执行 `git`。

### 规则二：临时脚本统一写到 `.tmp/` 目录

`.tmp/` 已在 `settings.json` 预授权，直接读写不弹窗。用完 `rm -rf .tmp/scripts/`。

### 规则三：脚本始终走本地 symlink 路径调用

`quant_lab/scripts/` 是 symlink，走这个路径调用 `PROJECT_ROOT` 自然指向本地工作区。走云盘真实路径会导致读不到数据目录。

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

## 自选股监控池

东方财富自选股的"全部"分组作为监控池权威列表。mx-zixuan 增删操作仅对"全部"分组生效。入池 = add，出池 = delete。
