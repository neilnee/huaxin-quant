# 模型三：深度估值模型（自执行指令）

- **版本管理**: 由 Git 分支与提交历史管理，文件名不再携带版本号
- **最近更新**: 2026-07-30
- **核心哲学**: 脚本编排、校验、存档和渲染；LLM 只完成小范围、结构化的研究判断。
- **触发方式**: 用户主动按单只标的触发，不消费 Bloom，也不自动给出交易动作。
- **输出**: `reports/valuation/<code>_<name>.md` + `reports/indexes/valuation_index.csv` + `reports/indexes/valuation_ranking.csv` + 当次可审计运行包 + Dashboard 数据包。
- **配套脚本**: `scripts/run_valuation.py`（端到端总控）+ `scripts/valuation_pipeline.py`（v3 五阶段编排器）+ `scripts/dashboard_valuation.py`（Dashboard 适配器）+ `scripts/valuate.py`（阶段零）+ `scripts/calc_valuation.py`（计算引擎）| 参考手册: `03-valuation-ref.md`

---

## 总览

```
阶段零（脚本）→ 阶段一（业务拆解+自算）→ 阶段二（共识+分歧+引擎计算）
→ 阶段三（预期差）→ 阶段四（催化剂）→ 阶段五（报告输出+检查）
```

**分工原则**：
- **脚本**：财务数据解析、指标计算、决策树、漏斗信号、PE 三步走、悲观参数应用、矩阵汇总、分歧度计算、2027E 迁移、反向检查触发、CSV 输出
- **LLM**：业务线识别与拆分、可比公司选择、增长质量分类、定性调整判断、共识数据研报提取、预期差发现、叙事写作、催化剂日历

---

## V4 简化共识协议（当前有效）

V4 覆盖本文件后续 V3 中关于分支柱估值、可比公司 PE、Layer 2/3 加值和页面结构的旧规则。旧字段仅用于兼容历史运行包，不得主导新分析或新页面。

模型三首先是机构研报共识阅读器。公开信息通常已经进入机构盈利预测；没有额外一手信息时，不得重新推演经营利润，也不得把业务拆分强行转换为分部估值。

### 页面与分析结构

1. 公司核心摘要：在估值结论之前，以结构化总结栏展示公司画像、盈利共识、增长逻辑、关键不确定性和跟踪重点。
2. 估值结论与一致性预期：共用一个年度切换，先展示行情日期、三情景价格和区间轨道，再展示公司整体口径的净利润、目标 PE、估值和取值说明；不重复展示目标价。
3. 业务支柱拆分：列出主营、增长业务、资产平台和潜在事项，说明业务实质、进展和利润形成逻辑；不分配利润或估值。
4. 机构利润预测与关键假设：逐家展示双年利润、从经营驱动到收入及净利润的证据链，以及预测成立所依赖的假设、后续跟踪指标和失效信号，回答“这家机构为什么得到这个利润、以后用什么验证”。
5. 机构一致预期明细表：展示逐机构双年营收、净利润、报告日期和目标价，作为横向底表；`profit_scope` 等内部清洗枚举不得直接展示。
6. 关键分歧：只解释同口径机构预测区间，不自动调整利润、PE或另加估值。
7. 后续验证节点：统一承接原催化剂和验证事项，说明时间、事件、成功与失败含义。
8. 主要风险。

页面删除“PE如何形成”“估值锚与可比”“独立事项估值状态”“核心假设汇总”“近期催化剂”“已验证事实”和“证据目录”。`source_ids`、证据分级和缓存定位继续保存在运行包中用于契约校验与内部审计，但公司报告页不得展示证据 ID、索引按钮、证据标签或原始证据目录。

估值结论只保留当前价、双年三情景价格和区间轨道；原单句 `core_judgement` 由前置公司核心摘要替代，不再单独展示，也不再展示“主要估值支柱/首要风险/最近验证节点”摘要卡。一致性预期估值区只保留年度切换和三情景明细表，不展示重复的双年度估值方块；明细表不展示内部情景对应机构，机构来源仅保留在运行包中。

报告表格的文本列统一左对齐，数值列的表头和数据统一右对齐；机构一致预期明细中的数值 `0` 视为未披露并显示 `—`，不得误导为机构明确预测零值。

阶段五A1必须输出 `investment_thesis.company_summary`，包含 `company_profile`、`earnings_consensus`、`growth_logic`、`key_uncertainties`、`tracking_focus` 五项；每项为 `{text,source_ids}`。公司画像必须说明主营业务、已披露经营规模或公司阶段；盈利共识必须包含历史经常性利润或双年机构预测的具体数字；增长逻辑必须指向有证据的业务、产能或经营变量；不确定性和跟踪重点必须与机构利润假设相连。禁止使用“盈利稳健、前景广阔、长期成长”等无量化或无业务指向的套话代替公司总结，缺少数据时必须明确写出缺口。

阶段五A1还必须输出 `business_pillar_analysis[]`，按 `source_pillar_id` 一对一覆盖阶段一完整业务地图，专供业务价值画像展示，不参与利润分配或估值计算。每项包含：

- `role_in_company={text,evidence_status,source_ids}`：说明该业务是当前利润基础、增长引擎、产能储备、资产平台、期权还是历史事项。
- `earnings_model={text,evidence_status,source_ids}`：说明收入和利润如何形成，披露不足时写清可确认的传导关系与缺口。
- `core_metrics[]`：每项为 `{name,value,unit,period,scope,evidence_status,source_ids}`；只记录公司或机构明确披露的数据，`scope` 必须说明是分部、项目还是公司整体参考。
- `growth_potential={text,horizon,drivers[],constraints[],evidence_status,source_ids}`：说明增长来源、兑现周期和约束；缺少上架率、价格、利润率等输入时不得测算增长上限。
- `valuation_anchor={level,text,evidence_status,source_ids}`：`level` 只能为 `core_anchor|important_component|growth_option|observation|excluded|unclear`。没有直接分部利润和估值时不得给出估值贡献百分比。
- `tracking_metrics[]`：每项为 `{name,direction,why,source_ids}`，必须能够验证增长和估值锚定是否成立。
- `data_gaps[]`：明确列出分部收入、利润、利润率、业务量、价格等缺失项。

页面业务支柱卡只展示上述业务价值画像，删除“拆分逻辑、利润关系、2025A参考”等分类过程字段。阶段五原 `business_pillars[]` 继续只服务于阶段二估值支柱继承和计算映射，不得替代阶段一业务地图。

业务支柱页面必须保留阶段一完整业务地图，并由 Dashboard 适配器按 `business_pillars[].pillar_id = business_item_coverage[].source_pillar_id` 合并阶段二的机构覆盖结论、原因和证据。不得用阶段五合并后的公司整体估值支柱替代业务地图；只有历史运行包确实缺少 `business_item_coverage` 时才能显示“待结合逐机构研报判断”。

### 一致性预期估值

- 三家机构只是发布底线，不是提取上限。逐篇读取全部时效与完整性合格的研报。
- 先统一年度、归母净利润口径和非经常性损益口径，再清洗异常值。明确包含重大非经常性收益的预测不得混入持续经营共识；口径未知须明确标注。
- 研报中的“当前股价对应 PE”只能标记为 `current_implied`，严禁作为估值倍数。
- 只有机构明确目标 PE，或由机构目标价和同一份研报预测利润可靠换算的目标价隐含 PE，才是估值样本，分别标记 `target_explicit`、`target_implied`。没有目标估值结论的研报只贡献利润预测。
- 每家机构的利润与目标估值保持为同一份研报的完整组合，不得跨机构拼接利润和 PE。
- 清洗后按机构估值组合的下沿、中位、上沿形成悲观、基准、乐观。每个情景保存对应机构、利润、目标 PE、目标价和证据。
- 合格利润样本少于3家时标记 `insufficient_consensus`；合格目标估值组合不足2家时标记 `insufficient_valuation_consensus`，不得用当前隐含 PE、可比公司或固定比例补造目标估值。
- 悲观估值必须小于等于基准估值，基准估值必须小于等于乐观估值；倒挂结果拒绝发布。

