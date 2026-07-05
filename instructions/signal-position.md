# Signal Position: 持仓管理层指令卡

- **版本管理**: 由 Git 分支与提交历史管理
- **最近更新**: 2026-07-06
- **所属模型**: 模型四 Tracker
- **策略配置**: `strategies/04-position.json`
- **核心目标**: 建立独立的持仓账本模块，按真实交易日期维护交易流水、当前批次和每日持仓状态，为后续个股/ETF 策略监控提供可信输入。

---

## 一、职责边界

持仓管理层分两阶段建设：

1. **账本管理**：记录真实交易、重建持仓状态、维护当前未闭合批次。
2. **策略监控**：后续基于账本状态、Bloom、行情和 ETF 均线输出交易点提示。

当前先实现账本管理，不实现自动交易策略。

持仓管理负责：

- 维护按月交易流水。
- 按 `trade_date` 重放交易，而不是按录入日期。
- 生成某日收盘后的持仓状态快照。
- 维护一份滚动的当前未闭合批次表。
- 为后续个股和 ETF 监控提供统一输入。

持仓管理不负责：

- 自动下单。
- 推断未提供的真实交易。
- 因 Bloom 或均线信号自动修改持仓。
- 自动调用模型三估值。

---

## 二、设计思路

持仓管理采用“事实源 + 可重建派生表”的设计。

核心原则：

- **交易事实不可混入策略判断**：真实买卖、分红、拆分和年初补仓都写入流水；Bloom、估值和人工判断只作为后续策略层输入。
- **按真实交易日期重放**：晚同步交易不影响历史时间线，`recorded_at` 只表示录入时间，账本计算只看 `trade_date`。
- **流水是主账本**：`trade_ledger_YYYY-MM.csv` 和 `position_plan.csv` 是事实源；`lots_current.csv`、`position_state_YYYY-MM-DD.csv`、年度复盘表都可以重建。
- **年度收益与账户总收益分开**：年度重建使用年初基准价；券商截图里的账户成本只作为参考字段保留，不参与年度收益硬计算。
- **当前持仓是校验锚点**：用截图或券商当前持仓表校验重放后的持仓数量，缺失的跨年持仓通过 `OPENING` 补齐。
- **现金事件显式记录**：分红、红利税等不出现在成交流水里的现金变化，用 `DIVIDEND` 事件补进账本。
- **非买卖份额变化显式记录**：ETF 折算、拆分等用 `SPLIT` 事件处理，不伪装成买入或卖出。

收益复盘核心公式：

```text
年度总收益 = 年度实现盈亏 + 当前持仓年度浮盈

年度实现盈亏 = 卖出净回款 + DIVIDEND净额 - 被卖出批次成本
当前持仓年度浮盈 = 当前市值 - 当前未闭合批次成本
```

对于跨年持仓：

```text
OPENING成本 = OPENING数量 * year_start之前最后一个交易日收盘价
```

如果缺少年前行情，才退回到 `year_start` 当日或之后最近交易日、账户成本或首笔成交价，并在引用字段中标记来源。

---

## 三、目录结构

持仓管理是独立模块，运行数据放在项目根目录下的 `position/`，不放入 `signals/`。

```text
position/
├── position_plan.csv
├── lots_current.csv
├── trades/
│   ├── trade_ledger_2026-07.csv
│   └── trade_ledger_2026-08.csv
├── states/
│   ├── position_state_2026-07-05.csv
│   └── position_state_2026-07-06.csv
├── performance/
│   └── trade_performance_2026.csv
└── imports/
    └── raw_trades_20260710.csv
```

文件职责：

