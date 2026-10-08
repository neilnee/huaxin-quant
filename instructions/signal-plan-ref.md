# Signal Plan展示参考：买点仓位提示

2026-10-09。配合[Signal Plan指令](signal-plan.md)使用。

## 规则与公式

规则由 `strategies/04-signal-plan.json` 的 `position_guidance` 维护；其独立子版本只标记仓位提示，不改变Plan价格、量能和等级计算版本。

| 市场确认状态 | 计划仓位使用比例 |
|---|---:|
| CONSOLIDATING / DEFENSIVE | 10%—20% |
| RECOVERY_WATCH | 20%—40% |
| SELECTIVE | 40%—60% |
| OFFENSIVE | 最高80%（内部范围0—80，仅表达上限） |

板块通过条件：NONE的趋势≥1，转强≥0，主线≥-1，退潮不通过。趋势必须为整数-2至2，同日板块数据READY且非回填；缺失和非有限数不通过。通过只代表板块资格，仓位比例不乘板块系数。

百分比为计划仓位使用比例，不以账户总资产为分母。系统只提供比例提示，不包含个人金额、金额换算或持有标的数量限制。

## 输出

- `position_strategy_version`、`position_guidance_mode`：独立仓位提示版本及新公式标记。
- `market_position_range`：市场仓位比例区间。
- `sector_eligible`、`sector_health_level`：板块资格及有效趋势。
- `position_status`、`position_advice`、`position_reason`：资格提示及原因。
- `adjusted_position`、`plan_position_a/b`：展示兼容范围，A/B使用相同市场范围；旧基础仓位和环境系数字段不再输出。

未知市场或无效板块时不提供参与建议；C/D保留观察。次日Plan只显示条件预案，实际触发日重算。本层不自动追加、减仓、移动退出锚或写入真实交易。