### 业务地图与逐机构利润逻辑

阶段一继续按业务实质拆分完整业务地图，包括已运营业务、在建/新增产能、海外项目、资产平台、REITs/并购和历史事项。业务地图只回答“利润可能从哪里来”，不决定公司级估值如何分配。

阶段二逐机构输出 `profit_analysis`：`institution`、`report_date`、`np_2026e`、`np_2027e`、`profit_scope`、`summary`、`profit_logic`、`key_assumptions[]`、`included_business_items[]`、`excluded_or_unclear_items[]`、`risks[]` 和 `source_ids`。

`profit_logic={status,summary,steps[],missing_links[]}`。`status` 只能为 `complete|partial|endpoint_only`；`steps[]` 按经营驱动、业务量/价格、收入、利润率/成本费用、归母净利润的实际披露顺序记录，每项包含 `stage`、`statement`、`period`、`value`、`unit`、`evidence_level` 和 `source_ids`。`evidence_level` 只能为 `explicit|qualitative`：研报明确披露的数值才可标 `explicit`，只说明方向时标 `qualitative` 且 `value/unit` 留空。2026E、2027E 归母净利润终点必须与同机构 `institution_forecasts` 完全一致；中间环节未披露时写入 `missing_links`，不得倒推或补造经营参数。

`key_assumptions[]` 每项包含 `assumption`、`tracking_metric`、`expected_direction`、`explicit_target`、`timeframe`、`failure_signal`、`tracking_origin` 和 `source_ids`。`tracking_origin` 只能为 `report_explicit|derived_monitoring`；研报没有明确数值目标时 `explicit_target` 必须为空。可以把研报假设映射为可观察的后续指标，但必须标记 `derived_monitoring`，不得把跟踪建议伪装成机构原话，也不得自行添加数值阈值。研报没有披露利润形成逻辑或成立假设时允许输出空项或 `endpoint_only`，禁止为了结构完整而推演。

业务支柱与机构假设只建立“多数机构已纳入 / 部分纳入 / 普遍未纳入”等说明性关系，不得把公司整体净利润机械拆给各支柱。

### 分歧、验证与阶段边界

- 非经常项目是否纳入属于口径差异，放入样本清洗说明，不作为经营分歧。
- 至少两家有效机构对同一经营变量给出有原文支持的不同假设，才形成关键分歧。
- 每项分歧列出机构观点、对应预测数值、明确理由、影响方向、证据和验证节点。
- 只看到利润或营收结果不同，不能反推根因；原文未说明时标记“原因未披露”。
- 阶段三之后的公告或新闻不并入机构分歧，只作为研报发布后的新增变化或验证材料。
- 阶段四只形成后续验证节点和主要风险，不再单列催化剂页面模块。
- 阶段五不得重新选择 PE，也不得用可比公司、当前隐含 PE 或后续公开信息覆盖阶段二一致性估值。

### 阶段三证据时限与增量门禁

阶段三只判断机构一致预期形成后的新增变化，不得把阶段二已经知道的事项再次计价。脚本从实际进入阶段二悲观、基准、乐观估值组合的机构中提取经原始证据日期校验的研报日期，并固定形成两个边界：有效研报日期的上中位数和最晚日期。被排除的机构、日期缺失或与引用证据不一致的研报不得参与边界计算。

动态专题证据按单条材料自身发布日期分为四类：

- `baseline_context`：早于上中位研报日，视为阶段一、二已经覆盖的背景，只保留审计，不送入阶段三阅读。
- `overlap_window`：不早于上中位研报日且不晚于最晚研报日，部分机构可能已经知悉；只保留审计，不送入阶段三阅读，也不得作为新增触发证据。
- `post_consensus`：晚于最晚有效研报日，是阶段三增量判断的必要触发证据，但时间较新本身不代表未计价。
- `date_unverified`：发布日期缺失、非法或晚于当前日期；只进入审计隔离区，不送入阶段三阅读，也不得支持估值。

单条材料不得继承同一搜索缓存中其他结果的最新日期，搜索抓取时间也不得替代发布时间。同日材料归入 `overlap_window`。阶段三阅读集合只能包含 `post_consensus`；业务背景和机构已知信息直接复用阶段一、二结构化结论，不得为补背景再次阅读旧专题原文。`probabilistic_event` 必须引用 `post_consensus` 证据，并显式输出 `baseline_overlap=not_included`、`overlap_reason` 和 `incremental_evidence_ids`；否则只能归为 `narrative_observation` 或不进入阶段三项目。

若没有任何 `post_consensus` 证据，脚本直接生成 `no_post_consensus_evidence` 结果，不调用阶段三研究 LLM，不得把“未发现合格新增证据”写成“公司没有预期差”。每次运行均须保存门禁版本、机构样本、双边界、各类材料数量和结果级分类；门禁规则、机构样本或证据集合变化时，续跑必须使阶段三至阶段五失效并重做。

### 运行包契约

阶段二继续保存 `stage_2_readings.json`、`stage_2_institutions.json`、`stage_2_model_inputs.json`、`stage_2_consensus.json` 和 `stage_2_baseline.json`。二A输出全量机构预测、PE语义、逐机构利润逻辑和排除清单；二B只输出公司级双年三情景一致性估值，不生成分业务估值。

研究卡保留 `investment_thesis`、`business_pillar_analysis`、`business_pillars`、`consensus`、`market_divergences`、`verification_nodes`、`risks` 及审计字段。`comparables`、旧 `assumption_ledger`、`narrative_options` 为兼容字段，不再是页面和公司级一致性估值的必需输入。

金额统一使用亿元，股本使用亿股，股价使用元，PE使用倍数。所有研报结论、利润驱动、分歧、验证节点和风险必须回溯到 `source_id`。

## V3 研究与报告协议（历史兼容）

模型三不是“给出一个 PE 和目标价”的计算器，而是用利润支柱解释市场正在交易什么、尚未交易什么，以及后续由什么事实验证。计算引擎只负责将经过研究的利润和概率换算为三情景估值；不能代替业务、预期和分歧判断。

### 报告的分析主线

```
业务与行业位置
  → 利润支柱（主营 / 差异化业务 / 叙事期权）
  → 机构一致预期（已定价的利润基线）
  → 市场分歧（同一事实的不同定价）
  → 预期差（尚未充分定价的新增事实）
  → PE / PB / EV-EBITDA 等估值条件
  → 双年三情景估值 + 验证节点
```

**三层含义必须固定**：

| 层级 | 回答的问题 | 估值来源 |
|------|-----------|----------|
| Layer 1：一致预期 | 主营业务按当前已知信息能赚多少？ | 至少 3 家未过期机构的 2026E+2027E 预测 |
| Layer 2：分歧定价 | 同一事实为何有乐观/悲观不同解读？ | 机构分歧、行业位置、可比估值和研究判断 |
| Layer 3：叙事期权 / 预期差 | 哪些新增事实尚未充分反映到利润？ | 订单、产能、产品、管理层行动等；以利润×倍数×概率量化 |

Layer 3 不是任意主题的溢价：没有明确业务实质、潜在利润、概率理由、证据和验证节点的叙事不得进入估值，只能留在观察清单。

### 利润支柱与估值路线

