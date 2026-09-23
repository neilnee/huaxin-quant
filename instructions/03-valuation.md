# 模型三：深度估值模型（自执行指令）

- **版本管理**: 由 Git 分支与提交历史管理，文件名不再携带版本号
- **最近更新**: 2026-09-08（文档对齐，策略不变）
- **核心哲学**: 脚本编排、校验、存档和渲染；LLM 只完成小范围、结构化的研究判断。
- **触发方式**: 用户主动按单只标的触发，不消费 Bloom，也不自动给出交易动作。
- **输出**: `reports/valuation/<code>_<name>.md` + `reports/indexes/valuation_index.csv` + `reports/indexes/valuation_ranking.csv` + 当次可审计运行包 + Dashboard 数据包。
- **配套脚本**: `scripts/run_valuation.py`（端到端总控）+ `scripts/valuation_pipeline.py`（v3 五阶段编排器）+ `scripts/dashboard_valuation.py`（Dashboard 适配器）+ `scripts/valuate.py`（阶段零）+ `scripts/calc_valuation.py`（计算引擎）| 参考手册: `03-valuation-ref.md`

---

## 总览

```
阶段零（财务与行情）→ 阶段一（业务地图）→ 阶段二（机构利润与目标估值组合）
→ 阶段三（共识后新增证据）→ 阶段四（验证节点）→ 阶段五（研究卡、映射、计算与发布）
```

**分工原则**：脚本编排、归档、校验、计算和发布；LLM 负责有来源的业务、机构预测逻辑、分歧及验证节点。不得自行补造经营利润或估值倍数。

## V4 简化共识协议（当前有效）

V4 是当前研究和页面协议。旧分支柱估值、可比 PE、Layer 2/3 加值及旧报告模板已移至 [历史兼容参考](03-valuation-legacy-ref.md)；旧字段只用于兼容运行包，不得主导新分析或新页面。

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

## 执行与审计约束

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

只从已完成且通过校验的运行包生成按日轻量摘要、最新公司目录与公司研究包。当前不发布独立证据包；source_ids 与 evidence.json 保留在运行包内部用于审计。公司页结构以本卡 V4 为准，不恢复旧三层矩阵、PE 构建和证据目录。

适配器读取运行包 manifest、research_card、calc_params、calc_results 和 evidence，不解析 Markdown，不从未完成研究中补结论。公司名不得使用纯代码或占位符；历史运行包已有 insufficient_* 状态需原样展示，不得补造目标估值。

页面重建不触发新搜索或 LLM，且须保留 dashboard/data/index.js 中其他模块日期索引：

```bash
python3 scripts/dashboard_valuation.py --date 260720
python3 scripts/dashboard_valuation.py --all
```

### 证据门禁（强制）

1. 每条外部事实都必须引用 `evidence.json` 中的 `source_id`；引用包含来源类型、发布日期（若有）、缓存文件和原文摘录。
2. 搜索返回限流、失败、空数据、超过 6 个月的研报，均不是“无覆盖”的证据，必须记录为 `insufficient_evidence`。
3. 共识阶段只有在至少 3 家有效、未过期机构预测齐备时才能标记 `done`；否则为 `insufficient_consensus`，不得勾选完成。
4. 每条机构预测、目标估值、研究判断及新增事项必须有证据引用。缺少引用、引用不存在或数值不合规时，脚本拒绝调用计算引擎。
5. LLM/API 未配置、调用失败、输出非 JSON 或校验失败时，运行显式失败；不得使用模型自行补写的兜底研究结论。默认读取全局 `DEEPSEEK_MODEL=deepseek-flash`，对应 DeepSeek-V4.1-Flash。

### 脚本化检索清单

编排器先调用妙想搜索（仅使用本地 `MX_APIKEY`）并缓存原始响应到 `cache/research/`。第一轮固定清单覆盖财报/主营业务、研报盈利预测（2026E+2027E）、公告与订单、管理层动作、可比公司估值和当前股价/市值；阶段一随后必须基于业务、资产平台、并购、REITs、产能和海外判断生成第二轮专题清单。财务基础指标仍由阶段零 briefing 提供，不重复由搜索猜测。

- 默认成功缓存有效期为 24 小时；`--refresh-evidence` 强制重拉，`--no-fetch-evidence` 只复用已有缓存。
- 单项搜索限流、空结果、网络失败会写入运行包 `manifest.json`，并在指数退避后重试；该项不能成为有效证据。
- 搜索结果原始 JSON 与查询元数据均保留。LLM 只能读取通过证据筛选的结果，不能把失败、空结果解释成“没有覆盖”。

### LLM 节点边界

LLM 由编排器按阶段逐节点调用，输入为 briefing、已完成阶段结论和当前专题的完整结果级证据，输出仅为 JSON。阶段一负责业务拆分与专题检索计划；阶段二负责机构共识/利润逻辑/目标估值组合；阶段三负责项目级预期差；阶段四负责验证节点；阶段五只负责将已完成研究映射到 `research_card` 与 `calc_params`。不得输出 Markdown、不得访问未提供的数据、不得声称搜索过未在证据包中的来源。

脚本负责：JSON 解析、schema/枚举/数值校验、证据 ID 校验、运行状态、计算、报告模板渲染和原始产物归档。报告中的估值数字必须来自 `calc_results.json`，报告中的事实必须来自 `research_card.json`，证据 ID 在运行包中保留；公司页不展示内部证据 ID。

通用映射不得依赖公司名、行业名或项目关键词。阶段五只合并已经通过证据和数值门禁的研究结果；公司估值使用阶段二完整机构组合，不能恢复旧可比 PE 回退或重复计入分歧。阶段一业务地图与阶段五画像一一对应，但不因此产生新的利润支柱。

## 分阶段执行与验收

| 阶段 | 当前职责 | 关键产物 |
|---|---|---|
| 零 | 标准财务输入、briefing 与行情日期 | briefing.json |
| 一 | 完整业务地图、证据阅读及专题计划 | stage_1_business.json、search_plan.json |
| 二 A/B | 逐机构预测与目标估值组合；公司级双年三情景 | stage_2_institutions.json、stage_2_model_inputs.json、stage_2_consensus.json、stage_2_baseline.json |
| 三 | 执行本卡新增证据时限门禁；没有合格新增证据时保留审计并跳过研究 LLM | stage_3_expectations.json |
| 四 | 汇总验证节点、失效条件与风险 | stage_4_catalysts.json |
| 五 | 有界研究/映射节点、契约验证、确定性计算与发布 | research_card.json、calc_params.json、calc_results.json |

各阅读批次与阶段五子节点独立保存、按已有恢复契约复用，不得将整个流程压缩为一次 LLM 汇总。金额、股本、价格单位遵循本卡运行包契约；研究卡 ready/兼容状态由脚本标准化和校验。

完成条件为：同一运行包的证据与机构样本通过门禁，三情景有序、行情口径一致，研究卡及计算结果合规，manifest 与总控摘要成功；正式发布还须报告和 Dashboard 成功。样本不足、来源失败或字段缺失不得用旧公式补齐。

旧 A–F 路线、决策树、利润桥、PE 三步走及旧写入检查仅在审计历史运行包时阅读 [历史协议](03-valuation-legacy-ref.md) 与 [引擎参考](03-valuation-ref.md)。它们不是新 V4 报告的追加验收条件。未来的共识覆盖、预测修订与实际业绩验证见 [路线图 R10](../docs/IMPROVEMENT_ROADMAP.md)。
