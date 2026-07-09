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
模型一 Pool → 模型二 Quant → 模型四 Tracker（Bloom → Plan → 花期策览）
```

## 查看进度

```bash
python3 scripts/monitor.py
```

monitor 每 2 秒刷新一次，输出到 `tracker/花期策览_<YYMMDD>.md`：

- **运行中**：显示阶段状态 + 进度条（模型二含逐只股票进度）
- **完成后**：自动替换为完整合并报告，monitor 退出

```bash
python3 scripts/monitor.py --date 260709         # 指定日期
python3 scripts/monitor.py --interval 1          # 调整轮询间隔（秒）
```

Ctrl+C 可随时退出 monitor，流水线继续在后台运行。重新连接：`python3 scripts/monitor.py`。

## ⚠️ 重要

**启动 daily.py 后不要在对话中持续汇报进度。** `daily.py &` 是非阻塞的，终端立即可用。想看进展时运行 `monitor.py`，不想看就做其他事。monitor 跑完自动停，报告在 `tracker/花期策览_<date>.md`。

## 手动运行（调试 / 单步）

```bash
python3 scripts/run_pool.py --date 260709
python3 scripts/quant_filter.py --pool pool/pool_260709.csv
python3 scripts/bloom.py --date 260709
python3 scripts/signal_plan.py --date 260709
python3 scripts/tracker.py --date 260709
```

各模块也可独立运行，详见 `instructions/` 目录下各指令卡。

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
| **终** | **`tracker/花期策览_<YYMMDD>.md`** | **合并日报（最终阅读入口）** |

## 首次初始化

```bash
cp .env.example .env
python3 scripts/init_runtime.py
```

## 异常处理

- 模型二失败 → 流水线中止（无下游数据）
- Bloom / Plan 失败 → 继续执行，终末报告标注错误
- 数据异常 → Bloom 标记 `DATA_ISSUE`，不删除候选
- LLM 调用失败 → 不影响核心流程，自动回退规则兜底

## 维护原则

- 改模型规则：先改 `instructions/*.md`，再改 `scripts/*.py`。
- 本地数据产物不提交 Git：`cache/`、`pool/`、`quant/`、`bloom/`、`signal_plan/`、`tracker/`、`reports/`、`.tmp/`。
- 提交只包含源文件、指令卡和文档。