1. 先按业务实质拆支柱，而非按财报科目机械拆分。主营、增长业务、重资产/轻资产/周期业务、以及兑现路径明显不同的业务应单独成为支柱。
2. 同一支柱内只有在增速差 <20%、毛利率差 <15pct、驱动和估值方法相同的业务才能合并；结论及原因必须写入报告。
3. 每个支柱必须有 2025 基准、2026E/2027E 利润桥、核心驱动、机构预测映射、估值路线和至少一个验证节点。
4. 支柱的 Layer 1 是共识主营利润；Layer 2 是对共识区间或估值倍数的有依据调整；Layer 3 是未充分定价的利润期权。不得把同一利润在三层重复计入。
5. PE 由可比公司、增长质量、行业景气、市场地位和叙事兑现度共同决定。报告须写明悲观/基准/乐观三种条件分别改变的是利润、PE、概率或其组合，不能只给一个黑箱倍数。

### 强制报告结构

1. **结论与市场交易主题**：最新有效交易日收盘价及价格日期、2026E/2027E 三情景区间、基准空间；一句话说明市场当前主要交易的利润支柱和叙事。
2. **业务与利润支柱地图**：行业位置、业务拆分/合并理由、每个支柱在总利润和总估值中的角色。
3. **Layer 1 — 一致预期主营**：至少 3 家机构的双年预测表、利润桥、可比公司表、估值方法和市值→股价换算。
4. **Layer 2 — 市场分歧**：乐观与悲观叙事、分歧根因、我方取值、对利润/PE/估值的影响。
5. **Layer 3 — 叙事期权与预期差**：公告/订单/产能/产品/管理层等搜索记录；每项的潜在利润、倍数、概率、定价状态和是否计入估值。无有效项目也必须明确记录已搜索且未计入。
6. **双年三情景估值汇总**：支柱×Layer 矩阵、2026E/2027E 对照、敏感性和反向检查。
7. **验证节点与催化剂日历**：每个主营假设、分歧和叙事项目至少一个可观察事件，说明验证什么、失效条件和预期影响。
8. **风险与更新记录**：风险对应具体支柱；明确下次更新需回答的问题。

报告的正文必须按以上顺序渲染；事实、假设和证据目录仅作为各章节的依据，不得替代章节本身。

### 研究卡数据契约

`research_card.json` 除 `facts`、`consensus`、`comparables`、`calc_params` 外，必须包含：

- `investment_thesis`：市场交易主题、公司所处阶段、核心判断及证据。
- `business_pillars[]`：支柱业务实质、拆分理由、路线、利润桥（2025A→2026E→2027E）、对应共识、估值驱动和验证节点。
- `market_divergences[]`：乐观/悲观叙事、分歧根因、定价状态、我方判断、影响 Layer 2 的参数及证据。
- `narrative_options[]`：Layer 3 项目或明确的“无显著预期差”结论；每项包含潜在利润、倍数、概率、是否计入估值、证据和验证节点。
- `verification_nodes[]`：日期/窗口、待验证事件、关联支柱、验证成功/失败的含义及证据。

`calc_params.pillars[]` 只是上述研究结论的数值映射。凡是进入 `calc_params` 的支柱、Layer 2/3 项目，必须能回溯到对应研究卡项目和 `source_id`。

映射必须只使用标准枚举，禁止按公司名、行业名或项目关键词硬编码：`classification` 只能为 `core_operating`、`asset_pipeline`、`narrative_option`、`non_recurring`；`valuation_route` 为 A~F/A1；`profit_timing` 为 `realized`、`contracted`、`pipeline`、`long_term`；`accounting_treatment` 为 `recurring`、`non_recurring`、`consolidated`。脚本据此决定进入核心 `pillar`、Type B 管道、Layer 3 期权或排除项；枚举缺失时必须失败，不得默认归类。

研究卡完成状态统一为 `ready`；兼容上游 LLM 返回的 `complete`，编排器在校验前将其标准化为 `ready`，其他状态一律视为证据不足。

### 五阶段脚本化执行（不得简化为一次 LLM 汇总）

编排器必须按旧指令卡的阶段顺序逐节点执行。每个节点只处理自己的研究问题、读取对应专题的完整原始证据、输出独立 JSON，并由下一节点引用；不得把全部问题压缩成一条 prompt 或把每个搜索缓存截成短摘要。

| 阶段 | 脚本职责 | LLM 节点输出 | 必须形成的运行包文件 |
|------|----------|-------------|----------------------|
| 零 | 拉取财务、决策树、漏斗与 briefing | 无 | `briefing.json` |
| 一 | 读取年报/主营/行业证据；逐批完整阅读后识别业务、资产平台、并购和资本结构 | 业务拆分、逐支柱利润桥、路线、**专题检索计划** | `stage_1_readings.json`、`stage_1_business.json`、`search_plan.json` |
| 二 | 拉取并逐批完整读取新研报、可比资料；逐机构抽取双年预测并建立机构基准估值模型 | 全量有效共识、分支柱双年三情景输入、估值方法与假设账本 | `stage_2_readings.json`、`stage_2_consensus.json`、`stage_2_baseline.json` |
| 三 | 按专题计划检索并逐批完整读取公告、订单、REITs、并购、产能、海外、管理层和产业链 | 每个项目的业务实质、利润、倍数、概率、Layer 2/3 映射或排除理由 | `stage_3_readings.json`、`stage_3_expectations.json` |
| 四 | 逐批读取验证材料并汇总各阶段结论 | 催化剂日历、验证节点、失效条件、风险 | `stage_4_readings.json`、`stage_4_catalysts.json` |
| 五 A | 合并前四阶段研究结论并校验叙事契约 | 不新增事实；只输出研究结论、共识、可比、事实与风险，不输出计算映射 | `stage_5_research.json` |
| 五 B | 读取已验证的五 A 研究结论，生成紧凑参数映射 | 只输出 `valuation_inputs` 和各研究项目的 `calculation_mapping` | `stage_5_mapping.json` |
| 五 C | 确定性合并、校验、计算和渲染 | 不调用 LLM；组装研究卡并生成引擎参数 | `research_card.json`、`calc_params.json`、`calc_results.json` |

阶段五 A、五 B 必须相互独立并可断点复用。五 A 内部按“主营与共识、分歧与期权、验证与事实”分成三个有界请求；五 B 内部按“PE 输入、支柱利润映射、分歧与期权映射”分成三个有界请求。各子文件分别保存后由脚本合并，避免单张长研究卡触发模型输出上限，也避免公司级净利润、Layer 2 和期权字段相互串扰。五 B 不得再次携带完整原文证据，只读取五 A 研究结论、阶段二/三结构化结论及证据目录。脚本按 `research_id` 确定性合并，禁止让单次 LLM 同时生成长篇研究卡和全部计算参数。`--rerun-stage5` 复用已通过校验的子文件，只重跑缺失或不合格部分。

阶段二同样必须拆成两个有界请求：二A只生成逐机构预测、排除名单、可比和估值锚口径分类，保存 `stage_2_institutions.json`；二B只根据二A与阶段一业务地图生成估值支柱和假设账本，保存 `stage_2_model_inputs.json`。脚本合并为 `stage_2_consensus.json` 并计算 `stage_2_baseline.json`。断点恢复时复用已完成的研报分批阅读和二A/二B子文件，不得因汇总JSON失败而重新搜索或重读原文。

**数值与渲染契约**：营收、利润、净资产、市值及估值增量统一使用“亿元”，股本使用“亿股”，股价使用“元”，概率使用 0~1 小数，增速和毛利率使用百分比数值（如 29.5 表示 29.5%）。`facts`、`assumptions`、`risks`、`catalysts` 必须是 `{statement,source_ids}` 对象数组；渲染层只认 `calculation_mapping`，不得兼容或生成 `calc_mapping`。任何利润桥绝对值超过合理亿元范围、字符串代替对象、缺少渲染字段或非法单位都必须在调用引擎前失败。

