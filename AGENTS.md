# AGENTS.md

## 项目概要

Huaxin Quant，多模型流水线的股票花期发现与跟踪系统。Codex 在本项目中负责阅读指令卡、维护配套脚本、执行数据流水线、验证输出结果，并在需要时提交代码。

项目采用双目录架构：

- `/Users/neil/ai/quant_lab`：本地工作区，作为 Huaxin Quant 的运行实例，负责日常运行、缓存、输出、报告。
- `/Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant`：云盘 Git 仓库，负责 Huaxin Quant 源代码、指令卡和开发文档版本管理。

`instructions/`、`scripts/`、`CLAUDE.md`、`dev_logs/` 在本地工作区中是指向云盘仓库的软链。缓存和输出目录只存在于本地工作区，默认不进 Git。

## 核心规则

### 1. Git 操作必须走云盘路径

本地工作区没有 `.git`。所有 Git 命令必须显式使用云盘仓库路径：

```bash
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant status
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant diff
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant add <file>
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant commit -m "..."
```

禁止在 `/Users/neil/ai/quant_lab` 直接执行普通 `git status`、`git diff`、`git add`、`git commit`。

### 2. 源文件走 Git，数据产物不提交

纳入 Git 的内容：

- `CLAUDE.md`
- `AGENTS.md`
- `instructions/*.md`
- `scripts/*.py`
- `dev_logs/`
- 必要的项目配置和开发文档

默认不纳入 Git 的内容：

- `cache/`
- `pool/`
- `quant/`
- `reports/`
- `signals/`
- `refer/`
- `.env`
- `tmp/`

运行模型产生的 CSV、JSON、PKL、报告文件只作为本地结果使用，除非用户明确要求提交。

### 3. 改规则先改指令卡，再改脚本

每个模型由 `instructions/` 下的指令卡定义规则，`scripts/` 下的脚本负责稳定执行。

修改筛选逻辑时顺序如下：

1. 更新对应指令卡，说明规则、阈值、字段口径和输出结构。
2. 更新配套脚本。
3. 运行语法检查和必要的脚本验证。
4. 对结果做摘要说明。
5. 代码和指令卡同一次提交。

不要只改脚本不改指令卡，也不要只改指令卡不更新脚本。

### 4. 固定指令卡文件名

当前已改为 Git 管理历史，不再靠文件名版本号管理。

活跃指令卡只保留：

- `instructions/01-pool.md`
- `instructions/02-quant.md`
- `instructions/03-valuation.md`
- `instructions/03-valuation-ref.md`
- `instructions/04-tracker.md`
- `instructions/04-tracker-ref.md`

禁止再新增 `*-dev.md`、`*-vX.Y.md` 或 `archive/` 版本副本。

### 5. 临时脚本放 `tmp/`

需要临时分析或一次性脚本时，统一写到：

```text
tmp/scripts/
```

优先使用项目已有脚本和标准命令。临时脚本用完后清理 `tmp/scripts/`，保留 `tmp/` 目录本身。

Codex 执行时优先用 `rg`、`sed`、`python3 -m py_compile`、项目脚本等稳定命令。不要用 ad-hoc 命令污染项目根目录。

## 目录职责

```text
quant_lab/  # Huaxin Quant 本地运行实例
├── instructions/      -> 云盘仓库，模型指令卡
├── scripts/           -> 云盘仓库，模型执行脚本
├── CLAUDE.md          -> 云盘仓库，Claude 工程规范
├── AGENTS.md          -> Codex 工程规范
├── dev_logs/          -> 云盘仓库，开发复盘日志
├── cache/             本地缓存
├── pool/              模型一输出
├── quant/             模型二输出
├── reports/           模型三报告
├── signals/           模型四信号跟踪
├── refer/             本地参考资料
├── tmp/               临时脚本和临时文件
└── .env               本地密钥配置，不入 Git
```

## 模型流水线

### 模型一：股票池初筛

入口：

```bash
python3 scripts/run_pool.py
```

常用参数：

```bash
python3 scripts/run_pool.py --force-refresh
python3 scripts/run_pool.py --skip-fetch
python3 scripts/run_pool.py --no-process
python3 scripts/run_pool.py --dry-run
```

职责：

- 阶段一：调用 mx-xuangu 分段拉取基础股票池。
- 阶段二到五：调用 `process_pool.py` 做硬过滤、行业排除、软标签、CSV 输出和摘要。

模型一当前硬规则包括市值 `>= 50亿`、净利润周期兼容、OCF/NP、负债率、毛利率、行业排除等。

### 模型二：VCP/P2/P3 量价分析