| 文件 | 职责 | 是否事实源 |
|------|------|------------|
| `position_plan.csv` | 人工维护的目标仓位和策略标签 | 是 |
| `trades/trade_ledger_YYYY-MM.csv` | 按真实交易月份归档的交易流水 | 是 |
| `lots_current.csv` | 当前未闭合批次滚动表 | 否，可由流水重建 |
| `states/position_state_YYYY-MM-DD.csv` | 某日持仓状态快照 | 否，可由流水重建 |
| `performance/trade_performance_YYYY.csv` | 年度交易表现复盘 | 否，可由流水重建 |
| `imports/` | 原始导入文件留档 | 辅助审计 |

事实源优先级：

```text
trade_ledger_YYYY-MM.csv + position_plan.csv
  > lots_current.csv
  > position_state_YYYY-MM-DD.csv
```

如出现冲突，以交易流水和目标计划重建为准。

---

## 四、目标仓位表

`position_plan.csv` 由人工维护，用于记录每个标的的目标仓位和策略标签。

字段：

```text
code,name,asset_type,target_type,target_value,strategy_tag,max_position_pct,status,manual_tag,notes
```

| 字段 | 说明 |
|------|------|
| `code` | 股票/ETF 代码 |
| `name` | 名称 |
| `asset_type` | `STOCK` / `ETF` |
| `target_type` | `shares` / `amount` |
| `target_value` | 目标股数或目标金额 |
| `strategy_tag` | 个股策略或 ETF 策略标签 |
| `max_position_pct` | 最大目标仓位比例，默认 `1.0` |
| `status` | `ACTIVE` / `CLOSED` / `WATCH_ONLY` |
| `manual_tag` | 人工备注标签 |
| `notes` | 其他说明 |

`position_plan.csv` 不记录实际持仓。实际持仓由交易流水重建。

---

## 五、交易流水

交易流水按真实交易月份归档：

```text
position/trades/trade_ledger_YYYY-MM.csv
```

字段：

```text
trade_id,trade_date,trade_time,recorded_at,code,name,asset_type,side,shares,price,amount,fee,tax,reason,signal_ref,notes
```

| 字段 | 说明 |
|------|------|
| `trade_id` | 全局唯一交易 ID |
| `trade_date` | 真实交易发生日期 |
| `trade_time` | 真实成交时间；没有时间时填 `00:00:00` |
| `recorded_at` | 录入日期或导入日期 |
| `side` | `BUY` / `SELL` / `OPENING` / `SPLIT` / `DIVIDEND` |
| `shares` | 股数/份额 |
| `price` | 成交价格 |
| `amount` | 成交金额，空则由 `shares * price` 计算 |
| `fee` / `tax` | 费用和税费 |
| `reason` | 交易原因，如 `Bloom_FORMING` / `manual` |
| `signal_ref` | 可选信号引用 |
| `notes` | 备注 |

关键规则：

- 晚补交易时，必须写入真实 `trade_date` 所在月份的流水文件。
- `recorded_at` 只表示记录进入系统的日期，不参与持仓时间线计算。
- `OPENING` 用于初始化历史持仓，等同于一笔买入，但原因必须写清楚。
- `SPLIT` 用于 ETF 份额折算、拆分等非买卖份额调整；`price` 填调整比例，不改变成本金额。
- `DIVIDEND` 用于现金分红、红利税等现金事件；不改变持仓数量，按 `amount - fee - tax` 计入该标的年度实现盈亏。
- 流水原则上只追加；如需纠错，应通过更正交易或人工确认后重建。

---

## 六、批次滚动表

`lots_current.csv` 只保留当前未闭合批次。

字段：

```text
lot_id,code,name,asset_type,open_date,source_trade_id,open_reason,shares_remaining,cost_price,cost_amount,realized_shares,cost_basis_type,price_ref_date,price_ref_source,status
```

规则：

- 买入或 `OPENING` 产生新批次。
- 卖出按 FIFO 扣减批次。
- 批次卖完后从 `lots_current.csv` 删除。
- `lots_current.csv` 可由交易流水重建，不做月度快照。
- `cost_basis_type` 用于区分真实买入成本和年度重建的年初基准成本。

---

## 七、持仓状态快照

