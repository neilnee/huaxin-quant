# Huaxin Quant Workflow

本文档定义 Huaxin Quant 的日常执行顺序和产物流转。模型细节以 `instructions/` 和 `scripts/` 为准。

## 目标

每天完成三件事：

1. 从基本面合格池中识别正在形成 VCP 形态的股票。
2. 通过 Bloom 信号层记录候选状态的跨日变化。
3. 阅读 Bloom 日报，辅助人工决定后续观察、估值或跟踪。

## 主流程

```text
模型一 Pool
  → 模型二 Quant
  → Bloom 信号层
  → 人工复盘 Bloom 日报
  → 可选：估值 / 自选股 / 持仓管理
```

## 首次初始化

```bash
cp .env.example .env
python3 scripts/init_runtime.py
```

## 每日执行

### 1. 生成股票池

```bash
python3 scripts/run_pool.py
```

产出：`pool/pool_<YYMMDD>.csv` — 模型二输入。

### 2. 运行量价筛选

```bash
python3 scripts/quant_filter.py --pool pool/pool_<YYMMDD>.csv
```

产出：

- `quant/quant_<YYMMDD>.csv` — 单日量价结构
- `cache/quant_runs/quant_<YYMMDD>.json` — 结构化结果（Bloom 输入）

### 3. 运行 Bloom 信号层

```bash
python3 scripts/bloom.py
python3 scripts/bloom.py --date 260705
```

产出：

- `bloom/bloom_<YYMMDD>.md` — Bloom 日报
- `bloom/state/bloom_state.csv` — 机器状态表
- `bloom/state/bloom_events.jsonl` — 事件流水
- `bloom/state/bloom_input_<YYMMDD>.json` — 结构化输入

### 4. 阅读 Bloom 日报

打开 `bloom/bloom_<YYMMDD>.md`，重点看：

1. **🔥 重点观察** — FORMING 及以上结构的标的，风险行内标记
2. **📋 池子变化** — 新进入和移出的标的
3. **📖 字段说明** — 枚举速查（文末）

## 产物关系

| 层级 | 文件 | 作用 |
|------|------|------|
| 模型一 | `pool/pool_<YYMMDD>.csv` | 基本面候选池 |
| 模型二 | `quant/quant_<YYMMDD>.csv` | 单日量价结构 |
| 模型二 | `cache/quant_runs/quant_<YYMMDD>.json` | 结构化结果（Bloom 输入） |
| Bloom | `bloom/state/bloom_state.csv` | 当前观察状态 |
| Bloom | `bloom/state/bloom_events.jsonl` | 跨日状态变化 |
| Bloom | `bloom/bloom_<YYMMDD>.md` | 人工阅读日报 |

## 人工复盘顺序

优先看：

1. `TRIGGERED` 或 `RISK_BLOCKED` 的标的。
2. `FORMING` / `MATURE` 升级的标的。
3. 连续保持有效状态的标的。
4. `EXIT` 移出的标的。
5. `DATA_ISSUE` 但本来值得关注的标的。

人工动作：`watch` / `ignore` / `valuation` / `tracker` / `zixuan_add` / `zixuan_remove`

## 可选后续

### 模型三估值

只对人工确认值得深入研究的标的执行。

### 模型四持仓管理

只对已有估值锚点或明确交易计划的标的执行。

## 异常原则

- 当日行情接口未完全更新时，不强制清缓存。
- 数据异常不直接删除候选，Bloom 标记为 `DATA_ISSUE` 维持原状态。
- 批量结果异常时，先抽查单股，再决定是否重跑。

## 维护原则

- 改模型规则：先改对应 `instructions/*.md`，再改 `scripts/*.py`。
- 本地数据产物不提交 Git：`cache/`、`pool/`、`quant/`、`bloom/`、`signals/`、`reports/`。
- 提交只包含源文件、指令卡和必要文档。

## 常用验证

```bash
python3 -m py_compile scripts/*.py
python3 scripts/run_pool.py --dry-run
python3 scripts/quant_filter.py --code 300604 --name 长川科技
python3 scripts/bloom.py --date 260705
```