**可比与 PE 语义门禁**：公司可比必须注明 `anchor_type=company`、PE/增速/毛利率的预测期与业务相关性，且关键数值不得为空；行业均值不能冒充单家公司。机构目标估值可用 `anchor_type=institution_target` 单独列示。阶段五 B 必须记录阶段二建议 PE、最终采用 PE、选择方法和偏离理由；最终 PE 相对阶段二建议值偏离超过 20% 且无显式证据理由时拒绝发布。经研究确认需要覆盖三步走结果时，必须显式输出 `pe_override`，不得靠不相关低 PE 可比隐式压低估值。

`cross_check_only` 机构锚必须全部进入 `excluded_anchor_names`，不得被阶段五重新描述为“已扣除独立事项的同口径PE”。当同口径 `direct_usable|convertible` PE与带数值的公司可比合计不足2项时，不得执行常规可比三步走；脚本使用交叉检查PE的保守下半区中位数形成显式 `override` 基准，并以基准的0.9/1.0/1.2倍形成情景倍数，标记为“锚不足的保守回退”，而非机构目标估值。若连3个交叉检查PE都不足则拒绝发布。

**机构分歧口径**：分歧度只根据逐机构的 2026E/2027E 净利润预测计算，不得根据不同业务支柱利润计算。行情刷新后，运行包中的 `briefing.market_quote`、`manifest.market_quote`、`calc_params.meta` 和最终报告必须保持同一价格日期与价格值。

利润桥必须声明 `profit_metric=net_profit|gross_profit|operating_profit`，报告按真实口径展示，禁止把毛利标成净利润或泛称利润。PE 倍数分歧一旦进入 `valuation_inputs` 的悲观/基准/乐观 PE，就必须在 Layer 2 映射中排除，不得以市值增量再次计价；阶段五 B 的分歧映射须显式给出 `driver_type=profit|multiple|margin|other` 供脚本去重。

**动态专题检索是强制的**：阶段一发现的重大资产重组/收购、REITs/资产证券化、非经常性项目、在建产能、海外节点、重点客户或新产品，必须各自生成专题查询。不得以固定的低数量上限删减独立项目；仅可合并事实、估值路径和验证节点完全相同的重复查询。阶段三须逐专题输出“计入 Layer 2/3”或“未计入及原因”，不能只写一条泛化公告摘要。

**阶段二基准模型契约**：3家机构只是最低发布门槛，不是提取上限。阶段二必须逐篇列出全部候选研报，按六个月时效、双年预测完整性和利润口径形成有效/排除名单；每家保存原始归母利润、持续利润、非经常项目、`profit_scope=recurring|includes_non_recurring|unknown`、判断依据和证据。机构PE或目标估值还必须记录 `valuation_scope`、包含/排除的项目ID及 `pe_usability=direct_usable|convertible|cross_check_only`。只有利润范围与估值范围一致的 `direct_usable` 锚可以直接进入支柱倍数中枢；可可靠扣除独立事项价值的 `convertible` 锚转换后使用；其余只作交叉验证。

阶段二在阶段一完整经济业务地图之上生成 `stage_2_baseline.json`。机构基准估值模型不是“主营PE表”，必须把核心经营业务、第二核心业务以及已有可靠机构/公告依据的资产事件、并购、REITs、处置收益和其他独立事项分别列成支柱。固定包含：

- `pillars[]`：稳定 `pillar_id`、`pillar_type=operating|independent_event`、名称、利润口径、估值方法，以及2026E/2027E悲观/基准/乐观的利润/项目收益、PE或其他倍数、实现概率、各参数理由和证据；估值由脚本按 `profit × multiple × probability` 确定性计算。经营支柱概率固定为1；独立事项必须逐情景给出0~1概率和阶段依据，不得机械套用统一±比例。明确有启动亏损、处置损失或现金成本的支柱允许负利润及负估值贡献，但倍数不得为负。
- `assumption_ledger[]`：`assumption_id`、`pillar_id`、年度、指标、基准值、单位、成立条件、信息截止日和证据；每项必须以 `included_in_baseline` 声明是否已纳入阶段二基准估值，并以 `pricing_channel=profit|multiple|standalone|none` 标明进入利润、倍数、独立项目或尚未进入。`pricing_reason` 说明判断依据。`quantification_status=quantitative|qualitative|missing_input` 只作为辅助属性，不能替代定价状态。
- `totals`：逐年度三情景支柱估值加总。阶段二只形成机构基准模型，不吸收阶段三之后的增量证据。

**业务地图与估值支柱准入必须分开**：阶段一 `business_pillars[]` 是完整经济业务候选地图，不代表每项都能独立估值；阶段二必须额外输出 `business_item_coverage[]`，逐一覆盖阶段一的稳定 `pillar_id`，且只能选择一个 `valuation_role`：

- `valued_operating`：已有持续经营利润及匹配估值方法，必须对应一个 `operating` 估值支柱并进入双年三情景表。
- `valued_event`：已有可量化收益/资产价值、客观进度节点及独立增量口径，必须对应一个 `independent_event` 估值支柱；悲观情景允许为零，但基准/乐观不得无故缺失。
- `merged_component`：确有经济贡献但无法从公司级共识可靠拆出，必须声明合并到哪个经营估值支柱及 `overlap_status=inside_target`，页面只能称“业务组成”，不得冒充独立估值支柱。
- `narrative_observation`：只有方向、概念或远期可能性，目标年度利润/资产价值、倍数或独立口径任一缺失；不进入估值，仅保留验证节点。
- `historical_excluded`：历史已实现且无可证明的剩余资产价值，只作口径审计，不进入前瞻估值。

每项还必须给 `quantification_status=quantitative|qualitative|missing_input`、`overlap_status=incremental|inside_target|not_applicable`、目标 `valuation_pillar_id`（如适用）、原因和证据。`valued_operating|valued_event` 必须是 `quantitative`；`valued_event` 必须是 `incremental`。脚本须检查阶段一项目完整覆盖、目标支柱存在且类型匹配。凡在页面被称为“估值支柱”的项目必须在估值表中有非缺失估值行；不能独立估值的项目必须改称业务组成、叙事观察或历史排除项。

悲观和乐观默认沿用基准利润、只调整倍数；只有存在明确量化依据时才允许利润变化。独立事项可以保持项目收益不变、按审批/交割阶段调整概率。利润、倍数和概率的每次变化分别说明原因，同一事实不得同时重复调整利润、倍数或独立项目。分部利润缺少机构直接披露时必须标记推算方法与置信度，不得冒充机构分部共识。

**经营利润总量守恒是硬门禁**：阶段二先从 `profit_scope=recurring|unknown` 且未发现重大非经常项目污染的全部双年机构预测中，按异常值清洗后确定公司级持续经营利润控制数；悲观/基准/乐观分别取清洗样本的下沿/中位/上沿，并保存样本机构、范围判断和计算方法。`includes_non_recurring` 不得进入该控制数；“研报未明确写不含非经常损益”只能标记 `unknown`，不能据此擅自判为 `includes_non_recurring`。

业务地图可以拆分而估值支柱不必强行拆分。只有机构或公司披露能够分别支持各经营业务的净利润/经营利润、分配方法和相匹配估值倍数时，才允许多个 `profit_basis=direct_segment_forecast` 经营估值支柱；其逐情景利润之和仍必须与公司级持续经营利润控制数一致。缺少可靠分部利润或分部估值锚时，IDC/AIDC等业务继续在业务结构和假设账本中分别展示，但估值合并为一个 `profit_basis=company_consensus` 的“核心持续经营业务”支柱。禁止根据收入、毛利或主观权重拆出若干净利润后造成利润凭空增加或减少。

