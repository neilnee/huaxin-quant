# 模型三：估值参考手册

> 执行指令见 `03-valuation.md`。本文件为按需查阅的参考内容，**不在每次执行时加载**。

---

## 估值公式速查

### 路线 A: EV/EBITDA（重资产 + 无非经常性）
```
企业价值(EV) = 2026E EBITDA × 可比 EV/EBITDA 均值
股权价值 = EV - 净有息负债
保守估值 = 股权价值 × 折扣系数
```

### 路线 A1: 分部估值（重资产 + 有非经常性）
三类非经常性处理：

| 类型 | 特征 | PE | 处理 |
|------|------|-----|------|
| A: 一次性 | 无历史重复 | 不给倍数/5-8x | 直接加回 × (0.5-0.8) |
| B: 可重复非年度 | 有管道/计划, 间隔>1年 | 8-15x | 年均化 × PE × 概率 |
| C: 经常化 | 每年稳定 | 10-20x | 核心PE × (0.6-0.8) |

类型B必须列出完整管道清单。

### 路线 B: PE + P/FCF（轻资产）
```
合理PE = 可比PE × (公司增速/可比增速) × 定性调整
保守 = 净利 × 合理PE × 折扣系数
FCF验证: FCF收益率 > 5%
```

### 路线 C: PE + DCF（中资产 + 稳定）
```
同路线B PE计算
DCF交叉: WACC 8-12%, 永续增长 2-3%
```

### 路线 D: PB + 周期PE（中资产 + 波动）
```
合理PB = 可比PB × (公司ROE/可比ROE) × 定性调整
保守 = 净资产 × 合理PB × 折扣系数
隐含PE分母用3年平均净利
```

### 路线 E: PS + PEG（亏损 + 高增长）
```
合理PS = 可比PS × (公司毛利率/可比毛利率)
保守 = 营收 × 合理PS × 折扣系数
```

### 路线 F: PB 净资产重估（亏损 + 低增长）
```
合理PB = 可比PB × 调整系数
保守 = 净资产 × 合理PB × 折扣系数
```

---

## PEG 分档速查

| 增长质量 | PEG上限 | 典型行业 |
|---------|---------|---------|
| 结构性成长 | ≤1.5 | AI光模块、GLP-1、半导体设备国产化 |
| 周期性成长 | ≤1.0 | 锂电、光伏、化工 |
| 成熟/衰退 | ≤0.7 | 铁路设备、传统制造 |

判断标准：增速来自一次性爆发还是结构性升级？增长跑道多长？

---

## 折扣系数

| 地位 | 系数 |
|------|------|
| 龙头（Top3 + 毛利率>同业） | 1.0 |
| 中游（Top10） | 0.85 |
| 跟随者 | 0.7 |

---

## 分歧度分档

| 分歧度 | 定价状态 |
|--------|---------|
| <20% | 已充分定价 |
| 20-50% | 大部分已定价 |
| >50% | 仅部分定价 |

---

## 保守情景参数规则

增长质量决定不确定性在哪端——保守情景只压不确定的那端。

| 增长质量 | 不确定性位置 | 悲观利润 | 悲观PE | 逻辑 |
|---------|------------|---------|--------|------|
| 结构性成长 | PE端 | 共识均值 | 基准×0.90 | AI/AIDC/GLP-1订单是实物，情绪降温≠生意消失 |
| 周期性成长 | 两端 | 共识下沿 | 基准×0.85 | 需求和估值都会波动 |
| 成熟/衰退 | 利润端 | max(扣非,共识下沿) | 基准PE | 估值已在地板，主要风险在业绩恶化 |

## 确定性三情景框架

| 确定性 | 基准取值 | 悲观PE折扣 | 悲观利润折扣 |
|--------|---------|-----------|------------|
| 高 | ~100% | ×0.90 | ×0.93 |
| 中 | 70-90% | ×0.80 | ×0.75 |
| 低 | 30-50% | ×0.80 | ×0.70 |

上行因子（可上调半级）：头部客户背书 / 送样验证 / 团队履历

---

## 报告模板骨架

