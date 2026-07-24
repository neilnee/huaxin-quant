const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const num = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const money = (value) => Number.isFinite(Number(value)) ? `${num(value, 2)} 元` : "—";
const pct = (value) => Number.isFinite(Number(value)) ? `${num(value, 1)}%` : "—";
const text = (item, keys) => keys.map((key) => item?.[key]).find((value) => value !== undefined && value !== null && value !== "") ?? "—";
const asList = (value) => {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === "") return [];
  if (typeof value === "string") return value.split(/[,，;；]/).map((item) => item.trim()).filter(Boolean);
  return [value];
};
const listText = (value) => asList(value).map((item) => typeof item === "object"
  ? text(item, ["event", "name", "node_id", "statement", "description"])
  : item).join("、");
const query = new URLSearchParams(location.search);
const code = query.get("code");
const catalog = window.QUANT_DASHBOARD_VALUATION_CATALOG || window.QUANT_DASHBOARD_VALUATION_LATEST;
let catalogEntry;
let report;
let selectedYear = "2026e";
let selectedPillarId;
let evidenceLoaded = false;

function showError(message) {
  $("report-error").textContent = message;
  $("report-error").classList.remove("hidden");
}

function loadScript(path) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = path;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error(`数据包加载失败：${path}`));
    document.head.appendChild(script);
  });
}

function dateLabel(stamp) {
  return stamp?.length === 6 ? `20${stamp.slice(0, 2)}-${stamp.slice(2, 4)}-${stamp.slice(4, 6)}` : "—";
}

function median(values) {
  const sorted = values.filter(Number.isFinite).sort((left, right) => left - right);
  if (!sorted.length) return null;
  return sorted.length % 2 ? sorted[(sorted.length - 1) / 2] : (sorted[sorted.length / 2 - 1] + sorted[sorted.length / 2]) / 2;
}

function sourceButtons(item) {
  const ids = asList(item?.source_ids);
  if (!ids.length) return "";
  return `<span class="source-ids">证据：${ids.map((id) => `<button type="button" class="source-link" data-source-id="${esc(id)}">${esc(id)}</button>`).join(" · ")}</span>`;
}

function scenarioFor(year) {
  const values = report.valuation?.years?.[year];
  if (values) return values;
  if (year === "2026e") return report.valuation || {};
  return { base: report.valuation?.base_2027 };
}

function scenarios(values) {
  return `<div class="scenario-row"><span>悲观<b>${num(values?.pessimistic)}</b></span><span>基准<b>${num(values?.base)}</b></span><span>乐观<b>${num(values?.optimistic)}</b></span></div>`;
}