入口：

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
```

单股分析：

```bash
python3 scripts/quant_filter.py --code 300604 --name 长川科技
```

职责：

- 拉取或复用日线行情缓存。
- 计算均线、斜率、波动收缩、缩量、涨跌幅、位置和风险标记。
- 输出 `P1_FORMING`、`P1_TIGHT`、`P1_HIGH`、`P2_PULLBACK`、`P3_RETEST`、`REJECT`。
- LLM 只允许做可选解释，不参与 P1/P2/P3 判定。

### 模型三：估值分析

入口脚本以当前指令卡为准，核心逻辑在 `scripts/valuate.py`、`scripts/calc_valuation.py`。

职责：

- 获取财务、公告、搜索、研报等信息。
- 形成估值参数。
- 用估值引擎计算未来年度估值锚点。
- 输出报告和排名表。

估值逻辑必须明确说明收入、利润、估值倍数和年度预测假设，禁止不解释地线性外推。

### 模型四：择时跟踪

入口脚本以 `instructions/04-tracker.md` 为准，核心逻辑在 `scripts/tracker.py`。

职责：

- 维护自选股和监控池。
- 跟踪交易信号、仓位状态和风控状态。
- 输出每日信号报告。

## 缓存和数据口径

| 目录 | 用途 | 规则 |
|---|---|---|
| `cache/xuangu/` | 模型一选股 raw 数据 | 默认 5 天有效，超期由脚本跳过 |
| `cache/daily/` | 模型二/四日线缓存 | 文件名含日期，跨天自动不命中 |
| `cache/financial/` | 模型三财务缓存 | 同一报告期内可复用 |
| `cache/calc_params/` | 估值引擎输入参数 | 按日期和股票保存 |
| `cache/calc_results/` | 估值引擎输出结果 | 按日期和股票保存 |

数据口径必须在指令卡和脚本中保持一致。模型一和模型三的财务报告期选择尤其要谨慎，避免“模型一通过、模型三发现亏损”的跨模型冲突。

## 权限和配置

Claude 权限配置参考：

```text
.claude/settings.local.json
```

Codex 权限配置参考：

```text
/Users/neil/.codex/config.toml
```

当前 Codex 项目配置应保持：

```toml
[projects."/Users/neil/ai/quant_lab"]
trust_level = "trusted"
writable_roots = [
  "/Users/neil/ai/quant_lab",
  "/Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant",
]
```

Claude 和 Codex 的权限语法不同，不要求逐字一致，但能力要保持等价：

- 本地工作区可读写。
- 云盘 Git 仓库可读写。
- 常用项目脚本可以执行。
- 金融数据 skill 或底层脚本可调用。
- `.env` 不提交、不展示密钥。

涉及网络、跨目录写入、删除、重置仓库、安装依赖等高风险操作时，按 Codex 当前权限模型请求确认。

## 代码编辑规范

- 优先遵循现有脚本风格，不引入无必要的新框架。
- 手工编辑文件使用 `apply_patch`。
- 不用 Python 写文件，除非是批量机械转换且比补丁更安全。
- 默认 ASCII；中文文档和既有中文文件可继续使用中文。
- 注释只解释不直观的业务规则或兼容逻辑。
- 不做无关重构。
- 不回滚用户或其他工具产生的改动。

## 验证规范

常用验证：

```bash
python3 -m py_compile scripts/*.py
python3 scripts/run_pool.py --dry-run
python3 scripts/run_pool.py --skip-fetch
python3 scripts/quant_filter.py --code 300604 --name 长川科技
```

真实拉取数据会访问外部接口，若当前沙箱无网络权限，按 Codex 权限流程请求联网执行。

验证结果要说明：

- 输入文件和输入标的数量。
- 输出文件路径。
- 成功、失败、跳过数量。
- 关键分层统计。
- 发现的数据缺口或字段兼容问题。

## 提交规范

提交前：

```bash
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant status --short
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant diff --stat
```

确认只提交源文件、指令卡和必要文档，不提交本地数据产物或密钥。

提交信息使用简洁英文动词前缀：

```text
feat: ...
fix: ...
docs: ...
chore: ...
```

提交后再检查工作区是否干净：

```bash
git -C /Users/neil/Library/CloudStorage/OneDrive-个人/huaxin_quant status --short
```

## 与用户沟通

- 先读代码和指令卡，再判断实现。
- 能执行就直接执行，不停留在建议层。
- 长任务中定期说明正在做什么、发现了什么。
- 最终回复只讲关键结果、验证情况和残留风险。
- 不暴露 API Key、`.env` 内容或其他敏感配置。