脚本必须逐年度、逐情景执行利润对账并输出 `operating_profit_reconciliation`：公司级控制利润、经营支柱利润合计、差额、样本机构及处理方式。单一合并经营支柱由脚本以控制利润覆盖LLM估算值；多个有直接分部证据的支柱仅允许在5%以内按比例对齐控制总量，超过5%直接拒绝发布。该门禁发生在估值计算之前，不能用提高或降低PE补偿利润拆分错误。

阶段三之后发现的已计价独立事项仍在同一估值模型中新增为独立支柱，并标记 `pricing_stage=incremental_evidence`；不得在页面下方另建一套Layer 2/3估值。市场分歧只用于解释基准模型中利润、倍数或概率的三情景差异，不再允许以无法回溯的总市值增减重复进入计算。

阶段三必须把专题事实拆成最小可估值单元，不得把“现有平台持续收益”和“未来扩募/并购/投产”混成同一项目。每个项目固定标记 `valuation_role=baseline_component|probabilistic_event|narrative_observation|historical_excluded`：`baseline_component` 只解释阶段二已计价利润，不能再次加值；`probabilistic_event` 必须证明相对阶段二基准是增量，提供2026E/2027E悲观、基准、乐观的收益/资产价值、倍数、概率及各自证据，并强制进入统一估值表；`narrative_observation` 与 `historical_excluded` 不进入估值。概率只表达事项是否发生，不能替代缺失的利润、资产价值或倍数。阶段五不得将通过该门禁的 `probabilistic_event` 降级为 `exclude`。

独立事项必须区分 `profit_basis=future_event|residual_asset_value|historical_realized`。未来扩募、处置或并购按未来收益/资产价值与概率估值；已实现事项只有在能够建立“期末仍留存的净现金/净资产—已用于偿债或再投资金额—尚未包含在其他估值支柱中的剩余价值”桥接时，才能标记 `residual_asset_value`。仅有往年已确认非经常损益的 `historical_realized` 项目，在所有前瞻年度自动归零，不得把历史利润再次加到2026E/2027E市值中。

页面以“已定价/未定价”为假设账本的主状态；这里的“定价”仅表示是否已纳入本估值模型，不代表股票市场价格已经充分反映。已定价项目须区分 `institution_baseline` 与 `incremental_evidence`，并能回溯到利润、倍数、概率或独立项目中的唯一入口；未定价项目不得暗含在估值中，并须说明缺失输入或暂不纳入的原因。机构基准估值表是页面唯一的估值分项总表，表下不重复展示悲观/基准/乐观三张汇总卡。

**证据粒度**：`evidence.json` 的一个 `source_id` 对应一条研报、公告或新闻结果，而不是整个搜索缓存；必须保留标题、日期、来源、缓存文件、结果序号及完整原文。运行只能装载本次固定检索和本次专题计划明确产出的缓存文件，不能按宽泛文件名关键词扫描历史缓存；同一公告/研报结果须以来源标识、标题和日期去重。阶段三逐条阅读原始专题证据；阶段四复用阶段三的阅读结论和证据引用，只为缺失的验证事件补充增量材料，不得重复逐条阅读阶段三原文。

**阶段三证据归并（强制）**：在阶段三原文阅读前，脚本先按结果级唯一键去重，并只保留本次专题计划明确产出的缓存文件。不得新增“先由 LLM 阅读全文再做筛选”的预筛节点；阶段三 LLM 直接对该去重证据集进行项目研究。

**LLM 边界**：LLM 可完整执行旧指令卡要求的业务判断、利润拆解、共识提取、分歧和预期差研究；脚本不得用默认值、泛化支柱或“仅观察”替代未完成研究。LLM 节点失败时仅该阶段失败，运行包保留已完成阶段和可复用证据。

---

## V2 执行协议（运行与审计约束）

### 端到端总控（默认入口）

日常执行模型三时，默认使用总控脚本，不再由对话或人工逐条串联阶段零、五阶段编排和恢复命令：

```bash
python3 scripts/run_valuation.py --code 688285
python3 scripts/run_valuation.py --code 688285 --refresh
python3 scripts/run_valuation.py --code 688285 --refresh-financial
python3 scripts/run_valuation.py --code 688285 --skip-financial-fetch
python3 scripts/run_valuation.py --code 688285 --resume
python3 scripts/run_valuation.py --code 688285 --resume --no-fetch-evidence --no-publish
python3 scripts/run_valuation.py --code 688285 --status
python3 scripts/run_valuation.py --code 688285 --dry-run
```

`run_valuation.py` 只负责执行控制，不参与研究判断或估值计算。其职责固定为：

1. 新运行校验股票代码、mx-data 脚本、财务原始缓存和缓存时效；缓存缺失或过期时直接调用 mx-data 补齐标准财务查询；所有执行校验运行锁。
2. 财务 raw JSON 通过接口状态、证券代码和可解析字段门禁后，通过本地 symlink 路径调用 `scripts/valuate.py --code <code>`，并校验新生成的 briefing；续跑默认保留原阶段零输入，不重新生成 briefing。
3. 调用 `scripts/valuation_pipeline.py` 执行检索、阶段一至五、参数门禁、计算和发布。
4. `--resume` 自动选择该股票最新的失败/可恢复运行包；对已经完成前四阶段但尚未形成研究卡的任务只重跑阶段五，对研究卡契约校验失败且事实证据完整的任务使用结构修复，其余任务按普通断点续跑处理。
5. 同一股票同一时间只允许一个总控任务；异常退出后可回收已失效的 PID 锁。
6. 子流程退出后必须读取 `manifest.json` 复核终态；发布模式下 Dashboard 失败也视为总控失败，不能只凭子进程返回码宣告成功。
7. 将总控命令、恢复决策、阶段零结果、运行包和最终状态写入运行包 `controller_summary.json`。

mx-data 通过 `.env` 的 `HUAXIN_MX_DATA_SCRIPT` 定位，默认回退到 `~/.claude/skills/mx-data/mx_data.py`，凭据只读取 `MX_APIKEY`。新运行执行阶段零时，财务缓存缺失或超过默认 90 天会自动查询：最新年报核心财务与资产负债字段、近三年利润/利润率历史、总股本及估值基础字段。每个查询失败可重试，只有新生成的 `*_raw.json` 能被 `valuate.py` 解析时才算完成。

`--refresh` 同时强制刷新财务、briefing 和模型三检索证据；`--refresh-financial` 只强制刷新财务后继续完整流程；`--skip-financial-fetch` 禁止调用 mx-data，仅允许使用满足时效门禁的本地缓存，与 `--allow-stale-financial` 联用时可显式放行已确认仍有效的旧财报。续跑和 `--skip-stage0` 只使用已归档/当日 briefing，不因工作区财务缓存后来过期而阻断，也不重新调用 mx-data。

`valuation_pipeline.py` 仍保留为内部入口，供定位、修复和开发验证使用：

### 主动触发与运行包

```bash
python3 scripts/valuation_pipeline.py --code 688285
python3 scripts/valuation_pipeline.py --code 688285 --evidence-dir cache/research
python3 scripts/valuation_pipeline.py --code 688285 --refresh-evidence
python3 scripts/valuation_pipeline.py --code 688285 --fetch-only
python3 scripts/valuation_pipeline.py --code 688285 --no-fetch-evidence
python3 scripts/valuation_pipeline.py --code 688285 --dry-run
python3 scripts/valuation_pipeline.py --code 688285 --resume-run cache/valuation_runs/688285_<timestamp> --no-publish
python3 scripts/valuation_pipeline.py --code 688285 --resume-run cache/valuation_runs/688285_<timestamp> --rerun-stage3 --no-fetch-evidence --no-publish
python3 scripts/valuation_pipeline.py --code 688285 --resume-run cache/valuation_runs/688285_<timestamp> --rerun-stage5 --no-publish
python3 scripts/valuation_pipeline.py --code 688285 --resume-run cache/valuation_runs/688285_<timestamp> --repair-research-card cache/valuation_runs/688285_<timestamp>/research_card_raw.json --no-fetch-evidence --no-publish
python3 scripts/valuation_pipeline.py --code 688285 --resume-run cache/valuation_runs/688285_<timestamp> --audit-research-card cache/valuation_runs/688285_<timestamp>/research_card.json --no-fetch-evidence --no-publish
```