function renderValuationYear() {
  const valuation = report.valuation || {};
  const selected = scenarioFor(selectedYear);
  const yearLabel = selectedYear === "2026e" ? "2026E" : "2027E";
  const current = Number(valuation.current_price);
  const cards = [
    ["当前价格", valuation.current_price, valuation.price_date ? `${valuation.price_date} · ${valuation.price_source || "行情数据"}` : "行情日期缺失", "current"],
    [`${yearLabel} 悲观`, selected.pessimistic, current ? `${pct((selected.pessimistic / current - 1) * 100)} 空间` : "—", ""],
    [`${yearLabel} 基准`, selected.base, pct(selected.base_upside_pct), "base"],
    [`${yearLabel} 乐观`, selected.optimistic, current ? `${pct((selected.optimistic / current - 1) * 100)} 空间` : "—", ""],
  ];
  $("valuation-cards").innerHTML = cards.map(([label, value, note, klass]) => `<article class="valuation-card ${klass}"><span>${label}</span><b>${money(value)}</b><small class="${Number(String(note).replace("%", "")) >= 0 ? "positive" : "negative"}">${note}</small></article>`).join("");

  const points = [["悲观", selected.pessimistic], ["基准", selected.base], ["乐观", selected.optimistic], ["当前", valuation.current_price]].filter(([, value]) => Number.isFinite(Number(value)));
  const values = points.map(([, value]) => Number(value));
  const min = Math.min(...values), max = Math.max(...values);
  $("valuation-track").innerHTML = points.length ? `<div class="track-line">${points.map(([label, value]) => {
    const position = max === min ? 50 : 5 + ((Number(value) - min) / (max - min) * 90);
    return `<span class="track-dot ${label === "当前" ? "current" : ""}" style="left:${position}%"><i>${label} ${num(value)}</i></span>`;
  }).join("")}</div>` : "";

  renderBaselineModelYear();

  document.querySelectorAll("[data-year]").forEach((button) => {
    const active = button.dataset.year === selectedYear;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
}

function renderYearComparison() {
  const baseline = report.stage2_baseline || {};
  const y26 = baseline.totals?.["2026e"] || {}, y27 = baseline.totals?.["2027e"] || {};
  const base26 = Number(y26.base), base27 = Number(y27.base);
  const delta = Number.isFinite(base26) && Number.isFinite(base27) ? base27 - base26 : null;
  $("year-comparison").innerHTML = [["2026E 悲观", y26.pessimistic], ["2026E 基准", y26.base], ["2026E 乐观", y26.optimistic], ["2027E 悲观", y27.pessimistic], ["2027E 基准", y27.base], ["双年基准变化", delta]].map(([label, value]) => `<div><span>${label}</span><b class="${Number(value) < 0 ? "negative" : ""}">${Number.isFinite(value) && label.includes("变化") && value > 0 ? "+" : ""}${num(value)} 亿</b></div>`).join("");
}

function renderBaselineModelYear() {
  const baseline = report.stage2_baseline || {};
  const yearLabel = selectedYear === "2026e" ? "2026E" : "2027E";
  const pillars = baseline.pillars || [];
  const scenariosList = [["pessimistic", "悲观"], ["base", "基准"], ["optimistic", "乐观"]];
  const profitControl = baseline.operating_profit_control?.[selectedYear]?.base;
  $("baseline-status").textContent = baseline.note || (baseline.status === "ready" ? `利润总量已对账${profitControl == null ? "" : ` · ${yearLabel}基准 ${num(profitControl)} 亿`}` : "兼容旧运行推导");
  $("matrix-unit").textContent = `${yearLabel} · 金额单位亿元，倍数单位x，概率直接参与独立事项估值`;
  $("matrix-table").innerHTML = `<thead><tr><th rowspan="2">业务支柱 / 方法</th>${scenariosList.map(([, label]) => `<th colspan="4">${label}</th>`).join("")}</tr><tr>${scenariosList.map(() => "<th>利润/收益</th><th>倍数</th><th>概率</th><th>估值</th>").join("")}</tr></thead><tbody>${pillars.map((pillar) => { const year = pillar.years?.[selectedYear] || {}; const type = pillar.pillar_type === "independent_event" ? "独立事项" : "经营业务"; const basis = pillar.profit_basis === "company_consensus" ? "公司共识利润" : (pillar.profit_basis === "direct_segment_forecast" ? "直接分部预测" : "独立事项"); return `<tr class="baseline-pillar-row" data-pillar-id="${esc(pillar.pillar_id)}"><td><b>${esc(pillar.name)}</b><small>${type} · ${basis} · ${esc(pillar.valuation_method)}</small></td>${scenariosList.map(([key]) => `<td>${num(year[key]?.profit)}</td><td>${num(year[key]?.multiple, 1)}x</td><td>${pct(Number(year[key]?.probability ?? 1) * 100)}</td><td><b>${num(year[key]?.valuation)}</b></td>`).join("")}</tr>`; }).join("") || "<tr><td colspan='13'>暂无机构基准估值模型</td></tr>"}<tr class="total-row"><td><b>估值模型合计</b></td>${scenariosList.map(([key]) => `<td colspan="3"></td><td><b>${num(baseline.totals?.[selectedYear]?.[key])}</b></td>`).join("")}</tr></tbody>`;
  $("matrix-table").querySelectorAll("[data-pillar-id]").forEach((row) => row.addEventListener("click", () => { selectedPillarId = row.dataset.pillarId; renderAssumptionLedger(); location.hash = "pillars"; }));
}

function renderAssumptionLedger() {
  const baseline = report.stage2_baseline || {};
  const pillars = baseline.pillars || [];
  if (!pillars.some((pillar) => pillar.pillar_id === selectedPillarId)) selectedPillarId = pillars[0]?.pillar_id;
  const pillar = pillars.find((item) => item.pillar_id === selectedPillarId);
  $("pillar-tabs").innerHTML = pillars.map((item) => `<button type="button" role="tab" data-ledger-pillar="${esc(item.pillar_id)}" class="${item.pillar_id === selectedPillarId ? "active" : ""}" aria-selected="${item.pillar_id === selectedPillarId}">${esc(item.name)}</button>`).join("");
  $("pillar-tabs").querySelectorAll("[data-ledger-pillar]").forEach((button) => button.addEventListener("click", () => { selectedPillarId = button.dataset.ledgerPillar; renderAssumptionLedger(); }));
  if (!pillar) { $("pillar-method").innerHTML = "<p>暂无支柱模型。</p>"; $("assumption-list").innerHTML = ""; return; }
  const pricingStage = { incremental_evidence: "增量证据", final_research: "最终PE研究", institution_baseline: "机构基准" }[pillar.pricing_stage] || "机构基准";
  $("pillar-method").innerHTML = `<div><span>支柱类型</span><b>${pillar.pillar_type === "independent_event" ? "独立事项" : "经营业务"}</b></div><div><span>估值方法</span><b>${esc(pillar.valuation_method)}</b></div><div><span>利润/收益口径</span><b>${esc(pillar.profit_metric)}</b></div><div><span>定价阶段</span><b>${pricingStage}</b></div><p>${esc(pillar.estimation_method || "")}${sourceButtons(pillar)}</p>`;
  const assumptions = (baseline.assumption_ledger || []).filter((item) => item.pillar_id === pillar.pillar_id);
  const channelLabels = { profit: "利润", multiple: "倍数", standalone: "独立项目", none: "未进入" };
  const quantLabels = { quantitative: "已量化", qualitative: "定性依据", missing_input: "待补输入" };
  $("assumption-list").innerHTML = assumptions.map((item) => {
    const priced = item.included_in_model === true || item.included_in_baseline === true;
    const stageLabel = item.pricing_stage === "incremental_evidence" ? "增量事项" : "机构基准";
    const value = item.baseline_value == null
      ? (item.quantification_status === "qualitative" ? "定性条件" : "待补输入")
      : `${esc(item.baseline_value)} ${esc(item.unit)}`;
    return `<article class="${priced ? "is-priced" : "is-unpriced"}"><div><span>${esc(item.year)}</span><b>${esc(item.metric)}</b></div><div class="pricing-state"><span class="pricing-badge">${priced ? `已定价 · ${stageLabel}` : "未定价"}</span><small>${esc(channelLabels[item.pricing_channel] || "状态待核对")} · ${esc(quantLabels[item.quantification_status] || "量化状态待核对")}</small></div><p>${esc(item.condition)}</p><div class="pricing-reason"><span>定价判断</span><b>${esc(item.pricing_reason || "待补充判断依据")}</b></div><div class="assumption-value"><span>基准值</span><b>${value}</b></div><small>信息截止：${esc(item.information_cutoff || "—")}</small>${sourceButtons(item)}</article>`;
  }).join("") || "<p class='section-note'>该支柱暂无结构化假设；需要在新阶段二运行中补齐。</p>";
}

function renderStatic() {
  const valuation = report.valuation || {}, thesis = report.investment_thesis || {}, stats = report.research_stats || {}, pe = report.pe_2026e || {}, reverse = report.status?.reverse_check || {};
  const consensusStatus = report.status?.consensus || {};
  document.title = `${report.name}（${report.code}）· 深度估值报告`;
  $("company-name").textContent = report.name;
  $("company-code").textContent = report.code;
  $("report-meta").textContent = `研究日期 ${dateLabel(report.analysis_date)}`;
  $("market-trade").textContent = thesis.market_trade || "—";
  $("company-stage").textContent = `公司阶段：${thesis.company_stage || "—"}`;
  $("consensus-status").textContent = consensusStatus.status === "done" ? `共识合格 · ${consensusStatus.valid_institutions || stats.consensus_count || 0} 家` : (consensusStatus.status || "共识状态缺失");
  $("quote-status").textContent = valuation.price_date ? `行情：${valuation.price_date} · ${valuation.price_source || "未知来源"}` : "行情日期缺失";
  $("research-stats").innerHTML = [[stats.evidence_count, "份证据"], [stats.consensus_count, "家机构"], [stats.pillar_count, "个支柱"], [stats.verification_count, "个验证点"]].map(([value, label]) => `<div><b>${num(value, 0)}</b><span>${label}</span></div>`).join("");
  $("reverse-check").textContent = reverse.triggered ? `反向检查已触发 · ${reverse.discount_rate_desc || ""}` : "反向检查未触发";
  $("core-judgement").textContent = thesis.core_judgement || "—";

  const decision = report.decision_summary || {};
  const risk = text(decision.primary_risk, ["statement", "description", "risk"]);
  const node = decision.next_verification || {};
  $("decision-cards").innerHTML = `<article><span>主要估值支柱</span><b>${esc(decision.primary_pillar?.name || "—")}</b><p>${decision.primary_pillar?.base_value == null ? "未单独量化" : `${num(decision.primary_pillar.base_value)} 亿元市值`}</p></article><article><span>首要风险</span><b>${esc(risk)}</b></article><article><span>最近验证节点</span><b>${esc(node.event || "—")}</b><p>${esc(node.timeframe || "时间待定")}</p></article>`;

  const consensus = report.consensus || [];
  const np26 = consensus.map((item) => Number(text(item, ["np_2026e", "net_profit_2026e"]))).filter(Number.isFinite);
  const np27 = consensus.map((item) => Number(text(item, ["np_2027e", "net_profit_2027e"]))).filter(Number.isFinite);
  $("consensus-summary").innerHTML = [["机构数量", consensus.length], ["2026E 中位利润", median(np26) == null ? "—" : `${num(median(np26))} 亿`], ["2026E 预测区间", np26.length ? `${num(Math.min(...np26))}–${num(Math.max(...np26))} 亿` : "—"], ["2027E 中位利润", median(np27) == null ? "—" : `${num(median(np27))} 亿`]].map(([label, value]) => `<div><span>${label}</span><b>${value}</b></div>`).join("");
  $("consensus-table").innerHTML = `<thead><tr><th>机构</th><th>报告日期</th><th>2026E 净利</th><th>2027E 净利</th><th>利润口径</th><th>2026E PE</th><th>PE用途</th></tr></thead><tbody>${consensus.map((item) => `<tr><td><b>${esc(item.institution)}</b></td><td>${esc(item.report_date || "—")}</td><td>${num(text(item, ["np_2026e", "net_profit_2026e"]))}</td><td>${num(text(item, ["np_2027e", "net_profit_2027e"]))}</td><td>${esc(item.profit_scope || "待核对")}</td><td>${num(item.pe_2026e, 1)}</td><td><b>${esc(item.pe_usability || "待核对")}</b><small>${esc(item.scope_reason || "")}</small></td></tr>`).join("") || "<tr><td colspan='7'>暂无结构化一致预期</td></tr>"}</tbody>`;

  const range = pe.step1_comparable_range || {};
  const valuationInputs = report.valuation_inputs || {};
  const modelPillars = report.stage2_baseline?.pillars || [];
  const pillarMultiples = (type) => modelPillars.filter((pillar) => pillar.pillar_type === type).map((pillar) => {
    const multiple = pillar.years?.["2026e"]?.base?.multiple;
    return `${pillar.name} ${multiple == null ? "—" : `${num(multiple, 1)}x`}`;
  }).join(" / ") || "—";
  $("pe-panel").innerHTML = [["主营支柱基准倍数", pillarMultiples("operating")], ["独立事项基准倍数", pillarMultiples("independent_event")], ["公司级PE交叉检查", pe.final_pe == null ? "—" : `${num(pe.final_pe, 1)}x（不直接套用）`], ["可比参考区间", `${num(range.lower, 1)}–${num(range.upper, 1)}x`], ["口径匹配", valuationInputs.pe_scope_basis || "旧运行待核对"], ["排除锚", listText(valuationInputs.excluded_anchor_names) || "—"]].map(([label, value]) => `<div><span>${label}</span><b>${esc(value)}</b></div>`).join("") + `<div class="pe-detail">${esc(pe.details || "")}</div>`;
  $("comparable-table").innerHTML = `<thead><tr><th>估值锚 / 可比</th><th>PE</th><th>增速</th><th>毛利率</th></tr></thead><tbody>${(report.comparables || []).map((item) => `<tr><td>${esc(item.name)}</td><td>${num(item.pe, 1)}x</td><td>${pct(item.growth_rate)}</td><td>${pct(item.gross_margin)}</td></tr>`).join("") || "<tr><td colspan='4'>暂无结构化可比数据</td></tr>"}</tbody>`;

  const baseline = report.stage2_baseline || {};
  renderAssumptionLedger();
  $("divergence-cards").innerHTML = (report.market_divergences || []).map((item) => `<article class="divergence-card"><h3>${esc(item.name)}<span class="source-ids">${esc(item.pillar || "")} · ${item.pricing_status === "priced_in" ? "已定价" : "未充分定价"}</span></h3><div class="case bull"><b>乐观情形</b>${esc(item.bull_case)}</div><div class="case bear"><b>悲观情形</b>${esc(item.bear_case)}</div><div class="case ours"><b>我们的判断</b>${esc(item.our_judgement)}<span class="source-ids">根因：${esc(item.root_cause || "—")}</span>${sourceButtons(item)}</div></article>`).join("") || "<p>暂无结构化分歧。</p>";
  $("option-cards").innerHTML = (report.narrative_options || []).map((item) => `<article class="option-card ${item.included_in_valuation ? "included" : ""}"><span class="badge">${item.included_in_valuation ? "已进入上方估值模型" : "仅观察，不计入"}</span><h3>${esc(item.name)}</h3><p>${esc(item.business_essence)}<br><b>当前判断：</b>${esc(item.pricing_status || "待核对")} · <b>验证节点：</b>${esc(listText(item.verification_nodes) || "待补充")}</p>${sourceButtons(item)}</article>`).join("") || "<p>暂无独立事项。</p>";
  $("verification-list").innerHTML = (report.verification_nodes || []).map((item) => `<article class="verification-item"><div class="verification-time">${esc(item.timeframe || "待定")}</div><div class="verification-line"></div><div class="verification-body"><h3>${esc(item.event)}</h3><span>${esc(item.node_id || "")} · ${esc(item.pillar || "")}</span><div class="meaning-grid"><p><b>验证成功：</b>${esc(item.success_meaning || "—")}</p><p><b>验证失败：</b>${esc(item.failure_meaning || "—")}</p></div>${sourceButtons(item)}</div></article>`).join("");
  $("facts-list").innerHTML = (report.facts || []).map((item) => `<li>${esc(text(item, ["statement", "fact", "description"]))}${sourceButtons(item)}</li>`).join("") || "<li>—</li>";
  $("assumptions-list").innerHTML = (report.assumptions || []).map((item) => `<li><b>${esc(text(item, ["name", "assumption"]))}</b>${item.name ? `：${esc(item.value)}` : ""}${item.rationale ? `<br>${esc(item.rationale)}` : ""}${sourceButtons(item)}</li>`).join("") || "<li>—</li>";
  $("risks-list").innerHTML = (report.risks || []).map((item) => `<li>${esc(text(item, ["statement", "description", "risk"]))}${sourceButtons(item)}</li>`).join("") || "<li>—</li>";
  $("catalysts-list").innerHTML = (report.catalysts || []).map((item) => `<li><b>${esc(text(item, ["timeframe", "time"]))}</b> · ${esc(item.event || "—")}${sourceButtons(item)}</li>`).join("") || "<li>—</li>";
  $("evidence-summary").textContent = `${stats.evidence_count || 0} 条原始证据 · 按需加载`;
  $("run-id").textContent = `运行包 ${report.run_id}`;
  renderYearComparison();
  renderValuationYear();
  $("report-content").classList.remove("hidden");
}

async function loadEvidence(focusId) {
  if (!evidenceLoaded) {
    $("evidence-state").textContent = "正在加载证据包…";
    await loadScript(catalogEntry.evidence_path);
    evidenceLoaded = true;
    const evidence = window.QUANT_DASHBOARD_VALUATION_EVIDENCE?.[report.run_id]?.evidence || [];
    $("evidence-state").textContent = `${evidence.length} 条证据已加载。`;
    $("evidence-table").innerHTML = `<thead><tr><th>ID</th><th>标题 / 类型</th><th>日期</th><th>摘要</th><th>缓存定位</th></tr></thead><tbody>${evidence.map((item) => `<tr id="evidence-${esc(item.source_id)}"><td><b>${esc(item.source_id)}</b></td><td>${esc(item.title || item.source_type || "—")}</td><td>${esc(item.published_at || "—")}</td><td class="evidence-excerpt">${esc(item.excerpt || "—")}</td><td>${esc(item.cache_path || "—")}</td></tr>`).join("")}</tbody>`;
  }
  if (focusId) {
    const row = document.getElementById(`evidence-${focusId}`);
    if (row) {
      document.querySelectorAll(".evidence-focus").forEach((item) => item.classList.remove("evidence-focus"));
      row.classList.add("evidence-focus");
      row.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  }
}

function bindInteractions() {
  document.querySelectorAll("[data-year]").forEach((button) => button.addEventListener("click", () => { selectedYear = button.dataset.year; renderValuationYear(); }));
  $("evidence-details").addEventListener("toggle", () => { if ($("evidence-details").open) loadEvidence().catch((error) => { $("evidence-state").textContent = error.message; }); });
  document.addEventListener("click", async (event) => {
    const source = event.target.closest(".source-link");
    if (!source) return;
    $("evidence-details").open = true;
    await loadEvidence(source.dataset.sourceId).catch((error) => { $("evidence-state").textContent = error.message; });
  });
  const links = [...document.querySelectorAll(".report-toc a")];
  const observer = new IntersectionObserver((entries) => {
    const visible = entries.filter((entry) => entry.isIntersecting).sort((left, right) => right.intersectionRatio - left.intersectionRatio)[0];
    if (!visible) return;
    links.forEach((link) => link.classList.toggle("active", link.hash === `#${visible.target.id}`));
  }, { rootMargin: "-18% 0px -65%", threshold: [0, 0.15, 0.5] });
  document.querySelectorAll(".report-section").forEach((section) => observer.observe(section));
}

async function start() {
  if (!catalog) throw new Error("最新估值目录尚未发布。");
  if (!code) throw new Error("缺少股票代码。");
  catalogEntry = (catalog.valuations || []).find((item) => item.code === code);
  if (!catalogEntry) throw new Error("没有找到该标的的已验证估值报告。");
  if (catalogEntry.report_path) {
    await loadScript(catalogEntry.report_path);
    report = window.QUANT_DASHBOARD_VALUATION_REPORTS?.[catalogEntry.run_id];
  } else {
    report = catalogEntry;
  }
  if (!report) throw new Error("公司研究数据包内容缺失。");
  renderStatic();
  bindInteractions();
}

start().catch((error) => showError(error.message));
