# Signal Bloom：模型四 Bloom 信号层指令卡

- **版本管理**: 由 Git 分支与提交历史管理
- **最近更新**: 2026-07-05
- **所属模型**: 模型四 Tracker
- **策略配置**: `strategies/04-bloom.json`
- **核心目标**: 对模型二发现的股票进行信号质量判断和跨日生命周期跟踪，输出观察状态、风险阻断、估值候选和下一步观察点。

---

## 一、职责边界

Bloom 信号层只做信号判断，不做估值、不做持仓管理、不输出最终交易动作。

Bloom 负责：

- 消费模型二结构化输出。
- 维护 Bloom 候选池状态。
- 判断结构信号是增强、触发、风险阻断、冷却还是移出。
- 判断是否值得送入后续估值触发层。
- 输出人类可读的每日 Bloom 信号报告。

Bloom 不负责：

- 重新识别 VCP 形态。
- 修改模型二 `structure_stage` / `setup_signal`。
- 判断估值安全边际。
- 管理已有持仓。
- 给出最终买入、卖出、仓位建议。

---

## 二、输入

Bloom 以模型二 JSON 为权威输入：

```text
cache/quant_runs/quant_<YYMMDD>.json
```

核心消费字段：

```text
code
name
model2_include
structure_type
structure_stage
setup_signal
action_hint
suggested_position
structure_score
structure_risk_score
structure_risk_flags
support_price
invalid_price
breakout_level
pivot_distance
contraction_count
contraction_pcts
contraction_days
volume_pattern
volume_dry_up
close
MA20
MA60
distance_ma20
chg_5
chg_20
vol_ratio / 量比
structure_valid
structure_invalid_reason
reason
```

Bloom 可读取自身历史状态：

```text
bloom/state/bloom_state.csv
bloom/state/bloom_events.jsonl
```

入池口径：

- Bloom 消费模型二全量 JSON，但新建 Bloom 状态只允许 `model2_include=true` 的标的。
- 如果标的已存在于 `bloom_state.csv`，即使当日 `model2_include=false` 或 `action_hint=REJECT`，也必须继续更新生命周期，用于判断 `COOLDOWN`、`INVALID` 或 `EXIT`。
- 如果标的此前不在 Bloom 状态表，且当日 `model2_include=false`，不得新建 Bloom 状态，避免无效标的污染观察池。

---

## 三、叠加量化指标

Bloom 不重新计算模型二，但会使用模型二已输出或可直接读取的量化指标做信号解释。

| 指标 | 用途 |
|------|------|
| `close` | 判断当前价格位置 |
| `MA20` / `MA60` | 判断是否靠近支撑、是否偏离趋势 |
| `distance_ma20` | 判断是否追高或接近合理观察位 |
| `volume_dry_up` | 判断缩量是否健康 |
| `chg_5` / `chg_20` | 判断短期是否过热 |
| `vol_ratio` / `量比` | 判断放量、滞涨或突破风险 |
| `pivot_distance` | 判断距离 pivot 的观察价值 |
| `score_change` | 判断结构质量相对昨日改善还是恶化 |
| `days_tracked` / `days_since_active` | 判断保留、冷却和移出 |

如模型二未输出某个指标，Bloom 可以留空或降级判断，但不得自造数据。

---

## 四、状态枚举

`bloom_status` 统一使用大写枚举：

| bloom_status | 含义 |
|--------------|------|
| `EARLY` | 早期结构，低优先级观察 |
| `FORMING` | 结构形成中，正常观察 |
| `MATURE` | 结构成熟或紧致，重点观察 |
| `TRIGGERED` | 模型二出现 `PULLBACK_BUY` / `RETEST_BUY` |
| `RISK_BLOCKED` | 结构存在，但当前风险过高 |
| `COOLDOWN` | 模型二临时出局，仍在观察保留期 |
| `INVALID` | 结构失效或等待重建 |
| `EXIT` | 移出 Bloom 池 |
| `DATA_ISSUE` | 数据异常，暂不改变长期判断 |

状态基础映射：

| 模型二字段 | Bloom 状态 |
|------------|------------|
| `setup_signal=PULLBACK_BUY` | `TRIGGERED` |
| `setup_signal=RETEST_BUY` | `TRIGGERED` |
| `structure_stage=VCP_TIGHT` | `MATURE` |
| `structure_stage=VCP_MATURE` | `MATURE` |
| `structure_stage=VCP_FORMING` | `FORMING` |
| `structure_stage=VCP_EARLY` | `EARLY` |
| `structure_stage=POST_BREAKOUT` | `COOLDOWN` |
| `structure_stage=TREND_REBUILD` | `INVALID` |
| `structure_valid=false` | `INVALID` |
| `structure_stage=DATA_ISSUE` | `DATA_ISSUE` |
| 已在 Bloom 状态表中的 `model2_include=false` / `action_hint=REJECT` | `COOLDOWN` 或 `EXIT` |

风险阻断优先级高于普通观察状态。若结构状态为 `FORMING` / `MATURE` / `TRIGGERED`，但触发高风险规则，则输出 `RISK_BLOCKED`。

---

## 五、信号枚举

`bloom_signal` 统一使用大写枚举：

