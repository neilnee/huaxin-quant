# Huaxin Quant Workflow

本文档定义 Huaxin Quant 的日常执行流程。模型细节以 `instructions/` 和 `scripts/` 为准。

## 每日执行

一条命令启动全流程：

```bash
python3 scripts/daily.py &
```

带选项：

```bash
python3 scripts/daily.py --date 260709 &         # 指定日期
python3 scripts/daily.py --skip-pool &            # 复用已有池子，跳过模型一
python3 scripts/daily.py --force-refresh &        # 强制刷新数据缓存
```

启动后立即返回，不阻塞当前终端。流水线在后台按顺序执行：

```text
市场数据增量更新 → 模型一 Pool → 模型二 Quant → Bloom 信号 → Signal Plan → 信号财务提示 → 当天完整资金观测 → 市场/回测/VCP 页面发布 → 信号股资金补查与信号页面发布 → 产物完整性核验 → 打开本地面板 → 东方财富自选同步（可选）
```

日常市场发布必须启用市场 LLM 解读；`market_regime_<YYMMDD>.json` 中 `llm.status=success`、解读正文非空，且页面 `analysis_source=llm` 后才算完成。市场 LLM 首次失败时允许自动重跑市场发布，但必须复用已经生成的“今日盘面主线”，不得重复检索或改写主线结论。`--no-llm` 只用于显式历史回放和开发调试，不能进入每日完整工作流。

市场数据更新、完整资金观测、只读页面发布和自选集合重建属于可重入阶段，遇到瞬时失败时允许有限次数自动重试。Pool、Quant、Bloom、Signal Plan 会生成筛选结果或生命周期账本，不得由总控盲目重复执行；失败时必须以 `failed` 终态中止，等待明确续跑。任何关键阶段失败、市场 LLM 未成功或同日产物核验不完整时，总进度不得标记为 `done`。

## 查看进度

```bash
python3 scripts/monitor.py
```

monitor 每 2 秒刷新一次，进度记录写入 `.tmp/daily_progress_<YYMMDD>.md`：

- **运行中**：显示阶段状态 + 进度条（模型二含逐只股票进度）
- **完成后**：保留最终阶段结果，包含三类页面数据的日期一致性核验，monitor 退出

```bash
python3 scripts/monitor.py --date 260709         # 指定日期
python3 scripts/monitor.py --interval 1          # 调整轮询间隔（秒）
```

Ctrl+C 可随时退出 monitor，流水线继续在后台运行。重新连接：`python3 scripts/monitor.py`。

## ⚠️ 重要

**启动 daily.py 后不要在对话中持续汇报进度。** `daily.py &` 是非阻塞的，终端立即可用。想看进展时运行 `monitor.py`，不想看就做其他事。monitor 跑完自动停，进度记录在 `.tmp/daily_progress_<date>.md`。

## 手动运行（调试 / 单步）

```bash
python3 scripts/market_regime.py update --date 2026-07-09
python3 scripts/run_pool.py --date 2026-07-09
python3 scripts/quant_filter.py --date 260709 --pool pool/pool_260709.csv
python3 scripts/bloom.py --date 260709
python3 scripts/signal_plan.py --date 260709
python3 scripts/market_regime.py run --date 2026-07-09
python3 scripts/dashboard_vcp.py --date 260709
python3 scripts/dashboard_signals.py --date 260709
python3 scripts/sync_zixuan.py --date 260709 --yes
```

各模块也可独立运行，详见 `instructions/` 目录下各指令卡。

市场或板块状态规则升级后，使用隔离历史回放刷新派生环境，不重新运行 Quant、Bloom、Signal Plan 或 LLM：

```bash
python3 scripts/rebuild_market_history.py
python3 scripts/rebuild_market_history.py --publish
```

## 独立执行模型三估值分析

模型三按单只股票主动触发，不属于 `daily.py` 的每日固定链路，也不自动消费 Bloom。日常执行统一使用端到端总控 `run_valuation.py`；`valuate.py`、`valuation_pipeline.py`、`calc_valuation.py` 只作为内部阶段或开发调试入口。