```markdown
# <名称>（<代码>）估值报告

> 首次覆盖/持续跟踪 | 当前股价/市值 | 决策树路线 | 分歧度

## 一、估值总览
- 估值区间 + 价格带图
- 双年估值对照表：估值(元) / 市值(亿元) / 隐含PE / 距当前(%)，2026E+2027E各三档
- 支柱×Layer 矩阵（每个支柱在三层中的贡献）
- 三层估值汇总表（按层合并：悲观/基准/乐观）
- 各支柱估值区间表（悲观/基准/乐观）
- 情景对照表（悲观/基准/乐观）

## 二、支柱一：主营业务
- 计算逻辑（公式代入数字，末尾必须输出：XX亿 ÷ XX亿股 = XX元/股）
- 核心假设表
- 利润测算过程
- 可比公司锚定表
- 敏感性表

## 三、支柱二：增长业务/分歧项（如有Type B/估值逻辑分歧则强制输出）
- 业务实质 + 逐项计算 + 假设依据
- 管道清单（Type B强制，每项目标注规模/概率/估值）

## 四、支柱三：Layer 3 预期差（强制 — 每只必须有，即使结论为"无"）
- 3.0 过滤结果 + 搜索执行记录
- 预期差清单（含量化、概率、定价状态）
- 若无预期差：写"已执行路径B/D搜索，未发现显著预期差"

## 五、2027E 估值（放在所有支柱之后）
- 参数迁移表 + 逐支柱计算 + 汇总

## 六、催化剂日历（近期+中期）

## 七、风险提示

## 八、研究更新记录

> 数据可信度说明
```

---

## CSV 字段定义

### `_index.csv`
`code,name,研究状态,阶段零~五状态,上次完成阶段,数据截至报告期,首次覆盖日期,最后更新日期`

### `_ranking.csv`
`code,name,主估值方法,当前股价,下行风险价,保守估值,基准估值,乐观估值,隐含PE(保守/基准/乐观),安全边际折扣率,距下行风险价,预期差数量,高概率预期差数量,近期催化数量,叙事匹配度,综合优先级`

排序按安全边际折扣率升序。综合优先级 = 折扣率得分(0-3) + 催化密度(0-2) + 叙事匹配(0-2) → A(5-7)/B(3-4)/C(0-2)

---

## 阶段零 mx-data 字段映射

> LLM 无需关心此节，由 `valuate.py` 自动处理。以下仅供脚本维护参考。

| 指标 | mx-data 关键词 |
|------|---------------|
| 归母净利润 | 归属母公司股东的净利润 |
| 扣非净利润 | 扣除非经常性损益后的净利润 |
| 经营现金流 | 经营活动产生的现金流量净额 |
| 固定资产 | 固定资产净额 |
| 在建工程 | 在建工程 |
| 研发费用 | 研发费用合计 |
| 一致预期净利 | 预测归属于母公司的净利润中值 |
| 一致预期增速 | 预测归属于母公司的净利润增长率 |

---

## 估值计算引擎规格

> 此节用于理解引擎内部逻辑或重建 `scripts/calc_valuation.py`。执行估值时不需要阅读。

### 引擎常量速查

引擎中硬编码的常量（与上方速查表一致，此处汇总所有阈值）：

| 常量 | 值 | 用途 |
|------|-----|------|
| PEG 上限 | structural=1.5, cyclical=1.0, mature=0.7 | PE 三步走第 2 步 |
| 市场折扣系数 | leader=1.0, mid=0.85, follower=0.7 | 基准估值 |
| 悲观 PE 折扣 | structural×0.90, cyclical×0.85, mature×1.00 | 悲观情景 |
| 定性调整幅度 | ±5-10%/项, 总计 ≤±20% | PE 三步走第 3 步 |
| 增速比率上限 | 2.0 | 防止极端增速差导致 PE 过度放大 |
| 分歧度分档 | <20%充分, 20-50%大部分, >50%部分 | Layer 2 定价状态 |
| 反向检查触发 | 折扣率>2.0 或 <0.3 / >1.5且龙头 / <0.4 或 区间宽度>5x | 反向检查 |
| Type B 非经常性 PE | A类5-8x, B类8-15x, C类核心PE×0.6-0.8 | 路线 A1 |
| 下行风险价比率 | 0.80 | 下行风险价 = 悲观估值 × 0.8 |
| WACC 范围 | 8-12% | 路线 C DCF 交叉验证 |
| 永续增长 | 2-3% | 路线 C DCF 交叉验证 |

### 引擎输入参数 schema

LLM 将以下 JSON 保存到 `cache/calc_params/<code>_<YYMMDD>.json`，然后运行 `python3 scripts/calc_valuation.py <该文件>`。

