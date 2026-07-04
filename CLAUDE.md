# CLAUDE.md

## 项目概要

Huaxin Quant，多模型流水线的股票花期发现与跟踪系统。每个筛选模型对应 `instructions/` 下的一个指令文件，由 LLM 读取后自动执行。

项目采用**双目录架构**：本地工作区负责日常运行（Claude Code 工作目录），云盘负责 Git 版本管理。指令卡、脚本、配置等源文件通过软链在本地编辑，Git 在云盘侧追踪实体文件；数据产出（缓存、筛选结果、报告）纯本地，不跟 Git。

## ⚠️ 核心规则

### 规则一：Git 操作必须走云盘路径

本地目录没有 `.git`，所有 Git 操作使用 `-C` 指向云盘仓库，避免反复触发授权：

```
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant status
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant diff
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant add <file>
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant commit -m "..."
```

**禁止**在本地目录执行 `git` 命令（会因找不到 `.git` 而失败或触发额外授权）。

### 规则二：临时脚本统一写到 `tmp/` 目录

`tmp/` 目录已在 `settings.json` 中预授权读写编辑（`Read/Write/Edit(tmp/**)`），写入和执行不会触发授权弹窗。脚本放 `tmp/scripts/` 子目录下：

```
✅ Write(tmp/scripts/calc.py)  →  Bash(python3 tmp/scripts/calc.py)
❌ Write(calc.py)              →  Bash(python3 calc.py)           ← 触发授权
❌ Bash(python3 -c "...")      →  单行 python3 -c 无法预授权       ← 触发授权
❌ Bash(python3 << *)          →  多行 heredoc 匹配不了            ← 反复弹授权
```

用完清理：`rm -rf tmp/scripts/`（保留 `tmp/` 目录本身）。

## 目录架构

```
quant_lab/（本地工作区 · Huaxin Quant 运行实例）
│
├── .claude/                        本地目录 · Claude Code 项目配置
│   ├── settings.json          🔗→ 云盘 .claude/settings.json（Git 版本管理）
│   └── settings.local.json        本地机特定权限（不入 Git）
│
├── instructions/              🔗→ 云盘 · 模型执行指令卡（Git 管理）
├── scripts/                   🔗→ 云盘 · 辅助 Python 脚本（Git 管理）
├── CLAUDE.md                  🔗→ 云盘 · 本文件（Git 管理）
├── dev_logs/                  🔗→ 云盘 · 开发复盘日志（Git 管理）
│
├── cache/                         本地 · 所有缓存数据，脚本自动管理
│   ├── daily/                      模型二+四共享日线缓存（<code>_<YYMMDD>.pkl）
│   ├── xuangu/                     模型一选股数据缓存
│   ├── financial/                  模型三财报缓存（_raw.json 保留）
│   ├── briefing/                   模型三简报册缓存
│   ├── calc_params/                模型三估值引擎输入参数 JSON
│   ├── calc_results/               模型三估值引擎输出结果 JSON
│   ├── research/                   mx-search 研报搜索结果
│   ├── reviews/                    LLM 每日复盘输入包（可重建）
│   └── zixuan/                     自选股同步本地缓存（zixuan.csv）
│
├── pool/                           本地 · 模型一（海选初筛）输出
├── quant/                          本地 · 模型二（量价精筛）输出
├── bloom/                          本地 · 模型二后的花期观察状态层
│                                    bloom_events.jsonl + bloom_state.csv
├── reports/                        本地 · 模型三（深度估值）报告 + 每日复盘报告
│                                    当前保留 _index.csv + _ranking.csv；daily/ 放花期复盘
├── signals/                        本地 · 模型四（择时跟踪）工作区
│                                    每日信号报告 + core_pool.csv + positions.csv
│                                    + batches.csv + trade_log.csv
├── refer/                          本地 · 参考资料（策略文档、研究 PDF 等）
├── SIGNALS.md                      本地 · 最新信号报告快捷副本
├── TODO.md                         本地 · 项目待办
├── .env                            本地 · 环境变量（settings.json deny 保护）
└── tmp/                            临时目录（settings.json 预授权读写编辑）
    └── scripts/                     临时脚本，用完即删


OneDrive/huaxin_quant/（云盘 · Huaxin Quant Git 仓库）
│
├── .git/                           Git 仓库
├── .gitignore
├── .claude/
│   └── settings.json               Claude Code 共享权限策略（Git 管理）
├── .obsidian/
├── CLAUDE.md
├── instructions/                   各模型指令卡
├── scripts/                        辅助 Python 脚本
└── dev_logs/                       开发复盘日志
```

## 开发约定

- **指令文件是源头**，脚本是指令的配套实现。改逻辑先改指令，再改脚本；脚本与指令同提交更新
- **指令卡保持精简**：只保留 LLM 执行所需内容（流程、规则、约束）。公式速查、报告模板、字段定义等放入配对 `*-ref.md`，按需查阅
- **固定文件名**：`01-pool.md`、`02-quant.md`、`03-valuation.md`、`03-valuation-ref.md`、`04-tracker.md`、`04-tracker-ref.md`
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
