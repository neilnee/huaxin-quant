# 模型四：Tracker 总控指令卡

- **版本管理**: 由 Git 分支与提交历史管理
- **最近更新**: 2026-07-09
- **策略配置**: `strategies/04-tracker.json`
- **核心目标**: 模型四是多个独立信号模块的统一编排层。tracker 本身不做信号判断、不拉数据、不算指标，只负责调度子模块运行 + 生成合并日报。

---

## 一、架构

```text
模型四 Tracker 总控（tracker.py）
├── Bloom 信号层        → signal-bloom.md   （生命周期 + 信号质量 + 观察要点）
├── Signal Plan 层      → signal-plan.md    （次日量价触发计划）
├── Position 层         → signal-position.md （持仓账本，独立运行）
└── 估值触发层           → 待建
```

- **Bloom**：回答"看什么、为什么看"——跨日生命周期、信号质量、风险阻断、估值候选。
- **Signal Plan**：回答"什么价位动手"——次日收盘价触发区间、A/B 类量价条件、失效位。
- **Tracker**：调度上面两个模块顺序运行，交叉数据，输出一份合并日报。
- **Position**：独立账本，不与 tracker 耦合，后续整合。

## 二、执行

```bash
python3 scripts/tracker.py                   # 完整运行：Bloom + Signal Plan → 合并报告
python3 scripts/tracker.py --date 260709     # 指定日期
python3 scripts/tracker.py --skip-bloom      # 只跑 Signal Plan
python3 scripts/tracker.py --skip-plan       # 只跑 Bloom
```

tracker 内部流程：

```text
1. 定位 quant_<YYMMDD>.json
2. 调 Bloom  →  写 bloom_state / bloom_events / bloom_input / bloom_<date>.md
3. 调 Plan   →  写 signal_plan_<date>.json / signal_plan_<date>.md
4. 读两边 dict，交叉合并 → 写 tracker/tracker_<date>.md
```

tracker 不重复计算，不修改子模块逻辑。子模块仍可独立运行。

每日完整工作流 `scripts/daily.py` 在 Tracker 成功后额外执行自选重建：

```text
5. 调 sync_zixuan.py → 删除本地账本中的上一日工作流自选 → 写入 Bloom 重点观察和买点 → 目标校验
```

该步骤不属于 `tracker.py` 的独立运行范围；每日完整工作流仅在 `.env` 中 `ENABLE_ZIXUAN_SYNC=true` 时执行。该环境变量是唯一的启停开关。

## 三、合并报告

输出：`tracker/花期策览_<YYMMDD>.md`

| 分区 | 内容 |
|------|------|
| 📊 总览 | Bloom + Plan 的关键数字合并一行 |
| 🔥 重点标的 | Bloom 重点观察 ∩ Plan 有计划 → 交叉视图 |
| 🌸 Bloom 活跃观察 | 全量活跃标的摘要表，链接到完整 Bloom 报告 |
| 📐 Signal Plan 买点计划 | 三类买点摘要表，链接到完整 Plan 报告 |
| ⚠️ 风险与排除 | Bloom 风险阻断 + Plan 排除清单 |

合并报告的增量价值在交叉视图：一眼看到"该重点看的标的中，哪些明天就有具体触发区间"。

完整细节查阅各自的独立报告（`bloom/`、`signal_plan/`），合并报告是摘要。

## 四、输入

```text
cache/quant_runs/quant_<YYMMDD>.json          # 模型二 JSON（唯一上游输入）
bloom/state/bloom_state.csv                   # Bloom 跨日状态（由 Bloom 模块读写）
bloom/state/bloom_events.jsonl                # Bloom 事件流水
```

## 五、输出

```text
tracker/花期策览_<YYMMDD>.md                   # 合并日报
bloom/bloom_<YYMMDD>.md                       # Bloom 独立报告（由 Bloom 模块生成）
bloom/state/bloom_state.csv                   # Bloom 状态（由 Bloom 模块维护）
bloom/state/bloom_events.jsonl                # Bloom 事件（由 Bloom 模块维护）
bloom/state/bloom_input_<YYMMDD>.json         # Bloom 输入快照
signal_plan/signal_plan_<YYMMDD>.json         # Plan 结构化输出
signal_plan/signal_plan_<YYMMDD>.md           # Plan 独立报告
```

## 六、子模块独立执行

tracker 不替代子模块。需要单独运行某个模块时：

```bash
python3 scripts/bloom.py [--date 260709]
python3 scripts/signal_plan.py [--date 260709]
```

各自指令卡：`instructions/signal-bloom.md`、`instructions/signal-plan.md`。

## 七、验收标准

- tracker 不修改 Bloom、Plan、Position 的代码逻辑。
- tracker 不重新计算模型二的 VCP 阶段或买点信号。
- 合并报告只展示摘要，完整数据链到各自模块的报告文件。
- Bloom 和 Signal Plan 各自仍可独立运行，互不绑定。
- 交叉视图正确展示 Bloom 重点观察 ∩ Plan 有计划的标的。
- `--skip-bloom` / `--skip-plan` 可用，处理只有一侧数据的场景。
- tracker 退出码 3 = Bloom LLM 失败（与 Bloom 模块保持一致）。