`states/position_state_YYYY-MM-DD.csv` 是某个日期收盘后的持仓状态。

字段：

```text
as_of,code,name,asset_type,strategy_tag,target_type,target_value,actual_shares,cost_price,cost_amount,realized_pnl,last_trade_date,position_status,manual_tag,account_cost_price,account_cost_amount,latest_price,account_unrealized_pnl
```

规则：

- 每次 `rebuild --as-of YYYY-MM-DD` 生成对应日期快照。
- 同一日期重复重建应覆盖同名快照，保持幂等。
- 快照不作为事实源，可随时由交易流水重建。
- `cost_price` 是账本重建成本，`account_cost_price` 是券商截图中的账户成本参考，两者不强行混用。

---

## 八、年度重建与复盘

当用户提供当前持仓截图和年内历史成交时，可先将截图人工整理为标准持仓 CSV，再用年内流水重建年度账本：

```bash
python3 scripts/position.py reconstruct-year --holdings position/imports/current_holdings_2026-07-05.csv --trades refer/历史成交0705.csv --as-of 2026-07-05 --year-start 2026-01-01 --overwrite-ledgers
```

重建规则：

- 二级市场 `证券买入` / `证券卖出` 写入交易流水。
- `公开发行申购` 不直接视为持仓买入，默认跳过，原始文件仍留档。
- 现金分红等非成交事件可写入 `position/imports/cash_events_YYYY.csv`，重建时自动合并进入对应月份流水。
- 若券商流水最后一笔 `股份余额` 与当前截图数量存在明确整数倍差异，生成 `SPLIT` 份额调整事件。
- `OPENING` 数量由 `当前持仓数量 - 年内净买入数量` 反推；对已平仓的年初持仓，若年内卖出大于买入，也会补对应 `OPENING`。
- `OPENING` 价格用于年度收益重建时，优先使用 `year_start` 之前最后一个交易日收盘价；若行情缺失，再使用 `year_start` 当日或之后最近交易日收盘价、账户成本或首笔成交价兜底，并在引用字段中记录来源。
- 年度表现输出到 `position/performance/trade_performance_YYYY.csv`，用于复盘年度实现盈亏、期末浮盈和总盈亏。

---

## 九、日常使用流程

### 1. 初始化目录

首次使用时执行：

```bash
python3 scripts/position.py init
```

该命令只创建目录和空模板，不写入真实交易。

### 2. 准备输入

常见输入有三类：

| 输入 | 放置位置 | 说明 |
|------|----------|------|
| 当前持仓截图/导出表 | `position/imports/current_holdings_YYYY-MM-DD.csv` | 截图需人工整理成 CSV |
| 券商历史成交 | `refer/` 或 `position/imports/` | 原始文件保留，重建时会归档到 `position/imports/` |
| 分红/红利税/现金事件 | `position/imports/cash_events_YYYY.csv` | 没有现金事件时可为空或不存在 |

当前持仓 CSV 最少需要：

```text
code,name,asset_type,shares,cost_price,latest_price,unrealized_pnl
```

现金事件 CSV 使用交易流水同一套字段，`side` 填 `DIVIDEND`。

### 3. 年度重建

用当前持仓和年内流水重建年度账本：

```bash
python3 scripts/position.py reconstruct-year \
  --holdings position/imports/current_holdings_2026-07-05.csv \
  --trades refer/历史成交0705.csv \
  --as-of 2026-07-05 \
  --year-start 2026-01-01 \
  --overwrite-ledgers
```

重建会完成：

- 清洗券商流水。
- 按月份写入 `trade_ledger_YYYY-MM.csv`。
- 反推跨年 `OPENING`。
- 合并现金事件。
- 自动识别明确整数倍的份额调整并写入 `SPLIT`。
- 重放交易并生成 `lots_current.csv`。
- 生成 `position_state_YYYY-MM-DD.csv`。
- 生成 `trade_performance_YYYY.csv`。

