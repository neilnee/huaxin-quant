# Signal Bloom：模型四 Bloom 信号层指令卡

- **版本管理**: 由 Git 分支与提交历史管理
- **最近更新**: 2026-08-20
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
setup_score
setup_quality
setup_reasons
setup_misses
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
| `TRIGGERED` | 模型二出现 `PULLBACK_BUY` / `BREAKOUT_BUY` / `RETEST_BUY` |
| `RISK_BLOCKED` | 结构存在，但当前风险过高 |
| `COOLDOWN` | 模型二临时出局，仍在观察保留期 |
| `INVALID` | 结构失效或等待重建 |
| `EXIT` | 移出 Bloom 池 |
| `DATA_ISSUE` | 数据异常，暂不改变长期判断 |

状态基础映射：

| 模型二字段 | Bloom 状态 |
|------------|------------|
| `setup_signal=PULLBACK_BUY` | `TRIGGERED` |
| `setup_signal=BREAKOUT_BUY` | `TRIGGERED` |
| `setup_signal=RETEST_BUY` | `TRIGGERED` |
| `structure_stage=VCP_TIGHT` | `MATURE` |
| `structure_stage=VCP_MATURE` | `MATURE` |
| `structure_stage=VCP_FORMING` | `FORMING` |
| `structure_stage=VCP_EARLY` | `EARLY` |
| `structure_stage=POST_BREAKOUT` | `COOLDOWN` |
| `post_breakout_state=POST_BREAKOUT_RETEST/HOT/CONSOLIDATING/FAILED/EXPIRED` 且未触发买点 | `COOLDOWN` |
| `structure_stage=TREND_REBUILD` | `INVALID` |
| `structure_valid=false` | `INVALID` |
| `structure_stage=DATA_ISSUE` | `DATA_ISSUE` |
| 已在 Bloom 状态表中的 `model2_include=false` / `action_hint=REJECT` | `COOLDOWN` 或 `EXIT` |

状态判定优先级为：数据异常 → 已触发买点 → 明确结构失效 → 突破后生命周期 → 普通 VCP 阶段 → 未识别阶段兜底。模型二为保留旧 VCP 突破后审计，可能输出 `structure_stage=NONE`、`model2_include=true` 和明确的 `post_breakout_state`；Bloom 必须优先消费 `post_breakout_state`，不得把这类标的按未识别阶段兜底为 `FORMING`。

`COOLDOWN` 只是突破后生命周期在 Bloom 状态枚举中的兼容映射，不再等同于统一的 5 日冷却退出。Bloom 必须原样保留模型二已有的 `post_breakout_state`、`structure_breakout_date`、`structure_breakout_score`、`breakout_days` 和 `structure_breakout_level`：

- `POST_BREAKOUT_HOT` / `POST_BREAKOUT_RETEST` / `POST_BREAKOUT_CONSOLIDATING` 持续进入“突破后跟踪”，不得因 `cooldown_keep_days` 提前移出。
- `POST_BREAKOUT_FAILED` / `POST_BREAKOUT_EXPIRED` 是突破后终态，当日直接 `EXIT`；退出事件仍进入当日报告供复盘，但不继续占用跟踪列表。
- 突破后有效期完全沿用模型二：突破当日至第 15 日为 HOT/RETEST，第 16–20 日为 CONSOLIDATING，超过 20 日由模型二转为 EXPIRED；Bloom 不重复计算突破天数或另造阶段。
- 若模型二重新识别出 `PRE_BREAKOUT` 新结构，则按新结构的普通 VCP 阶段重新进入突破前生命周期。

Bloom 状态层继续完整维护所有有效突破后生命周期；日报、VCP Dashboard 和自选目标中的“突破后跟踪”重点集合只保留 `structure_breakout_score >= 55` 的标的。筛选只使用本次突破冻结的原 VCP 分数，不使用当日重扫分，也不因 HOT/RETEST/CONSOLIDATING 状态另设豁免。

风险阻断优先级高于普通观察状态。若结构状态为 `FORMING` / `MATURE` / `TRIGGERED`，但触发高风险规则，则输出 `RISK_BLOCKED`。

---

## 五、信号枚举

`bloom_signal` 统一使用大写枚举：

