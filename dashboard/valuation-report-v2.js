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
const itemText = (value, keys) => typeof value === "object" ? text(value, keys) : value;
const query = new URLSearchParams(location.search);
const code = query.get("code");
const catalog = window.QUANT_DASHBOARD_VALUATION_CATALOG || window.QUANT_DASHBOARD_VALUATION_LATEST;
let catalogEntry;
let report;
let selectedYear = "2026e";
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
  const scenariosList = [["pessimistic", "悲观"], ["base", "基准"], ["optimistic", "乐观"]];
  const consensusYear = baseline.consensus_valuation?.years?.[selectedYear];
  const fallbackPillar = (baseline.pillars || []).find((pillar) => pillar.pillar_type === "operating") || (baseline.pillars || [])[0];
  const year = consensusYear || fallbackPillar?.years?.[selectedYear] || {};
  const sampleCount = baseline.consensus_valuation?.sample_count ?? baseline.operating_profit_control?.[selectedYear]?.included_forecasts?.length;
  $("baseline-status").textContent = baseline.note || `${sampleCount == null ? "机构样本已清洗" : `${sampleCount} 个有效估值样本`} · 公司整体口径`;
  $("matrix-unit").textContent = `${yearLabel} · 净利润和估值单位亿元；PE仅采用机构目标估值，不使用当前股价倒推PE`;
  $("matrix-table").innerHTML = `<thead><tr><th>情景</th><th>对应机构</th><th>净利润</th><th>目标 PE</th><th>目标价</th><th>估值</th><th>取值说明</th></tr></thead><tbody>${scenariosList.map(([key, label]) => { const item = year[key] || {}; return `<tr><td><b>${label}</b></td><td>${esc(listText(item.institutions || item.institution) || "—")}</td><td>${num(item.profit)} 亿</td><td>${num(item.multiple, 1)}x</td><td>${money(item.target_price)}</td><td><b>${num(item.valuation)} 亿</b></td><td>${esc(item.reason || item.profit_reason || "—")}${sourceButtons(item)}</td></tr>`; }).join("")}</tbody>`;
}

function renderBusinessMap() {
  const pillars = report.business_map || report.business_pillars || [];
  const natureLabels = { recurring_operation: "持续经营", future_event: "成长项目", historical_realized: "历史事项", narrative_candidate: "潜在事项" };
  $("business-map").innerHTML = pillars.map((item, index) => {
    const bridge = item.profit_bridge || {};
    const coverage = item.institution_coverage || item.coverage_summary || "待结合逐机构研报判断是否已纳入盈利预测";
    return `<article class="business-map-card"><div class="business-map-index">${String(index + 1).padStart(2, "0")}</div><div><div class="card-top"><h3>${esc(item.name)}</h3><span class="badge">${esc(natureLabels[item.economic_nature] || item.classification || "业务组成")}</span></div><p>${esc(item.business_essence || item.split_rationale || "—")}</p><dl><div><dt>拆分逻辑</dt><dd>${esc(item.split_reason || item.split_rationale || "—")}</dd></div><div><dt>利润关系</dt><dd>${esc(coverage)}</dd></div>${bridge["2025a"] == null ? "" : `<div><dt>2025A参考</dt><dd>${num(bridge["2025a"])} ${esc(bridge.unit || "亿元")}</dd></div>`}</dl>${sourceButtons(item)}</div></article>`;
  }).join("") || "<p class='section-note'>暂无结构化业务地图。</p>";
}

function renderProfitAnalysis() {
  const analyses = report.institution_profit_analysis || [];
  $("profit-analysis-list").innerHTML = analyses.map((item) => {
    const assumptions = asList(item.key_assumptions);
    const drivers = asList(item.profit_drivers);
    const risks = asList(item.risks);
    return `<article class="profit-analysis-card"><div class="profit-analysis-head"><div><h3>${esc(item.institution)}</h3><span>${esc(item.report_date || "—")} · ${esc(item.profit_scope || "口径待核对")}</span></div><div><b>2026E ${num(item.np_2026e)} 亿</b><b>2027E ${num(item.np_2027e)} 亿</b></div></div><p class="profit-summary">${esc(item.summary || "研报摘要待补充")}</p><div class="profit-analysis-grid"><div><span>利润驱动</span><ul>${drivers.map((value) => `<li>${esc(itemText(value, ["driver", "statement", "name", "description"]))}</li>`).join("") || "<li>研报未结构化披露</li>"}</ul></div><div><span>关键假设</span><ul>${assumptions.map((value) => `<li>${esc(itemText(value, ["assumption", "statement", "name", "description"]))}</li>`).join("") || "<li>研报未结构化披露</li>"}</ul></div><div><span>主要风险</span><ul>${risks.map((value) => `<li>${esc(itemText(value, ["risk", "statement", "name", "description"]))}</li>`).join("") || "<li>研报未结构化披露</li>"}</ul></div></div>${sourceButtons(item)}</article>`;
  }).join("") || "<p class='section-note'>旧运行尚未保存逐机构利润逻辑；原始预测仍可在下表查看。</p>";
}