### 运行前配置

项目 `.env` 至少需要：

```bash
MX_APIKEY=<东方财富妙想 API Key>
DEEPSEEK_API_KEY=<模型 API Key>
HUAXIN_MX_DATA_SCRIPT=/Users/neil/.claude/skills/mx-data/mx_data.py
```

总控只读取环境变量，不在日志和运行包中保存密钥。`HUAXIN_MX_DATA_SCRIPT` 未配置时，默认尝试 `~/.claude/skills/mx-data/mx_data.py`。

### 首次或正常执行

```bash
python3 scripts/run_valuation.py --code 300442 --name 润泽科技
```

`--name` 在本地 briefing 或估值索引已有有效名称时可省略。总控按以下顺序执行：

```text
财务缓存检查
  → 缺失/过期时调用 mx-data（三组标准财务查询）
  → 校验 raw JSON、证券代码和可解析字段
  → 阶段零 briefing
  → 最新有效交易日行情
  → 固定证据检索
  → 阶段一：业务与利润支柱
  → 动态专题检索
  → 阶段二：机构共识、可比与分歧
  → 阶段三：项目与预期差
  → 阶段四：催化剂与验证节点
  → 阶段五：研究卡与参数映射
  → 三情景估值计算
  → Markdown、CSV 与 Dashboard 发布
```

同一股票同一时间只允许一个总控任务。终端中断时总控会终止当前子进程；异常退出遗留的失效 PID 锁会在下次启动时自动回收。

### 执行前预览

```bash
python3 scripts/run_valuation.py --code 300442 --name 润泽科技 --dry-run
```

`--dry-run` 只检查配置、缓存和恢复计划，并打印将执行的 mx-data、阶段零和主流水线命令，不联网、不调用 LLM、不创建估值运行包。首次覆盖新股票时建议先执行一次。

### 数据刷新

```bash
# 强制刷新财务、briefing、固定检索和动态专题证据
python3 scripts/run_valuation.py --code 300442 --name 润泽科技 --refresh

# 只强制刷新财务，然后继续完整估值流程
python3 scripts/run_valuation.py --code 300442 --name 润泽科技 --refresh-financial

# 禁止调用 mx-data，只允许使用未过期的本地财务缓存
python3 scripts/run_valuation.py --code 300442 --skip-financial-fetch

# 已确认旧财报仍是当前有效口径时显式放行
python3 scripts/run_valuation.py --code 300442 --skip-financial-fetch --allow-stale-financial
```

默认财务缓存有效期为 90 天，检索缓存有效期为 24 小时，可分别用 `--financial-max-age-days` 和 `--search-cache-hours` 调整。`--refresh` 会产生新的妙想和 LLM/API 调用，只在确需更新证据时使用。

### 查看状态与断点续跑

```bash
# 查看最近五次运行及失败原因
python3 scripts/run_valuation.py --code 300442 --status

# 自动选择最新未完成运行包，并判断普通续跑、阶段五重跑或研究卡修复
python3 scripts/run_valuation.py --code 300442 --resume

# 续跑时完全禁止联网，只复用运行包证据和已有成功缓存
python3 scripts/run_valuation.py --code 300442 --resume --no-fetch-evidence

# 显式指定运行包
python3 scripts/run_valuation.py --code 300442 \
  --resume-run cache/valuation_runs/300442_<timestamp>
```

续跑固定使用运行包自己的 `briefing.json` 和证据快照，不重新执行阶段零，也不会因工作区财务缓存后来过期而改变原研究边界。自动恢复无法解决真实证据不足；少于三家有效双年机构预测等门禁仍会明确失败，需要补充证据后再运行。

### 只研究计算、不正式发布

```bash
python3 scripts/run_valuation.py --code 300442 --no-publish
```

该模式仍生成完整运行包、研究卡、参数和计算结果，但不覆盖正式 Markdown、估值索引、排名和 Dashboard。适合新规则验证或人工审阅。

### 估值产物与日志