```json
{
  "meta": {"code": "300442", "name": "润泽科技", "total_shares": 16.34, "current_price": 98.71},
  "growth_quality": "structural|cyclical|mature",
  "market_position": "leader|mid|follower",
  "comparable_pe_median": 25.0, "comparable_pe_lower": 20.0, "comparable_pe_upper": 30.0,
  "comparable_pb_median": null, "comparable_pb_lower": null, "comparable_pb_upper": null,
  "comparable_ps_median": null, "comparable_ps_lower": null, "comparable_ps_upper": null,
  "company_growth_rate": 47.0, "comp_growth_rate": 12.0,
  "qualitative_adjustments": [{"item": "行业龙头", "adjustment": 0.10}],
  "pe_override": null, "pb_override": null,
  "ebitda_2026e": null, "ebitda_2027e": null,
  "comparable_ev_ebitda": null, "net_debt": null,
  "wacc": null, "terminal_growth": null,
  "roe": null, "book_value_per_share": null,
  "pillars": [
    {
      "name": "支柱名称",
      "route": "A|A1|B|C|D|E|F",
      "consensus_np_2026e": 29.7, "consensus_np_2027e": 37.8,
      "consensus_np_lower_2026e": 26.9, "consensus_np_upper_2026e": 32.9,
      "consensus_np_lower_2027e": null, "consensus_np_upper_2027e": null,
      "deducted_np": 19.01,
      "layer2": {"pessimistic": 48, "base": 237, "optimistic": 386, "description": "REITs分歧"},
      "layer3_items": [{"name": "印尼一期", "pessimistic": 0, "base": 0, "optimistic": 13, "probability": 0.25}],
      "type_b_pipeline": [{"name": "A-7扩募", "project_profit": 43.0, "pe": 10, "probability": 0.55}],
      "net_assets": null, "roe": null, "revenue": null, "gross_margin": null
    }
  ]
}
```

**字段说明**：
- `meta`：必填。`total_shares` 单位亿股，`current_price` 单位元。
- `growth_quality`：必填。结构性→PE端波动为主；周期性→两端均有不确定性；成熟→利润端风险为主。
- `market_position`：必填。龙头(Top3+毛利率>同业)、中游(Top10)、跟随者。
- `comparable_pe_*`：路线B/C用。可比公司PE区间。
- `comparable_pb_*`：路线D/F用。可比公司PB区间。
- `comparable_ps_*`：路线E用。可比公司PS区间。
- `qualitative_adjustments`：选填。每项 ±5-10%，引擎限制总计 ≤±20%。
- `pe_override`：选填。手工指定PE时使用，引擎跳过三步走公式。引擎自动计算对应的悲观/乐观PE。
- `pillars[]`：必填，至少一个。
  - `route`：该支柱的估值路线（A~F）。引擎按路线分派到对应的计算公式。
  - `consensus_np_*`：共识净利（亿元）。纯L1的业务填实际值，纯L2/L3的业务填0。
  - `layer2`：该支柱的L2分歧调整（亿元）。引擎归入矩阵的L2层。
  - `layer3_items[]`：该支柱的L3预期差。引擎汇总到矩阵的L3层。
  - `type_b_pipeline[]`：仅路线A1。引擎逐项计算 `Σ(利润×PE×概率)`，结果归入L2。
  - 路线D需 `net_assets`+`roe`，路线E需 `revenue`+`gross_margin`。

### 引擎输出 schema

引擎运行后输出 `cache/calc_results/<code>_<YYMMDD>.json`：

```json
{
  "_meta": {"code": "300442", "name": "润泽科技", "engine_version": "1.0-dev", "generated": "2026-05-22 12:00"},
  "pe_2026e": {"final_pe": 35.0, "pessimistic_pe": 31.5, "optimistic_pe": 42.0, "details": "PE三步走:\n  第1步: ..."},
  "pe_2027e": {"final_pe": 35.0, "pessimistic_pe": 31.5, "optimistic_pe": 42.0},
  "pillars_2026e": [{"route": "B", "pessimistic_value": 935.55, "base_value": 1039.5, "details": "路线B: ..."}],
  "pillars_2027e": [...],
  "matrix_2026e": {
    "matrix_rows": [{"pillar_name": "AIDC", "layer1": {...}, "layer2": {...}, "layer3": {...}}],
    "layers_summary": {"layer1": {"pessimistic": 936, "base": 1040, "pricing_status": "充分定价"}, ...},
    "total": {"pessimistic": 1108, "base": 1276, "optimistic": 1696}
  },
  "matrix_2027e": {...},
  "divergence_2026e": {"divergence_pct": 20.3, "classification": "大部分定价", "max_val": 32.9, "min_val": 26.9, "mean_val": 29.7, "count": 4},
  "divergence_2027e": {...},
  "reverse_check": {"triggered": false, "discount_rate": 0.95, "triggers": [], "checks": {}},
  "ranking_row": {"股票代码": "=\"300442\"", "悲观估值_元": "67.78", "基准估值_元": "78.09", ...},
  "report_fragments": {"overview_table": "### 双年估值对照\n...", "matrix_summary": "### 2026E 三层汇总\n...", "reverse_check": "🔍 反向检查..."}
}
```