`--resume-run` 只允许续跑同一运行包：已有的阶段 JSON 与证据快照直接复用，仅执行缺失阶段；不得重新检索或重调已完成 LLM 节点。
`--rerun-stage3` 用于证据范围或通用映射契约升级：复用阶段一、二和本次专题计划的成功缓存，从阶段三重做至阶段五。与 `--no-fetch-evidence` 联用时，即使缓存超过默认有效期，也只读已成功的原始快照，不发生联网检索。
`--rerun-stage5` 用于阶段五研究或映射契约失败，只重做不合格的五 A/五 B 子阶段，前四阶段不得重跑；删除对应子阶段文件才会强制从该子阶段重新生成。
`--repair-research-card` 只允许修复已归档研究卡的 schema、枚举、单位和显式映射，不得新增或改写研究事实与证据；修复后仍须通过同一套证据门禁、路线参数校验和计算引擎。
`--audit-research-card` 使用已完成阶段的阅读结论做项目覆盖和估值语义审计：检查独立并购/资产/产能/海外/证券化项目是否遗漏，检查增长率百分比单位、PE 锚的底层明细和研究结论与计算参数一致性；它不重新搜索或阅读原文。

原文阅读按批次独立保存到运行包 `batch_readings/`，缓存键同时校验该批 `source_ids`。进程中断后，续跑必须复用 source_ids 完全一致的已完成批次，只调用尚未完成或证据集合已变化的批次。每次 LLM 尝试的 HTTP 状态、请求/响应长度、原始响应和异常写入 `llm_traces/`；不得以静默重试掩盖长响应或供应商错误。

续跑时如检索缓存的响应封装变化导致旧 source_id 不再出现在最新原始快照，已完成 `stage_*_readings.json` 中逐条归档的阅读结论必须作为 `cached_llm_reading` 证据保留原 source_id，供后续阶段复用并展示其运行包来源；它不进入新的原文阅读批次，也不得冒充新的外部检索结果。

每次运行创建 `cache/valuation_runs/<code>_<timestamp>/`，至少保存：

```
manifest.json       # 阶段状态、输入/输出文件和错误原因
briefing.json       # 阶段零输入快照
evidence.json       # 可用证据及其 source_id
research_card.json  # LLM 的结构化研究结论
calc_params.json    # 经脚本验证后送入引擎的参数
calc_results.json   # 引擎原始结果
```

报告、排名和索引只能读取该运行包中的已验证文件；不得从对话上下文、旧报告或未归档网页补数。

### Dashboard 发布

完成的 v2 运行由 `dashboard_valuation.py` 发布为按日归档的轻量摘要 `dashboard/data/<YYYYMM>/valuation_context_<YYMMDD>.js`，并生成 `dashboard/data/valuation/catalog.js`、`dashboard/data/valuation/reports/<run_id>.js` 与 `dashboard/data/valuation/evidence/<run_id>.js`。`dashboard/data/valuation_latest.js` 仅作为轻量兼容目录，不得再内嵌完整研究卡或证据。最新目录对每只股票只保留时间最近的已验证运行，页面调试只重建这些只读数据包，不重新搜索、阅读材料或调用 LLM。

适配器只读取运行包内的 `manifest.json`、`research_card.json`、`calc_params.json`、`calc_results.json` 和 `evidence.json`，不解析 Markdown，也不读取未完成运行补充研究结论。运行整体为 `done` 且研究卡、参数和计算结果通过门禁即可发布；`insufficient_consensus` 是必须展示的研究状态，不能以此为由从目录静默删除。公司名称依次取计算参数、运行清单、研究卡和估值研究索引中的有效名称；纯数字代码、空值和占位符不得作为股票名称展示。进度包对每只股票只发布最新一次运行状态，禁止将旧失败任务、旧中断任务或无开始时间的残留运行重复显示为“分析中”。

Dashboard 主面板中的“投研分析”是最新完成报告索引，显示公司名与代码、报告日期、估值区间、空间和共识状态；点击标的进入独立 `valuation-report.html` 公司研究页。公司页必须完整展示一致预期、PE 构建、三层三情景矩阵、利润支柱、市场分歧、叙事期权、验证节点、事实假设、风险催化剂与证据目录。任何 `insufficient_*` 状态必须原样展示。

公司页首屏必须同时展示行情日期与来源、2026E/2027E 三情景每股估值和基准空间、共识质量、主要估值支柱、首要风险与最近验证节点。双年度估值和空间只能由 `calc_results.json` 与同一 `current_price` 确定性派生；页面不得自行补写研究判断。三层矩阵须支持 2026E/2027E 切换，明确市值、利润和股价单位，缺失值显示为未量化而不是零。

完整公司研究包按用户打开标的时加载，证据包仅在展开证据目录或点击正文 `source_id` 时加载。正文证据引用必须可定位到证据目录中的同一 `source_id`；索引页不得预载完整研究卡、证据摘录或缓存定位。桌面端和移动端都须提供可访问的章节导航，并支持 URL 锚点直接定位章节。

所有 Dashboard 发布器都必须保留其他模块的 `dashboard/data/index.js` 条目；市场、VCP、信号的日常发布不得覆盖 `valuation` 索引。

```bash
python3 scripts/dashboard_valuation.py --date 260720
python3 scripts/dashboard_valuation.py --all
```

### 证据门禁（强制）

1. 每条外部事实都必须引用 `evidence.json` 中的 `source_id`；引用包含来源类型、发布日期（若有）、缓存文件和原文摘录。
2. 搜索返回限流、失败、空数据、超过 6 个月的研报，均不是“无覆盖”的证据，必须记录为 `insufficient_evidence`。
3. 共识阶段只有在至少 3 家有效、未过期机构预测齐备时才能标记 `done`；否则为 `insufficient_consensus`，不得勾选完成。
4. 每个预测假设、可比参数和 Layer 3 项目必须有证据引用。缺少引用、引用不存在或数值不合规时，脚本拒绝调用计算引擎。
5. LLM/API 未配置、调用失败、输出非 JSON 或校验失败时，运行显式失败；不得使用模型自行补写的兜底研究结论。默认读取全局 `DEEPSEEK_MODEL=deepseek-v4-flash`。

### 脚本化检索清单

编排器先调用妙想搜索（仅使用本地 `MX_APIKEY`）并缓存原始响应到 `cache/research/`。第一轮固定清单覆盖财报/主营业务、研报盈利预测（2026E+2027E）、公告与订单、管理层动作、可比公司估值和当前股价/市值；阶段一随后必须基于业务、资产平台、并购、REITs、产能和海外判断生成第二轮专题清单。财务基础指标仍由阶段零 briefing 提供，不重复由搜索猜测。

- 默认成功缓存有效期为 24 小时；`--refresh-evidence` 强制重拉，`--no-fetch-evidence` 只复用已有缓存。
- 单项搜索限流、空结果、网络失败会写入运行包 `manifest.json`，并在指数退避后重试；该项不能成为有效证据。
- 搜索结果原始 JSON 与查询元数据均保留。LLM 只能读取通过证据筛选的结果，不能把失败、空结果解释成“没有覆盖”。

### LLM 节点边界

LLM 由编排器按阶段逐节点调用，输入为 briefing、已完成阶段结论和当前专题的完整结果级证据，输出仅为 JSON。阶段一负责业务拆分与专题检索计划；阶段二负责共识/可比/分歧；阶段三负责项目级预期差；阶段四负责验证节点；阶段五只负责将已完成研究映射到 `research_card` 与 `calc_params`。不得输出 Markdown、不得访问未提供的数据、不得声称搜索过未在证据包中的来源。

