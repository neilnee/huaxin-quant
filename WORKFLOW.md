# Huaxin Quant Workflow

本文档只定义 Huaxin Quant 的日常执行顺序和产物流转。模型细节以 `instructions/` 和 `scripts/` 为准，工作流不重复实现规则。

## 目标

每天完成三件事：

1. 从基本面合格池中识别正在形成形态的股票。
2. 记录候选状态的连续变化，而不是只看单日结果。
3. 用 LLM 生成简短复盘，辅助人工决定后续观察、估值或跟踪。

## 主流程

```text
模型一 Pool
  -> 模型二 Quant
  -> Bloom 状态层
  -> LLM Daily Review
  -> 人工复盘
  -> 可选：估值 / 自选股 / 择时跟踪
```

当前优先级是前四步：`Pool -> Quant -> Bloom -> Daily Review`。

## 每日执行

### 1. 生成股票池

```bash
python3 scripts/run_pool.py
```

产出：

- `pool/pool_<YYMMDD>.csv`

用途：

- 作为模型二输入。
- 回答“哪些股票基本面和行业条件值得继续看”。

### 2. 运行量价筛选

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
```

产出：

- `quant/quant_<YYMMDD>.csv`
- `cache/quant_runs/*.json`

用途：

- 判断单日技术状态。
- 输出 `P1_FORMING`、`P1_TIGHT`、`P1_HIGH`、`P2_PULLBACK`、`P3_RETEST`、`REJECT`。

### 3. 更新 Bloom 状态并生成复盘

```bash
python3 scripts/daily_review.py --date <YYMMDD>
```

产出：

- `bloom/bloom_state.csv`
- `bloom/bloom_events.jsonl`
- `cache/reviews/review_input_<YYMMDD>.json`
- `reports/daily/review_<YYMMDD>.md`

用途：

- 把模型二的单日判断转为跨日观察状态。
- 识别新增、延续、升级、降级、失效和数据异常。
- 生成当日人工复盘入口。

## 产物关系

| 层级 | 文件 | 作用 |
|---|---|---|
| 模型一 | `pool/pool_<YYMMDD>.csv` | 基本面候选池 |
| 模型二 | `quant/quant_<YYMMDD>.csv` | 单日量价状态 |
| 状态层 | `bloom/bloom_state.csv` | 当前观察状态 |
| 事件层 | `bloom/bloom_events.jsonl` | 跨日状态变化 |
| 复盘层 | `reports/daily/review_<YYMMDD>.md` | LLM 每日摘要 |

`quant/` 是当天截面，`bloom/` 是过程记录，`reports/daily/` 是人工阅读入口。

## 人工复盘顺序

优先看：

1. 新进入 `P1_TIGHT` 或 `P3_RETEST` 的标的。
2. 从 `P1_FORMING` 升级的标的。
3. 连续保持有效状态的标的。
4. 放量、破位、降级或失效的标的。
5. 数据异常但本来值得关注的标的。

人工动作只做标记或后续任务安排：

- `watch`：继续观察。
- `ignore`：暂不关注。
- `valuation`：进入模型三估值。
- `tracker`：进入模型四择时跟踪。
- `zixuan_add` / `zixuan_remove`：人工确认后同步自选股。

## 可选后续

### 模型三估值

只对人工确认值得深入研究的标的执行。

主要产出：

- `reports/valuation/`
- `reports/indexes/valuation_index.csv`
- `reports/indexes/valuation_ranking.csv`

### 模型四择时

只对已有估值锚点或明确交易计划的标的执行。

主要产出：

- `signals/`
- `SIGNALS.md`

## 异常原则

- 当日行情接口未完全更新时，不强制清缓存。
- 已成功拉到当日数据的标的自然复用。
- 未拉到最新数据的标的允许后续重试。
- 数据异常不直接删除候选，先在复盘中标记。
- 批量结果异常时，先抽查单股，再决定是否重跑。

## 维护原则

- 改模型规则：先改对应 `instructions/*.md`，再改 `scripts/*.py`。
- 改工作流顺序：更新本文档和对应脚本入口。
- 本地数据产物不提交 Git：`cache/`、`pool/`、`quant/`、`bloom/`、`reports/`、`signals/`。
- 提交只包含源文件、指令卡和必要文档。

## 常用验证

```bash
python3 -m py_compile scripts/*.py
python3 scripts/run_pool.py --dry-run
python3 scripts/quant_filter.py --code 300604 --name 长川科技
python3 scripts/daily_review.py --date <YYMMDD>
```