function renderStatic() {
  const valuation = report.valuation || {}, thesis = report.investment_thesis || {}, stats = report.research_stats || {}, reverse = report.status?.reverse_check || {};
  const consensusStatus = report.status?.consensus || {};
  document.title = `${report.name}（${report.code}）· 深度估值报告`;
  $("company-name").textContent = report.name;
  $("company-code").textContent = report.code;
  $("report-meta").textContent = `研究日期 ${dateLabel(report.analysis_date)}`;
  $("market-trade").textContent = thesis.market_trade || "—";
  $("company-stage").textContent = `公司阶段：${thesis.company_stage || "—"}`;
  $("consensus-status").textContent = consensusStatus.status === "done" ? `共识合格 · ${consensusStatus.valid_institutions || stats.consensus_count || 0} 家` : (consensusStatus.status || "共识状态缺失");
  $("quote-status").textContent = valuation.price_date ? `行情：${valuation.price_date} · ${valuation.price_source || "未知来源"}` : "行情日期缺失";
  $("research-stats").innerHTML = [[stats.evidence_count, "份证据"], [stats.consensus_count, "家机构"], [(report.business_map || report.business_pillars || []).length, "个业务支柱"], [stats.verification_count, "个验证点"]].map(([value, label]) => `<div><b>${num(value, 0)}</b><span>${label}</span></div>`).join("");
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
  $("consensus-table").innerHTML = `<thead><tr><th>机构</th><th>报告日期</th><th>2026E 营收</th><th>2026E 净利</th><th>2027E 净利</th><th>利润口径</th><th>目标价</th></tr></thead><tbody>${consensus.map((item) => `<tr><td><b>${esc(item.institution)}</b></td><td>${esc(item.report_date || "—")}</td><td>${num(item.revenue_2026e)}</td><td>${num(text(item, ["np_2026e", "net_profit_2026e"]))}</td><td>${num(text(item, ["np_2027e", "net_profit_2027e"]))}</td><td><b>${esc(item.profit_scope || "待核对")}</b><small>${esc(item.profit_scope_reason || item.scope_reason || "")}</small></td><td>${money(item.target_price)}</td></tr>`).join("") || "<tr><td colspan='7'>暂无结构化一致预期</td></tr>"}</tbody>`;

  renderBusinessMap();
  renderProfitAnalysis();
  $("divergence-cards").innerHTML = (report.market_divergences || []).map((item) => `<article class="divergence-card"><h3>${esc(item.name)}<span class="source-ids">${esc(item.divergence_type || item.pillar || "机构观点分歧")}</span></h3><div class="case bull"><b>较高预测</b>${esc(item.bull_case)}</div><div class="case bear"><b>较低预测</b>${esc(item.bear_case)}</div><div class="case ours"><b>共识解读</b>${esc(item.our_judgement)}<span class="source-ids">根因：${esc(item.root_cause || "原因未披露")}</span>${sourceButtons(item)}</div></article>`).join("") || "<p>暂无结构化分歧。</p>";
  $("verification-list").innerHTML = (report.verification_nodes || []).map((item) => `<article class="verification-item"><div class="verification-time">${esc(item.timeframe || "待定")}</div><div class="verification-line"></div><div class="verification-body"><h3>${esc(item.event)}</h3><span>${esc(item.node_id || "")} · ${esc(item.pillar || "")}</span><div class="meaning-grid"><p><b>验证成功：</b>${esc(item.success_meaning || "—")}</p><p><b>验证失败：</b>${esc(item.failure_meaning || "—")}</p></div>${sourceButtons(item)}</div></article>`).join("");
  $("risks-list").innerHTML = (report.risks || []).map((item) => `<li>${esc(text(item, ["statement", "description", "risk"]))}${sourceButtons(item)}</li>`).join("") || "<li>—</li>";
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