脚本负责：JSON 解析、schema/枚举/数值校验、证据 ID 校验、运行状态、计算、报告模板渲染和原始产物归档。报告中的估值数字必须来自 `calc_results.json`，报告中的事实必须来自 `research_card.json` 并展示证据 ID。

通用计算映射不得依赖公司名、行业名或项目关键词。阶段五必须显式给出 `classification`、`profit_timing`、`accounting_treatment`、`calculation_mapping.target`、`pillar_id`、路线与路线参数；脚本仅按这些标准字段映射。路线参数契约为：A 使用双年 EBITDA、可比 EV/EBITDA 与净负债；A1/B/C 使用双年净利及上下沿；D/F 使用净资产、ROE 与 PB 可比；E 使用双年营收、毛利率与 PS 可比。Layer 2、Type B 管道和 Layer 3 只能显式挂接到已存在的 `pillar_id`。
同一支柱可挂接多条 Layer 2，脚本按悲观/基准/乐观分别累加而非后项覆盖；Type B 管道统一进入所属支柱的 Layer 2，不受主营支柱采用 A/A1/B/C/D/E/F 哪条路线限制。被 `exclude` 的支柱不得承接任何非排除的 Layer 2、Type B 或 Layer 3 项目。

> 下文阶段一至五保留为研究内容规范；其执行顺序和完成判定以本节 V2 协议为准。

---

## 阶段零：数据准备（脚本执行）

```bash
python3 scripts/valuate.py                    # 全部待处理标的
python3 scripts/valuate.py --code 300442      # 单只
python3 scripts/valuate.py --no-cache         # 强制刷新
```

脚本自动完成：财务数据拉取 → 指标计算 → 决策树五信号判定 → 漏斗信号 → 简报册输出 `cache/briefing/<code>_<YYMMDD>.json`。LLM 直接引用简报册数据，不手工拉数。

**数据口径**：valuate.py 取 mx-data 返回的最新可用数据，与模型一 process_pool.py 的 LATEST 口径一致。模型一和三使用同一报告期，避免出现"Q1 盈利但年报亏损"被过滤后又重新估值的不一致。

**价格口径**：财务指标和 `valuation_snapshot.market_cap` 属于财务报告期快照，不得用于推导报告生成时的当前价。估值编排器必须通过项目统一日线数据服务读取最新有效交易日的收盘价，写入 `briefing.market_quote` 和 `calc_params.meta.current_price/price_date/price_source`。行情必须覆盖 `expected_trade_date()` 且价格大于零；缺失、过期或接口失败时运行显式失败，不得回退为“报告期总市值 ÷ 总股本”。仅更新行情价格时允许复用现有研究卡和全部 LLM 结论，只重建计算结果与 Dashboard 数据包。

### 决策树五信号速查

| 信号 | 判断逻辑 |
|------|---------|
| Q1: 稳定利润 | 归母净利>0 且 OCF/NP>0.5 |
| Q1b: 高增长营收 | 营收>0 且 增速>30%（仅 Q1=false 时触发） |
| Q2: 资产轻重 | 固产/总资产 >40%→重, <20%→轻, 其余→中 |
| Q3: 利润稳定 | 近3年毛利率波动 <5pct→稳定, ≥5pct→波动 |
| Q_IRREG: 非经常性 | 非经常性占比 >30%→触发 |

### 漏斗信号速查

| 信号 | 高优先级阈值 | 中优先级阈值 |
|------|-----------|-----------|
| 产能扩张 | 在建/总资产 >10% | 5-10% |
| 技术突破 | 研发费率 >15% 且 营收增速<20% | 研发费率 >8% |
| 海外拓展 | 海外占比 >30% 且 海外增速>国内 | — |
| 非经常性 | 非经常性占比 >30% | — |

---

## 阶段一：业务拆解 + 自算预测（不联网）

**1.1 业务结构识别**（强制记录）：从简报册营收构成出发，列出所有业务线的收入/占比/增速/毛利率，判断是否需要拆分。**判断结论必须写入报告**——拆分的说明各自估值方法，合并的说明"方法相同+增速差<20%+毛利率差<15pct→合并"。

**1.2 驱动因素拆解**：每条占比>10%的业务线，识别核心驱动（如机柜规模×上架率、出货量×均价、产能×利用率）。

**1.3 决策树分派**：脚本已输出 Q1-Q_IRREG。LLM 按树走到叶子节点，确定路线 A~F。**多条业务线特征不同时必须分别走树，加总得整体估值。**

```
Q1=true:
  Q2=重 → [Q_IRREG?] → A1(分部) / A(EV/EBITDA)
  Q2=轻 → B(PE+P/FCF)
  Q2=中 → [Q3?] → C(PE+DCF) / D(PB+周期PE)
Q1=false:
  Q1b=true → E(PS+PEG)
  Q1b=false → F(PB重估)
```

> 路线详情（公式/非经常性ABC分类/管道清单要求）见参考手册。

**1.4 盈利质量**：简报册 5 项指标逐项标注健康/关注/警示。

**1.5 利润率趋势**：标注方向（上升/稳定/波动/下降），近一年变化>3pct 时说明原因。

**1.6 自算 2026E 预测**（强制，按业务线）：

```
2025 扣非基准:    XX 亿
+ 增量来源1:      +XX 亿 (依据: ...)
+ 增量来源2:      +XX 亿 (依据: ...)
- 减项:           -XX 亿 (依据: ...)
─────────────────
2026E 核心利润:   XX 亿
```

若利润率偏离历史中枢 ±5pct，必须做正常化分析（偏离原因/可逆性/预测取值）。增速>100% 禁止线性外推。

---

## 阶段二：共识提取 + 参数准备 + 引擎计算

### 2.1 共识提取（Layer 1 — 强制，双年度）

mx-search 强制拉取研报（每只至少一次，不能仅依赖简报册。简报册 consensus 仅 `quality=verified` 时参考）。

```bash
python3 /path/to/mx_search.py --output-dir cache/research "<公司名称> 研报 盈利预测 2026 2027"
```

提取 ≥3 家机构的结构化数据，**同时拉取 2026E 和 2027E**：

| 机构 | 报告日期 | 2026E营收 | 2026E净利 | 2027E营收 | 2027E净利 | 目标价 | PE | 评级 | 关键假设 |
|------|---------|----------|----------|---------|----------|--------|-----|------|---------|

汇总表分两年输出（均值/中位数/最高/最低/机构数）。**6个月以上旧研报剔除。**

**2.1b 业务线拆分（强制）**：多条业务线且估值方法不同、增速差>20%、或毛利率差>15pct 时，逐条提取共识并独立估值。

### 2.2 分歧量化（Layer 2 — 强制）

从研报中提取结构化共识数据后，分歧度由引擎自动计算：`(最高-最低)/均值×100%`。

| 分歧度 | 定价状态 | 行动 |
|--------|---------|------|
| <20% | 充分定价 | 共识=基准 |
| 20-50% | 大部分定价 | 分析来源 |
| >50% | 部分定价 | 强制根因分析 |

**估值逻辑分歧**（分歧度>30% 或机构对同一业务线给差>30%PE 时强制）：

```
乐观叙事 (XX证券, PE XXx): [逻辑]
悲观叙事 (XX证券, PE XXx): [逻辑]
→ 我的判断: [偏向+依据]
→ Layer 2 调整: [取共识上沿/下沿/均值]
```

**你的立场**：自算 vs 共识区间。偏乐观→取共识上沿（需证据），偏悲观→取下沿，一致→不调整。

### 2.3 交叉验证

自算 vs 共识对比表。偏差>20% → 需在阶段三中验证假设，无证据→回归共识。