**报告编写时引用**：
- `pe_2026e.details` → 复制到"计算逻辑"节
- `matrix_2026e.layers_summary` → 复制三层汇总表
- `report_fragments.overview_table` → 复制双年估值对照表
- `report_fragments.matrix_summary` → 复制三层汇总
- `reverse_check` → 写反向检查段落
- 每个 pillar 的 `details` → 复制到对应支柱的计算逻辑节

### 核心函数签名

以下签名足以重建脚本或理解引擎内部逻辑：

```
calc_pe_three_step(comparable_median, comparable_lower, comparable_upper,
                    company_growth, comp_growth, growth_quality,
                    qualitative_adjustments) → {final_pe, pessimistic_pe, optimistic_pe, details}
  公式: step2 = clamp(median × min(growth_ratio, 2.0), lower, PEG_cap × growth)
        step3 = clamp(step2 × (1 + Σadj), lower, PEG_cap × growth)
        悲观PE = final_pe × 悲观折扣(growth_quality)
        乐观PE = max(upper, final_pe × 1.2)

calc_pessimistic_params(growth_quality, base_pe, consensus_np,
                         consensus_np_lower, deducted_np) → {pessimistic_pe, pessimistic_np, rule_description}
  规则: structural→压PE(×0.90)不压利润; cyclical→两端各压; mature→压利润不压PE

// 路线分派: ROUTE_DISPATCH = {A, A1, B, C, D, E, F}
calc_route_b_valuation(pillar, pe_result, total_shares, year) → {pessimistic_value, base_value, optimistic_value, ...}
  公式: 市值 = 净利 × PE × 折扣系数(market_position)
  路线C复用B + DCF注释(WACC 8-12%, g 2-3%)
calc_route_d_valuation(pillar, params, total_shares, year) → {...}
  公式: 市值 = 净资产 × 可比PB×(公司ROE/可比ROE) × 定性调整 × 折扣系数
calc_route_e_valuation(pillar, params, total_shares, year) → {...}
  公式: 市值 = 营收 × 可比PS×(公司毛利率/可比毛利率) × 折扣系数
calc_route_f_valuation → 同D（简化版，无ROE调整）
calc_route_a_valuation(pillar, params, total_shares, year) → {...}
  公式: EV = EBITDA × EV/EBITDA, 股权价值 = EV - 净负债, 市值 = 股权价值 × 折扣系数
calc_route_a1_valuation(pillar, pe_result, total_shares, year) → {...}
  公式: 核心(路线B) + Σ Type B管道(Σ 项目利润×PE×概率)

calc_type_b_pipeline(items) → {pessimistic_value, base_value, optimistic_value, breakdown}
  公式: Σ(项目利润 × PE × 概率), 悲观/乐观用概率±0.15

calc_divergence(consensus_values) → {divergence_pct, classification, max_val, min_val, mean_val, count}
  公式: (max-min)/mean × 100%

aggregate_matrix(pillars, pillar_results) → {matrix_rows, layers_summary, total}
  逻辑: 每支柱取 layer1/layer2, layer3从pillar.layer3_items汇总, 按层合并

migrate_params_2027e(params, pe_result_2026e) → {pe_2027e, pillars_2027e, migration_notes}
  规则: PE不下调, 悲观折扣沿用, L3概率+0.15, 管道概率+0.15

reverse_check(current_price, pess_ps, base_ps, opt_ps, growth_quality, market_position) → {triggered, discount_rate, triggers, checks}
  触发: 折扣率>2.0/<0.3, >1.5且龙头, <0.4, 区间宽度>5x

generate_ranking_row(code, name, current_price, pess_ps, base_ps, opt_ps, pe, method) → dict
update_ranking_csv(ranking_row) → int
generate_report_fragments(result, total_shares, current_price) → dict
run_valuation(params) → 完整结果dict
```