| bloom_signal | 判断依据 |
|--------------|----------|
| `NEW_ENTRY` | 昨日不在 Bloom 池，今日进入有效观察状态 |
| `UPGRADE` | 状态等级上升，或结构分明显改善 |
| `DOWNGRADE` | 状态等级下降，或结构分明显恶化 |
| `SETUP_TRIGGER` | `setup_signal=PULLBACK_BUY/BREAKOUT_BUY/RETEST_BUY` |
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
suggested_position
structure_score
structure_risk_score
structure_risk_flags
setup_score
setup_quality
setup_reasons
setup_misses
close
MA20
MA60
distance_ma20
volume_dry_up
pivot_distance
post_breakout_state
structure_breakout_date
structure_breakout_score
breakout_days
structure_breakout_level
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
bloom/state/snapshots/bloom_state_before_<YYYYMMDD>.csv
dashboard/data/<YYYYMM>/vcp_context_<YYMMDD>.js
```

Bloom 日快照和事件账本必须先写入 `cache/strategy/strategy_data.sqlite`，再从数据库发布 `bloom_input`、当前状态 CSV、事件 JSONL 和报告。Bloom 读取历史状态时以数据库为权威来源；同日首次写入前快照继续保留，专用于重复运行的昨日状态基准。该变更只调整持久化，不改变生命周期判定，详细契约见 `instructions/strategy-data.md`。

`dashboard_vcp.py` 仅读取当日 `bloom_input` 与同日模型二 JSON，按月发布 VCP 结构页所需的独立数据包；`--all` 可重建全部已有 Bloom 日期。它不得改写 Bloom 状态、模型二输出或触发交易动作。

`dashboard_signals.py` 在每日工作流中必须以 `--fetch-capital` 启动，并按当日触发股与 Signal Plan 股票代码去重补查个股主力和融资资金。该补查使用独立于完整资金观测的请求预算；发布数据包必须记录 `capital_fetch_enabled`、实际请求数和错误列表，每条信号必须保留 `capital_support`，数据缺失时明确降级而不得省略。每日完整性核验必须确认同日信号数据包确实启用了资金补查。

VCP 页面补充申万二级行业与板块状态时，优先读取同日 `market/stock_strength_<YYMMDD>.csv` 和 `market/sector_heat_<YYMMDD>.csv`；同日文件缺失时才读取数据库中的同日快照。不得回退到其他日期。历史降级口径沿用 Market Regime 产物的 `history_basis`，不得把当前成分回填伪装成严格点时数据。所属板块卡片中的主阶段、阶段趋势和短线脉冲附加标记必须使用同一尺寸的胶囊标签，并在同一水平行内纵向居中展示，不得因卡片通用 `span` 样式变成纵向排列；各标签继续保留自身语义颜色。

VCP 标的详情必须把 `contraction_count` 明确标为“标准收缩轮次”，并独立展示
`contraction_extensions` 的“扩展收缩轮次”；扩展段不得混入或改写标准轮数。下方“收缩与量能”按开始日期
统一排列当前 `contraction_group` 和 `contraction_extensions`，每段必须明确标记“标准段 / 确认型重置段 /
末端微收缩段”，同时展示时间、收盘回撤、持续时间、段均量、收复幅度及扩展段确认关系。历史 Quant
缺少扩展字段时按 0 轮兼容展示，不得从价格数据临时推导。

信号发现页的个股详情必须保持以下层次：“当日触发/次日计划”与“发现来源”标签并列，不再单独显示“发现来源”标题；“市场板块”同行展示市场环境与申万二级板块组合标签，不再在顶部网格重复显示板块和板块状态；顶部网格只保留买点类型和结构阶段。次日触发条件紧跟在仓位预案之后，随后依次展示个股资金验证和量价参考。

详情标签按信息属性分色：信号计划/触发使用金色/红色，发现来源使用蓝至靛紫色，市场环境使用青绿、蓝、黄、红表达强弱，板块状态统一使用紫色系并以明暗区分强弱，财务提示使用绿/红/灰表示通过/风险/未验证，技术风险使用红色。资金标签必须复用资金观测页的四档方向色，不另建一套配色。

个股资金验证只作为旁路证据，不得改写模型二买点、评分、等级或仓位。信号发布器按股票代码去重后复用 `capital_data.sqlite`，每批最多5只；展示主力资金状态、最新净流入率、近3日流入天数，以及融资资金状态、近5日融资余额变化和最新融资净买入额。数据包保留主力资金和融资资金的实际日期供审计；信号详情必须在主力资金模块下显示 `main_data_date`，在融资资金模块下显示 `margin_data_date`，日期缺失时各自显示“数据日期待补”，不得借用另一模块日期。已查询但未返回融资字段时保持“数据缺失”，不得写成零。

VCP Dashboard 的历史起点与系统回放起点一致，为 `2026-05-06`。若历史 Quant 已存在但缺少早期 `bloom_input`，只允许按日期顺序做确定性轻量回放：生命周期状态必须写入临时隔离目录，跳过 LLM、事件账本和当前 `bloom_state.csv`，仅补充缺失的历史 `bloom_input` 后再由 `dashboard_vcp.py --all` 发布。不得为修复页面重复运行市场、Pool 或 Quant，也不得用当前 Bloom 状态倒灌历史日期。

`bloom_state_before_<YYYYMMDD>.csv` 是当日首次写入前的状态快照，用于同日重复运行时保持 `consecutive_reject`、`days_tracked` 等生命周期字段的判断基准稳定。重复运行同一天时必须优先读取该快照，避免已写入的当日 state 覆盖昨日累计状态，导致 `EXIT` 判断被冲掉。

报告分区：

1. 突破前跟踪（EARLY/FORMING/MATURE/TRIGGERED/RISK_BLOCKED，含结构评分；今日新进入标 🆕 标记）
2. 突破后跟踪（HOT/RETEST/CONSOLIDATING）
3. 今日结构升级
4. 成熟/触发重点观察
5. 高风险阻断
6. 冷却与准备移出
7. 数据异常
8. 待估值候选

Markdown 的“全量观察”分区中，“突破前跟踪”展示全部突破前活跃标的（EARLY/FORMING/MATURE/TRIGGERED/RISK_BLOCKED）；“突破后跟踪”展示尚未退出的 `POST_BREAKOUT_*` 标的，并明确显示突破后状态、突破日期、突破后交易日和距 Pivot。每只显示代码、名称、状态和结构评分，今日新进入的额外标注 🆕；“移出”使用紧凑多列表格展示，表头保持为空，单元格包含股票代码、名称和 Bloom 状态；不得把大量移出标的拼成单行长文本。

VCP Dashboard 默认页签名称为“突破前跟踪”，替代原“全部”；原有 Bloom 状态筛选继续只筛突破前标的，最后增加“突破后跟踪”页签。突破后页签使用模型二 `post_breakout_state` 作为主状态，结构分只显示冻结的 `structure_breakout_score`；不得用 `VCP_FORMING` 等当前重扫阶段或当日 `structure_score` 掩盖、改写旧 VCP 突破时的结构质量。

VCP 结构标题卡右侧的六个汇总数据块在桌面宽度下必须保持单行横排；只在窄屏空间不足时降为三列或两列，不得影响其他模块的汇总卡布局。

“突破后跟踪”表的状态列因表头已明确上下文，单元格只显示“强势 / 回踩 / 整理”短标签，并分别使用红、蓝、紫三种语义色；详情区域继续显示完整状态含义。列表按冻结结构分从高到低排序。

Markdown 的“重点观察”表格列为：

```text
代码 | 名称 | 结构 | Bloom | 买点 | 风险 | 观察要点
```

- 重点观察纳入规则：`VCP_MATURE` / `VCP_TIGHT` 默认纳入；`VCP_FORMING` 需 `structure_score >= 60`；`VCP_EARLY` 需 `structure_score >= 60`；若 `setup_signal=PULLBACK_BUY/BREAKOUT_BUY/RETEST_BUY` 或 Bloom 状态为 `TRIGGERED`，不受结构分门槛限制，必须纳入。
- 重点观察表只承载突破前结构；存在明确 `POST_BREAKOUT_*` 的标的统一进入“突破后跟踪”，不得因当日重扫得到的阶段或结构分重复进入重点观察表。
- 重点观察排序规则：先排有买点触发的标的，再按结构阶段强弱排序（`VCP_TIGHT` > `VCP_MATURE` > `VCP_FORMING` > `VCP_EARLY`），最后按 `structure_score` 从高到低排序。
- `结构` 列格式为 `model2_stage / structure_score分`，例如 `VCP_FORMING / 62分`。
- `买点` 列格式为 `setup_signal / setup_score分 / setup_quality级买点 / 建议仓位：suggested_position`，例如 `BREAKOUT_BUY / 82分 / A级买点 / 建议仓位：40%-50%`；无买点时填 `-`。
- 不单独设置“收缩”列；收缩明细保留在该股票下方的缩进详情行中，且必须优先使用模型二 `contraction_group` 表示当前 VCP 结构，不得从全部历史 `contractions` 机械截取最近 N 段。
- `Bloom` 列只表示生命周期状态，不得替代或吞掉模型二买点类型。

LLM 观察要点：

- Bloom 可调用 DeepSeek 为重点观察标的生成 `llm_insight`。
- 只有 `structure_score >= 70` 的重点观察标的才调用 LLM；低于门槛的标的继续保留在重点观察列表，并使用脚本生成的 `watch_reason`，不得改变其 Bloom 状态、买点或排序。
- 传给 LLM 的上下文必须包含 `model2_setup_signal`、`setup_score`、`setup_quality`、`setup_reasons`、`setup_misses` 和 `suggested_position`，观察要点应考虑买点类型与质量。
- LLM 观察要点按单只股票逐个请求，避免批量 JSON 截断或单个返回异常影响全部标的。
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
- 存在明确 `POST_BREAKOUT_*` 生命周期的标的必须进入独立的“突破后跟踪”，不得混入“突破前跟踪”；HOT/RETEST/CONSOLIDATING 不受普通 5 日 COOLDOWN 退出影响，FAILED/EXPIRED 当日退出。
- `valuation_candidate` 只代表送估值候选，不代表估值结论或交易建议。
- LLM 观察要点不得静默失败；每日 summary 和 Markdown 必须能看出 LLM 是成功、部分成功、跳过还是失败。
- 配置了 LLM 且调用失败时，Bloom 命令不得以成功状态退出。