### 2.4 可比公司锚定

≥2家可比公司，提取 PE/PB/PS + 增速 + 毛利率，对比差异。

### 2.5 构建引擎参数 → 调用估值计算引擎

完成以上判断后，将结果组织为结构化参数 JSON，保存到 `cache/calc_params/<code>_<YYMMDD>.json`（完整 schema 见附录 A.2），然后运行引擎：

```bash
python3 scripts/calc_valuation.py cache/calc_params/<code>_<YYMMDD>.json
```

**你需要提供的参数**（判断结果）：

| 参数 | 来源 | 示例 |
|------|------|------|
| `growth_quality` | 增长质量判断 | "structural" / "cyclical" / "mature" |
| `market_position` | 市场地位 | "leader" / "mid" / "follower" |
| `comparable_pe_median/lower/upper` | 可比公司表 | 25.0, 20.0, 30.0 |
| `company_growth_rate` / `comp_growth_rate` | 阶段一自算 + 可比公司 | 47.0, 12.0 |
| `qualitative_adjustments` | 定性调整项 ±5-10%/项 | [{"item":"龙头","adjustment":0.10}] |
| `pillars[]` | 每业务线：路线、共识净利、L2调整、L3预期差、管道清单 | 见附录 A.2 |
| `pe_override`（可选） | 手动指定 PE（当公式结果与判断差异大时） | 35.0 |

**引擎自动完成**（不需要你算）：
- PE 三步走：增速调整 → PEG 上限 clamp → 定性调整 → 最终 PE
- 悲观情景参数：按增长质量自动压 PE 或压利润
- 各路线估值：按路线分派公式计算三情景市值/每股
- Type B 管道：`Σ(项目利润 × PE × 概率)` 逐项加总
- 分歧度计算
- 2027E 参数迁移：PE 不下调、概率上调、折扣收窄
- 三层矩阵汇总：支柱 × Layer 矩阵 + 按层合并
- 反向检查触发
- `reports/indexes/valuation_ranking.csv` 自动更新

引擎输出 `cache/calc_results/<code>_<YYMMDD>.json`，含 PE 计算细节、每支柱估值、三层矩阵、报告数字片段。**直接引用引擎输出的数字写入报告，不手工重算。**

> **pe_override 使用场景**：当公式产出的 PE 与你的判断差距大时（例如超高增速导致公式 PE 偏高），使用 `pe_override` 手工指定。引擎仍会自动计算悲观 PE 和乐观 PE。

### 2.6 2027E 估值

引擎自动从 2026E 参数迁移到 2027E。核心规则：
- PE 不下调（增速放缓是基数效应，不是生意变差）
- 悲观 PE 折扣沿用（仅增长质量降档时调整）
- Layer 3 概率上调（时间推进，折扣收窄）
- Type B 管道概率上调（扩募验证后确定性提高）

**不需要重做的**：业务拆分、分歧根因分析、可比公司选择、催化剂日历。

---

## 阶段三：预期差发现（Layer 3 — 未定价）

**预期差 ≠ 分歧。** 分析师在讨论的=Layer 2，分析师没覆盖的=Layer 3。

### 3.0 最低执行要求（强制 — 每只必须完成）

以下两项**每只标的必须执行**，不论漏斗信号强弱。即使结果为空，也要在报告中记录"已搜索，未发现显著预期差"。

**路径 B（时间差）— 强制**：`"<公司名>" 公告 2026`。搜研报最新日期之后的新公告。
**路径 D（管理层）— 强制**：`"<公司名>" 增持 回购 股权激励`。

路径 A（漏斗）和路径 C（产业链）仅在漏斗有高/中信号时强制执行。

### 3.1 搜索执行

关键词参考：
- 路径 B: `"<公司名>" 公告 2026`、`"<公司名>" 订单 中标 签约`
- 路径 D: `"<公司名>" 增持 回购 股权激励`
- 路径 A/C（漏斗方向）: 产能`产能 扩产 在建`、技术`研发 产品 验证`、海外`海外 出海 工厂`
- 路径 C（产业链）: `"<下游客户>" 2026 计划 指引`

### 3.2 预期差清单

逐条记录：名称、业务实质、当前状态、量化利润潜力、市场定价状态（未/部分/过度）、实现概率（高70%+/中40-70%/低<40%），概率必须有具体理由。

### 3.3 独立估值

估值增量 = 潜在净利 × 合理倍数 × 实现概率。将预期差量化为 `layer3_items` 填入引擎参数（见附录 A.2），引擎自动汇总到三层矩阵中。

---

## 阶段四：催化剂识别

每条预期差绑定 ≥1 个催化事件。仅记录近期+中期。输出最低格式：

| 时间 | 事件 | 关联支柱 | 重要度 | 可检查 |
|------|------|---------|--------|--------|

主叙事匹配：<维度> / <位置> / 核心·间接·边缘。

---

## 阶段五：输出报告 + 检查

### 5.0 写入前检查清单（强制 — 6 项）

```
□ 1.1 业务线拆分判断完成
□ 1.6 利润测算完成（逐项目拆解，≥3行增量来源，每行有依据）
□ 2.1 共识提取完成（≥3家机构, 2026E+2027E）
□ 2.4 可比公司锚定完成（≥2家, 含PE/增速/毛利率对比表）
□ 3.0 路径B搜索已执行 + 路径D搜索已执行
□ 4   催化剂日历已输出（≥2条近期+中期事件）
```

以上 6 项全部打勾才能写入报告。以下项目由引擎保证，不需手工检查：PE 计算、悲观参数应用、矩阵汇总、2027E 迁移、CSV 格式、反向检查触发。

### 5.1 报告输出

报告模板见参考手册。核心要求：
- 先给答案（估值区间），再拆计算逻辑，再列依据
- **报告结构顺序**：估值总览（含双年对照 + 引擎输出的三层汇总表）→ 支柱一（2026E+2027E）→ 支柱二 → 支柱三（Layer 3 预期差, 必须有内容, 即使结论为"无显著预期差"）→ 催化剂日历 → 风险提示 → 研究更新记录
- **数字来源**：PE 计算过程、三层矩阵、估值总览表 — 直接引用 `calc_results` JSON 中的 `details` 和 `report_fragments` 字段
- 支柱一必须含：计算逻辑（引擎 details）、核心假设表、利润测算（你写的）、可比公司表（你选的）。**计算逻辑末尾必须输出市值→股价转换**
- 支柱二/三含：业务实质、计算逻辑、假设依据、管道清单（Type B强制，逐项列出每项目的规模/概率/估值，引擎 breakdown 可直接引用）
- 所有数据标注来源（可信度：妙想金融/公司公告>券商研报>行业新闻）

### 5.2 反向检查

引擎自动执行反向检查（阈值见附录 A.1 常量表）。检查 `calc_results` JSON 的 `reverse_check` 字段：
- `triggered=false` → 报告中写"🔍 反向检查：折扣率 X.XX，未触发。"
- `triggered=true` → 阅读 `triggers` 和 `checks`，逐一回应检查项，将结论写入报告

引擎不替代你的判断——它只告诉你哪些条件触发了，你需要解释为什么以及是否需要调整。

### 5.3 进度管理

- `reports/indexes/valuation_index.csv`：每标的一行，记录各阶段状态（done/failed/pending）和日期。每完成一个阶段立即写回。
- `reports/indexes/valuation_ranking.csv`：**引擎自动更新**（运行 `calc_valuation.py` 时自动追加/替换行，按安全边际折扣率升序排列）。不需要手工写 CSV。

---

> 待优化项见 `TODO.md`。引擎常量 / 参数 schema / 函数签名 / 重建脚本所需规格见参考手册 `03-valuation-ref.md`。版本历史由 Git 追溯，复盘记录见 `dev_logs/`。
