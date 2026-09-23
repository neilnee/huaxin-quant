# CLAUDE.md

Huaxin Quant 多模型流水线的股票花期发现与跟踪系统。每个筛选模型对应 `instructions/` 下的一个指令文件，由 LLM 读取后自动执行。

项目采用**双目录架构**：本地工作区负责日常运行（Claude Code 工作目录），源码仓库负责 Git 版本管理。指令卡、脚本、配置等源文件可以通过软链在本地编辑，Git 在源码仓库侧追踪实体文件；数据产出（缓存、筛选结果、报告）和本地开发日志不跟 Git。

## ⚠️ 核心规则

### 规则一：Git 操作必须走云盘路径

本地目录没有 `.git`，所有 Git 操作使用 `git -C <云盘路径>`，**禁止**在本地目录直接执行 `git`。

### 规则二：临时脚本统一写到 `.tmp/` 目录

临时脚本统一放 `.tmp/scripts/`。完成后只清理本任务创建且已确认不再需要的文件，保留 `.tmp/` 目录；实际权限以当前运行环境为准。

### 规则三：脚本始终走本地 symlink 路径调用

`quant_lab/scripts/` 是 symlink，走这个路径调用 `PROJECT_ROOT` 自然指向本地工作区。走云盘真实路径会导致读不到数据目录。

### 规则四：Python 使用项目虚拟环境

使用 Python 3.11+，优先 `.venv/bin/python scripts/...`；以下 python3 示例须先 `source .venv/bin/activate`。不得使用 macOS 系统 Python 3.9。

### 规则五：研究记录与真实交易

投研协作、研究笔记与持仓维护统一遵循 [AGENTS.md](AGENTS.md) 第7、8节：保持专业、独立且有证据的判断，准确回应用户原意；判断有连续性，操作有适应性。研究先回顾原计划，讨论后按日合并证据与修正；持仓按券商基线加后续确认交易维护，缺失成交字段不得推断。

## 🧭 模块使用指南

> 以下只列常用命令，详细规则见对应指令卡。

### 每日一键流水线

```bash
python3 scripts/daily.py &        # 后台串行执行：数据更新 → Pool → Quant → Bloom → Signal Plan → 完整资金观测 → 信号股资金补查 → 回测/页面发布 → AI研读数据包 → 完整性核验 → 打开面板 → 可选自选同步
python3 scripts/monitor.py        # 前台查看进度 → .tmp/daily_progress_<date>.md
```

详细说明见 `WORKFLOW.md`。

### 模型一：海选初筛（Pool）

基本面核心质量池 + RS 强势扩展池 + 来源与软标签；不再整体排除行业。执行方式参考 `instructions/01-pool.md`。

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

优先消费策略库中的模型二结果（JSON 为兼容入口），维护跨日信号生命周期，LLM 解读重点观察标的。执行方式参考 `instructions/signal-bloom.md`。

```bash
python3 scripts/bloom.py [--date 260706]
```

### 持仓管理（Position）

独立账本：交易流水、当前持仓、每日状态快照。执行方式参考 `instructions/signal-position.md`。

```bash
python3 scripts/position.py add-trade --trade-date 2026-07-06 --code 688676 --name 金盘科技 --side BUY --shares 200 --price 83.89
python3 scripts/position.py rebuild --as-of 2026-07-06
```

### Signal Plan 信号层

基于模型二买点判定，生成次日量价触发计划（具体价格区间、量能条件、失效位）。执行方式参考 `instructions/signal-plan.md`。

```bash
python3 scripts/signal_plan.py [--date 260709]
```

### 策略回测

分别评价 VCP 首次入选和 Plan 次日兑现后的 5/10/20 日总回报；Plan A/REGULAR 不等于模型二 A/B/C/D，当前尚无独立的全部模型二信号评估集合。执行方式参考 `instructions/backtest.md`。

```bash
python3 scripts/backtest.py --date 260806
```

### 模型四：Tracker 总控

统一编排 Bloom + Signal Plan，生成合并日报。执行方式参考 `instructions/04-tracker.md`。

```bash
python3 scripts/tracker.py [--date 260709]
python3 scripts/tracker.py --skip-bloom    # 只跑 Signal Plan
python3 scripts/tracker.py --skip-plan     # 只跑 Bloom
```

### 模型三：深度估值（Valuation）

按需机构共识研究与三情景估值，默认使用 `run_valuation.py`；不由 Bloom 自动触发。执行方式参考 `instructions/03-valuation.md`。

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
| `signal_plan/` | Signal Plan 买点计划 + JSON | 本地 |
| `tracker/` | 合并日报 | 本地 |
| `reports/` | 估值报告 + indexes/ | 本地 |
| `position/` | 持仓账本 | 本地 |
| `.tmp/` | 临时脚本（预授权，用完即删） | 本地 |
| `.env` | 环境变量（不入 Git） | 本地 |

> 物理云盘路径不代表已纳入 Git；本实例 dashboard、daily_research、dev_logs 也可为软链，生成数据与人工记录仍默认忽略。

## 开发约定

- **指令文件是源头**，脚本是指令的配套实现。改逻辑先改指令，再改脚本；脚本与指令同提交更新
- **指令卡保持精简**：只保留 LLM 执行所需内容（流程、规则、约束）。公式速查、报告模板、字段定义等放入配对 `*-ref.md`，按需查阅
- **固定文件名**：模型主指令卡固定为 `01-pool.md`、`02-quant.md`、`03-valuation.md`、`03-valuation-ref.md`、`04-tracker.md`；模型四内部信号模块使用 `signal-` 前缀，如 `signal-bloom.md`、`signal-plan.md`、`signal-position.md`
- **分支开发**：较大改动在 Git feature 分支上直接修改活跃文件；稳定后 commit/merge 保留历史，不靠复制文件发版
- **收尾更新**：每次完成一组规则变更后，更新 `TODO.md` 勾掉已完成项
- **数据口径统一**：模型一和模型三使用相同报告期数据，避免跨模型数据口径不一致
- **Agent 执行后清理**：mx-search 等 skill 并行执行后可能遗留 `tmp_*/` 目录，每次批量估值或搜索完成后清理

## 自选股监控池

按 instructions/sync-zixuan.md 管理系统受管自选；本地受管账本限定可删除集合，保留“全部”分组中的手工自选。daily 仅在 ENABLE_ZIXUAN_SYNC=true 时执行；文档整理和只读审查不触发外部同步。

## 文档导航与当前实现边界

最近核对：2026-09-08。完整模块导航见 [docs/README.md](docs/README.md)，架构见 [DESIGN.md](DESIGN.md)，执行流程见 [WORKFLOW.md](WORKFLOW.md)，待实现方案见 [docs/IMPROVEMENT_ROADMAP.md](docs/IMPROVEMENT_ROADMAP.md)。

- daily.py 是每日总控；tracker.py 仅按需合并 Bloom 与 Signal Plan 报告。
- cache/strategy/strategy_data.sqlite 是 Quant/Plan/Bloom/兑现与生命周期权威存储，文件是兼容发布；真实交易仍由 Position 流水管理。
- market/、capital/、backtest/、signal_plan/、reports/ai_daily/ 均为运行产物；dashboard 页面源码入 Git，dashboard/data 生成包不入 Git。
- Position 已有账本，账户级风控、独立持仓监控、自动估值队列尚未落地。不得把 TODO 或改进路线图中的建议当作现行规则。
- 当前代码与文档描述不符时，先区分描述过期和规则变更；仅校正文档不更改策略版本，实际规则修改仍按指令卡→配置/脚本→验证执行。