| 类型 | 路径 | 说明 |
|------|------|------|
| 运行包 | `cache/valuation_runs/<code>_<timestamp>/` | briefing、证据、各阶段 JSON、LLM trace、研究卡、参数、计算结果和 manifest |
| 总控摘要 | `<运行包>/controller_summary.json` | 财务动作、恢复策略、实际命令、终态和失败原因 |
| 总控日志 | `.tmp/valuation_controller/<code>_<timestamp>.log` | mx-data、阶段零和主流水线的合并输出 |
| 正式报告 | `reports/valuation/<code>_<name>.md` | 模型三研究报告 |
| 估值索引 | `reports/indexes/valuation_index.csv` | 阶段和研究状态 |
| 估值排名 | `reports/indexes/valuation_ranking.csv` | 三情景估值、安全边际和优先级 |
| Dashboard | `dashboard/data/<YYYYMM>/valuation_context_<YYMMDD>.js` | 按日估值数据包 |

总控最终必须同时满足子进程退出码、`manifest.status=done`、核心运行包文件完整，以及发布模式下报告和 Dashboard 均成功，才会返回成功。详细研究规则、证据门禁和修复参数见 `instructions/03-valuation.md`。

## 产物关系

| 层级 | 文件 | 说明 |
|------|------|------|
| 模型一 | `pool/pool_<YYMMDD>.csv` | 基本面候选池 |
| 模型二 | `quant/quant_<YYMMDD>.csv` | 单日量价结构 |
| 模型二 | `cache/quant_runs/quant_<YYMMDD>.json` | 结构化结果（Bloom / Plan 输入） |
| Bloom | `bloom/bloom_<YYMMDD>.md` | Bloom 日报 |
| Bloom | `bloom/state/bloom_state.csv` | 跨日状态表 |
| Bloom | `bloom/state/bloom_events.jsonl` | 事件流水 |
| Plan | `signal_plan/signal_plan_<YYMMDD>.json` | 买点计划结构化数据 |
| Plan | `signal_plan/signal_plan_<YYMMDD>.md` | 买点计划日报 |
| 模型三 | `cache/valuation_runs/<code>_<timestamp>/` | 单股估值可审计运行包 |
| 模型三 | `reports/valuation/<code>_<name>.md` | 单股估值研究报告 |
| 面板 | `dashboard/data/<YYYYMM>/*.js` | 市场环境、VCP 结构、信号发现及按需估值数据包 |

## 首次初始化

```bash
cp .env.example .env
python3 scripts/init_runtime.py
```

## 异常处理

- 数据更新、Pool、Quant、Bloom、Signal Plan、完整资金观测、页面发布或产物完整性核验在允许的有限重试后仍失败 → 流水线以 `failed` 终态中止，避免下游使用过期产物
- 市场 LLM 状态不是 `success`、正文为空或页面没有标记 `analysis_source=llm` → 市场发布未完成；总控复用既有盘面主线重试，仍失败则中止
- 完整资金观测达到请求预算或部分数据源失败时允许以 `partial` 产物继续，但同日资金归档、Dashboard 数据包和资金日期索引不得缺失；信号页面必须确认已启用信号股资金补查。
- `daily.py` 不带 `--date` 时，按 15:00 分界线选择预期最近交易日；带 `--date` 时，所有阶段和三类页面数据包均使用该日期，可用于按日回补。
- Bloom LLM 调用失败（退出码 3）→ 保留规则产物，继续执行后续阶段
- 模型三的 mx-data、证据检索、LLM、研究卡或参数门禁失败 → 本次估值显式失败并保留运行包，使用 `--status` 查看原因、`--resume` 断点续跑；不得用规则兜底补写研究事实
- 浏览器无法自动启动 → 不影响已生成的页面数据，可手动打开 `dashboard/index.html`
- 数据异常 → Bloom 标记 `DATA_ISSUE`，不删除候选

## 维护原则

- 改模型规则：先改 `instructions/*.md`，再改 `scripts/*.py`。
- 本地数据产物不提交 Git：`cache/`、`pool/`、`quant/`、`bloom/`、`signal_plan/`、`tracker/`、`reports/`、`.tmp/`。
- 提交只包含源文件、指令卡和文档。