### 4. 日常补录一笔交易

```bash
python3 scripts/position.py add-trade \
  --trade-date 2026-07-06 \
  --code 300442 \
  --name 润泽科技 \
  --asset-type STOCK \
  --side BUY \
  --shares 100 \
  --price 80 \
  --reason manual
```

补录后再执行：

```bash
python3 scripts/position.py rebuild --as-of 2026-07-06
```

如果补录的是大量券商流水，优先用 `import` 或重新执行 `reconstruct-year`。

### 5. 日常重建状态

只根据现有流水重建某日状态：

```bash
python3 scripts/position.py rebuild --as-of 2026-07-06
```

该命令不会重新反推 opening，也不会重新解析原始券商流水；它只重放已有 `trade_ledger_YYYY-MM.csv`。

---

## 十、维护规则

- 每次同步新成交，以券商真实成交日期为准，不按同步日期入账。
- 月度流水原则上只追加；若要纠错，优先重新从原始券商流水执行 `reconstruct-year --overwrite-ledgers`。
- 当前持仓截图只用于重建和校验，不替代交易流水。
- 分红、红利税、ETF 折算等非成交事项要显式写入现金事件或份额调整，不能混在买卖里。
- `lots_current.csv` 是滚动派生表，卖完的批次不保留在当前表中。
- `position_state_YYYY-MM-DD.csv` 是 dated snapshot，不需要再维护一份无日期的状态表。
- `trade_performance_YYYY.csv` 是复盘表，不作为事实源；任何流水变更后都应重建。
- `position/` 下运行数据默认不进 Git；脚本、指令卡、策略配置才进 Git。
- 后续策略监控只能读取账本输出，不得直接改交易流水和持仓状态。

---

## 十一、脚本入口

正式入口：

```bash
python3 scripts/position.py init
python3 scripts/position.py bootstrap --from signals/positions.csv --as-of 2026-07-05
python3 scripts/position.py add-trade --trade-date 2026-07-06 --code 300442 --name 润泽科技 --asset-type STOCK --side BUY --shares 100 --price 80 --reason manual
python3 scripts/position.py import --file tmp/trades.csv
python3 scripts/position.py reconstruct-year --holdings position/imports/current_holdings_2026-07-05.csv --trades refer/历史成交0705.csv --as-of 2026-07-05 --year-start 2026-01-01 --overwrite-ledgers
python3 scripts/position.py rebuild --as-of 2026-07-05
```

命令说明：

| 命令 | 职责 |
|------|------|
| `init` | 创建 `position/` 目录和模板文件 |
| `bootstrap` | 从旧持仓快照生成目标计划和 `OPENING` 流水 |
| `add-trade` | 补录一笔标准交易 |
| `import` | 导入标准交易 CSV，并按 `trade_date` 写入对应月份 |
| `reconstruct-year` | 用当前持仓和年内券商流水反推年初仓位并生成年度复盘 |
| `rebuild` | 按交易流水重建持仓状态和当前批次 |

---

## 十二、后续策略监控边界

账本管理完成后，再独立实现策略监控：

- 个股：基于 `position_state` + `lots_current` + `bloom_state` + 行情，提示加仓观察、减仓观察、止损复核。
- ETF：基于 `position_state` + `lots_current` + 均线/量价，提示趋势破位、网格观察、止盈止损。

策略监控只输出提示，不修改交易流水和持仓状态。

---

## 十三、验收标准

- 持仓数据放在独立 `position/` 模块下，不写入 `signals/`。
- 交易流水按真实 `trade_date` 写入对应月份。
- 晚补交易后，重建能按真实时间线更新持仓。
- `lots_current.csv` 只保留未闭合批次，卖完即删除。
- `position_state_YYYY-MM-DD.csv` 可重复生成且幂等。
- 当前持仓截图和年内流水可重建当年账本、当前批次和年度表现复盘。
- 未实现策略监控前，不输出买卖建议。
