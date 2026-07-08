# Huaxin Quant

[English](README.en.md) | 中文

Huaxin Quant 是一个面向 A 股的多模型股票发现与跟踪系统。它把基本面海选、VCP 量价结构识别、Bloom 跨日信号跟踪、估值分析和持仓管理拆成可复现的脚本流水线，用于发现“基本面达标、结构逐步成熟、买点可量化”的候选股票。

项目以确定性脚本为核心：`instructions/` 定义模型规则和执行约束，`strategies/` 保存可调参数，`scripts/` 负责数据拉取、缓存、计算、输出和状态维护。LLM 只用于解释和报告增强，不参与核心结构和买点判定。

> 本项目用于研究和复盘，不构成投资建议。

## 核心策略

### 模型一：Pool 选股

- 从全市场 A 股中做基本面初筛，排除 ST、50 亿以下市值和明显不符合质量底线的公司。
- API 初筛关注营收增长、归母净利润增长、毛利率和研发投入比例。
- 自算硬过滤覆盖上市时长、净利润规模、经营现金流质量、资产负债率和毛利率底线。
- 行业侧使用黑名单做减法，排除金融、地产、强周期、公用事业、消费低弹性和医药等不符合当前策略偏好的方向。
- 软标签只用于排序和复核，不直接剔除通过硬过滤的股票。

### 模型二：Quant 结构与买点

- 在模型一候选池中识别 VCP 结构阶段：`VCP_EARLY`、`VCP_FORMING`、`VCP_MATURE`、`VCP_TIGHT`。
- VCP 结构关注多轮收缩、波幅递减、量能趋势、pivot 距离、趋势状态和结构有效期。
- 三类买点：
  - `PULLBACK_BUY`：结构内缩量回踩，偏早期和轻仓观察。
  - `BREAKOUT_BUY`：枢轴突破参与，避免直接突破后完全踏空。
  - `RETEST_BUY`：突破后回踩确认，确定性最高但不一定每天出现。
- 买点评分由结构基础分、动作类型分、动作质量分和风险修正组成，最终映射为 A/B/C/D 级。
- 风险标记会降低买点质量或阻断买点，例如跌破趋势、短期过热、长上影、放量滞涨等。

### 模型三：Valuation 估值

- 对重点候选标的做业务拆分和估值锚定。
- 脚本负责 DCF、PE 等确定性计算；LLM 可用于业务线解释和报告生成。
- 输出估值报告、估值区间和排序索引，供后续仓位和优先级判断使用。

### Bloom 信号层

- 消费模型二 JSON，维护跨日状态池和生命周期事件。
- 将模型二结构映射为 `EARLY`、`FORMING`、`MATURE`、`TRIGGERED`、`RISK_BLOCKED`、`COOLDOWN`、`EXIT` 等状态。
- 重点观察列表优先展示已触发买点的标的，其次按结构阶段和结构分排序。
- Bloom 报告会展示结构、买点、风险、当前 VCP 收缩组和 LLM 观察要点。

### 持仓管理

- 独立维护本地交易流水、持仓快照和重建后的账本。
- 支持初始化、导入成交、追加交易、按日期重建持仓。
- 持仓模块不直接决定买卖，只提供执行记录和后续复盘依据。

## 开发环境

### 要求

- Python 3.10+
- macOS / Linux shell 环境
- `requirements.txt` 中的 Python 依赖
- 可访问的金融数据接口或本地数据工具
- 可选：DeepSeek 兼容 API，用于 Bloom/估值报告中的 LLM 解读

安装依赖：

```bash
python3 -m pip install -r requirements.txt
```

初始化运行目录：

```bash
python3 scripts/init_runtime.py
```

检查脚本语法：

```bash
python3 -m py_compile scripts/*.py
```

### 环境变量

复制示例文件：

```bash
cp .env.example .env
```

常用变量：

```text
MX_APIKEY=
HUAXIN_XUANGU_SCRIPT=/path/to/mx_xuangu.py
DEEPSEEK_API_KEY=
DEEPSEEK_MODEL=deepseek-v4-flash
DEEPSEEK_BASE_URL=https://api.deepseek.com
```

- `MX_APIKEY`：模型二、Bloom/Tracker 等行情拉取的主数据源密钥。
- `HUAXIN_XUANGU_SCRIPT`：模型一调用选股工具时使用。
- `DEEPSEEK_*`：可选，启用 LLM 解读和报告增强时使用。

## 常用命令

```bash
# 模型一：基本面候选池
python3 scripts/run_pool.py
python3 scripts/run_pool.py --skip-fetch

# 模型二：VCP 结构与买点
python3 scripts/quant_filter.py
python3 scripts/quant_filter.py --date 260707
python3 scripts/quant_filter.py --code 300604 --name 长川科技

# Bloom：跨日信号池和观察报告
python3 scripts/bloom.py
python3 scripts/bloom.py --date 260707

# 模型三：估值
python3 scripts/valuate.py
python3 scripts/valuate.py --code 300442

# 持仓管理
python3 scripts/position.py init
python3 scripts/position.py rebuild --as-of 2026-07-06
```

## 项目结构

```text
instructions/      模型指令卡和规则说明
strategies/        策略参数 JSON
scripts/           流水线脚本和共享工具
scripts/data/      行情和数据源适配
WORKFLOW.md        日常执行工作流
DESIGN.md          系统设计说明
AGENTS.md          Codex 工程规范
CLAUDE.md          Claude 工程规范
```

运行时目录默认不提交到 Git：

```text
cache/             日线、选股、估值等本地缓存
pool/              模型一输出
quant/             模型二输出
bloom/             Bloom 报告、状态池和事件
reports/           估值和其他报告
position/          本地持仓账本
dev_logs/          本地开发复盘日志
.tmp/              临时脚本和临时文件
```

## 核心模块

| 模块 | 入口脚本 | 说明 |
|------|----------|------|
| Pool | `scripts/run_pool.py` | 拉取和处理基本面候选池 |
| Quant | `scripts/quant_filter.py` | 识别 VCP 结构、风险和买点 |
| Bloom | `scripts/bloom.py` | 维护跨日观察池并生成日报 |
| Valuation | `scripts/valuate.py` | 生成估值报告和估值排序 |
| Tracker | `scripts/tracker.py` | 对核心池进行择时信号检查 |
| Position | `scripts/position.py` | 管理交易流水和持仓账本 |
| Shared Data | `scripts/shared.py` | 日线缓存、日期口径、数据源降级 |

详细策略请看：

- [模型一 Pool](instructions/01-pool.md)
- [模型二 Quant](instructions/02-quant.md)
- [模型三 Valuation](instructions/03-valuation.md)
- [Bloom 信号层](instructions/signal-bloom.md)
- [持仓管理](instructions/signal-position.md)
- [交易策略文档](instructions/trading-strategy.md)
