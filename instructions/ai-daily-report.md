# 每日 AI 研读数据包（独立发布指令）

- **版本**：`huaxin_ai_daily_v1.1`
- **目标**：把同日 Dashboard 已发布结论确定性汇总为单一 JSON，供外部 AI 直接研读。
- **配套脚本**：`scripts/daily_ai_report.py`
- **输出**：`reports/ai_daily/<YYYYMM>/huaxin_quant_ai_report_<YYMMDD>.json`

## 一、执行方式

```bash
python3 scripts/daily_ai_report.py --date 260817
python3 scripts/daily_ai_report.py --date 2026-08-17
```

脚本必须支持指定日期、重复安全执行和原子覆盖。它只读取本地已发布数据，不联网、不调用 LLM、不运行或改写任何上游模块。

每日默认工作流在市场、资金、VCP、信号四类 Dashboard 数据包全部发布后调用本脚本，并在随后
执行的全流程完整性核验中检查报告日期和 schema 版本。独立命令继续保留，用于指定日期重建。

## 二、输入

指定日期必须同时存在以下 Dashboard 数据包：

```text
dashboard/data/<YYYYMM>/market_context_<YYMMDD>.js
dashboard/data/<YYYYMM>/capital_context_<YYMMDD>.js
dashboard/data/<YYYYMM>/vcp_context_<YYMMDD>.js
dashboard/data/<YYYYMM>/signals_context_<YYMMDD>.js
```

四个数据包的内部日期必须与请求日期一致。任一输入缺失、格式无效或日期不一致时必须失败，不得输出伪完整报告。

## 三、输出范围

固定包含：

1. `market_trend`：市场状态、持续天数、状态变化、评分、宽基、广度和已有市场解读。
2. `daily_hotspot`：已有当日主线、核心事件、板块、代表股和资讯证据。
3. `sector_strength_rankings`：申万一级、申万二级、概念、风格的当前排名表及这些排名项对应的代表股。
4. `sector_capital`：资金页汇总、阈值、候选板块及板块内代表股。
5. `vcp_structures`：VCP 页面全部当日活跃结构及详情，不受筛选或分页影响。
6. `trading_signals`：信号页面全部当日触发、次日计划及旁路证据。

交易信号在 AI 数据包中必须按交易场景绑定价格语义：

- 顶层不得保留无法判断归属的通用 `support_price`、`invalid_price`。
- 结构枢轴放入 `trade_price_semantics.structure`。
- BREAKOUT、PULLBACK、RETEST 的触发、支撑和失效价格分别保留在
  `trade_price_semantics.plans.<plan_type>`，并用 `matched_plan` 标明当前信号对应的方案。
- 这里只重组 Dashboard 已发布字段，不重新计算价格，不改变信号、计划或仓位结论。
- 每条信号增加稳定的 `record_role`、`signal_id`、`parent_structure_id`；每条 VCP 结构增加
  `structure_anchor` 和 `structure_id`。标识仅用于数据关联，不参与任何评分或交易判断。

固定排除：

- 板块历史变化矩阵、阶段轨迹和未进入当前排名表的板块详情。
- 回测、买点生命周期、投研估值、持仓、自选同步。
- HTML/CSS/JavaScript 展示属性、筛选状态、分页状态。
- 原始行情缓存、LLM prompt、推理过程、接口响应和本地绝对路径。
- 任何新增总结、判断、交易建议或二次 LLM 解读。

## 四、数据规范

- 文件编码为 UTF-8，顶层 `schema_version=huaxin_ai_daily_v1.1`。本版本冻结后，新增兼容字段只升级
  次版本；删除字段或改变既有字段语义必须升级主版本。
- 股票代码保留为字符串；日期使用 `YYYY-MM-DD`；缺失展示值统一转为 `null`。
- 原始数字继续保持数字类型；禁止生成 `NaN` 或 `Infinity`。
- VCP 内嵌板块快照按明确字段表恢复数字和布尔类型；禁止对股票代码、日期、枚举等字符串做
  猜测式全局转换，已声明的机器字段遇到非法类型时必须失败。
- 已有 LLM 文字可以原样保留，但必须在 `field_provenance` 中标记为 `existing_llm_output`。
- 市场 LLM 文字必须附带确定性的 `llm_validation`。校验器仅比较可直接验证的结构化事实；
  冲突时输出 `WARNING`、`analysis_safe_to_use=false` 和冲突明细；通过时为 `true`，不适用时为
  `null`。校验器不改写原文，也不生成新的市场判断。
- 确定性状态、评分和指标分别标记为 `deterministic_rule` 或 `calculated_metric`。
- `content_index` 的数量必须与实际数组一致。
- `data_quality` 必须保留源模块状态、错误和降级信息。
- VCP 状态计数必须携带 `count_basis`、`mutually_exclusive` 和 `total`。Bloom 来源计数的口径是
  合并历史跟踪状态与当日 Quant 结果后的完整生命周期快照，不得与当日 `input_total`、
  `result_total` 或页面 `display_total` 直接比较。页面状态计数必须由实际输出的
  `vcp_structures.items` 重新计数，不盲信历史 Dashboard summary。
- `dictionaries.enums` 必须解释报告中用于决策分层的主要枚举，包括记录角色、信号类型、
  VCP/Bloom 阶段、来源池、观察决策、仓位状态及板块策略层级。
- 成交量不得使用全局默认单位。`dictionaries.units_by_path` 必须按路径声明：信号、Plan量能阈值、
  VCP标准/扩展收缩段的绝对成交量均为“手”，每手100股；`volume_activity`、`volume_ratio_*`、
  `volume_dry_up`、`volume_vs_*` 等为无量纲比率。
- `trading_signals.items[].trade_price_semantics` 是 AI 与下游消费交易价格的 canonical 语义层；
  `plan_inputs` 和嵌套 `formula_ref` 仅作为原始模型依据与计算溯源，发生歧义时不得覆盖 canonical 层。

## 五、验收

- 输出 JSON 可严格解析，且不包含本地绝对路径。
- 市场排名仅含当前四类排名表，不含 `sector_history` 或 `sector_rank_matrix`。
- 资金候选、VCP 标的、信号数量与同日 Dashboard 数据包一致。
- 每条信号不得在顶层出现无场景归属的 `support_price`、`invalid_price`，当前方案必须能由
  `trade_price_semantics.matched_plan` 定位。
- 信号 `record_role` 必须与 `signal_kind` 一致，`signal_id` 在当日报告内唯一；存在结构锚点时，
  `parent_structure_id` 必须能关联同日 VCP `structure_id` 或 Plan 自带的结构锚点。
- VCP 内嵌板块的数字、布尔机器字段必须保持 JSON 原生类型。
- 状态计数 `total` 必须等于各状态计数之和；互斥口径必须明确标记为 `true`。
- 当全部宽基已站上 MA20 时，市场 LLM 文字若仍要求“更多宽基重回/站上 MA20”，
  `llm_validation` 必须给出冲突告警且 `analysis_safe_to_use=false`。
- `dictionaries.units_by_path` 必须覆盖报告中的全部绝对成交量字段，不得再声明“默认按股”。
- 代表股只来自当前排名板块或资金候选板块。
- 同日重复执行可以安全覆盖同一路径。