| bloom_signal | 判断依据 |
|--------------|----------|
| `NEW_ENTRY` | 昨日不在 Bloom 池，今日进入有效观察状态 |
| `UPGRADE` | 状态等级上升，或结构分明显改善 |
| `DOWNGRADE` | 状态等级下降，或结构分明显恶化 |
| `SETUP_TRIGGER` | `setup_signal=PULLBACK_BUY/RETEST_BUY` |
| `RISK_BLOCK` | 高风险分或硬风险标记触发 |
| `COOLDOWN` | 当日不再满足模型二观察条件，但仍在保留期 |
| `EXIT` | 连续无效或结构失效超过规则，移出 Bloom 池 |
| `DATA_HOLD` | 数据异常，维持原状态，不做升级或移出 |
| `CONTINUED` | 状态延续，无明显变化 |

状态等级：

```text
EXIT / INVALID / DATA_ISSUE = 0
COOLDOWN = 1
EARLY = 2
FORMING = 3
MATURE = 4
TRIGGERED = 5
RISK_BLOCKED = 按原结构状态保留等级，但输出风险阻断
```

---

## 六、风险分层

风险等级由 `structure_risk_score` 和 `structure_risk_flags` 共同确定。

```text
LOW:    structure_risk_score < 20
MEDIUM: 20 <= structure_risk_score < 40
HIGH:   structure_risk_score >= 40
HARD:   命中硬风险标记
```

硬风险标记：

```text
OVERHEAT_CHG5
OVERHEAT_CHG20
DOWNTREND
DEEP_FALL
```

风险规则：

- `HIGH` 或 `HARD` 风险触发 `RISK_BLOCK`。
- 风险阻断不否定模型二结构，只表示当前不适合推进到交易动作。
- 有 `SETUP_TRIGGER` 但被风险阻断时，报告应写为“触发候选信号，但风险阻断”，不能写成“可买”。

---

## 七、池子决策

`pool_decision` 统一使用大写枚举：

| pool_decision | 含义 |
|---------------|------|
| `ADD` | 新进入 Bloom 池 |
| `KEEP_FOCUS` | 重点观察 |
| `KEEP_LOW` | 低优先级观察 |
| `COOLDOWN` | 保留观察，但暂不推进 |
| `EXIT` | 移出 Bloom 池 |
| `DATA_HOLD` | 数据异常，维持原状态 |

基础规则：

- `TRIGGERED` / `MATURE` / `FORMING`：`KEEP_FOCUS`
- `EARLY`：`KEEP_LOW`
- `RISK_BLOCKED`：`COOLDOWN`
- `INVALID`：进入冷却；超过保留期后 `EXIT`
- `DATA_ISSUE`：`DATA_HOLD`
- `EXIT`：只写入当日事件和报告，不再保留在滚动状态表 `bloom_state.csv`

---

## 八、估值候选

Bloom 不做估值，但输出是否值得送入估值触发层。

字段：

```text
valuation_candidate
valuation_priority
```

规则：

| valuation_priority | 条件 |
|--------------------|------|
| `HIGH` | `TRIGGERED` 或高质量 `MATURE`，结构分高，风险低 |
| `MEDIUM` | `FORMING` / `MATURE`，结构分中高，风险中低 |
| `LOW` | `EARLY` 或结构还不稳定 |
| `NONE` | `INVALID` / `EXIT` / `DATA_ISSUE` / `RISK_BLOCKED` |

`valuation_candidate=true` 仅表示值得进入估值触发层排队，不表示估值通过，也不表示可以买入。

---

## 九、输出

每只股票输出标准字段：

```text
code
name
bloom_status
bloom_signal
pool_decision
signal_quality
risk_level
valuation_candidate
valuation_priority
model2_stage
model2_setup_signal
model2_action_hint
structure_score
structure_risk_score
structure_risk_flags
close
MA20
MA60
distance_ma20
volume_dry_up
pivot_distance
score_change
days_tracked
days_in_observation
watch_reason
next_watch_point
llm_insight
strategy_version
```

文件输出：

```text
bloom/bloom_<YYMMDD>.md
bloom/state/bloom_state.csv
bloom/state/bloom_events.jsonl
bloom/state/bloom_input_<YYMMDD>.json
```

报告分区：

1. 今日新进入
2. 今日结构升级
3. 成熟/触发重点观察
4. 高风险阻断
5. 冷却与准备移出
6. 数据异常
7. 待估值候选

LLM 观察要点：

- Bloom 可调用 DeepSeek 为重点观察标的生成 `llm_insight`。
- 若 LLM 未配置、调用失败或返回不完整，报告必须显式写出 LLM 状态和原因，并回退使用脚本生成的 `watch_reason`。
- LLM 失败不得影响 Bloom 状态、事件、池子决策和报告生成。
- 配置了 LLM 但调用失败时，脚本应在写出兜底报告后返回非 0，让执行层可以按联网权限重跑。

---

## 十、验收标准

- 所有 Bloom 状态、信号、池子决策使用大写枚举。
- Bloom 不修改模型二输出，只做解释和生命周期管理。
- 重复运行同一天时，事件流水先删除同日事件再重写，保持幂等。
- 单日数据异常不能直接导致 `EXIT`。
- 全新的 `model2_include=false` 标的不能写入 Bloom 状态表；已在状态表中的标的可因连续冷却或失效进入 `EXIT`。
- `EXIT` 标的必须从滚动状态表移除，后续只能由模型二重新发现并以新生命周期进入。
- 高结构分但高风险的股票应输出 `RISK_BLOCKED`，而不是 `TRIGGERED` 的正向交易结论。
- `valuation_candidate` 只代表送估值候选，不代表估值结论或交易建议。
- LLM 观察要点不得静默失败；每日 summary 和 Markdown 必须能看出 LLM 是成功、部分成功、跳过还是失败。
- 配置了 LLM 且调用失败时，Bloom 命令不得以成功状态退出。
