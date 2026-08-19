const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const num = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const money = (value) => Number.isFinite(Number(value)) ? `${num(value, 2)} 元` : "—";
const numOrDash = (value, digits = 2) => Number.isFinite(Number(value)) && Number(value) !== 0 ? Number(value).toFixed(digits) : "—";
const moneyOrDash = (value) => Number.isFinite(Number(value)) && Number(value) !== 0 ? `${num(value, 2)} 元` : "—";
const pct = (value) => Number.isFinite(Number(value)) ? `${num(value, 1)}%` : "—";
const text = (item, keys) => keys.map((key) => item?.[key]).find((value) => value !== undefined && value !== null && value !== "") ?? "—";
const asList = (value) => {
  if (Array.isArray(value)) return value;
  if (value === undefined || value === null || value === "") return [];
  if (typeof value === "string") return value.split(/[,，;；]/).map((item) => item.trim()).filter(Boolean);
  return [value];
};
const itemText = (value, keys) => typeof value === "object" ? text(value, keys) : value;
const query = new URLSearchParams(location.search);
const code = query.get("code");
const catalog = window.QUANT_DASHBOARD_VALUATION_CATALOG || window.QUANT_DASHBOARD_VALUATION_LATEST;
let catalogEntry;
let report;
let selectedYear = "2026e";

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

function scenarioFor(year) {
  const values = report.valuation?.years?.[year];
  if (values) return values;
  if (year === "2026e") return report.valuation || {};
  return { base: report.valuation?.base_2027 };
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
  $("valuation-cards").innerHTML = cards.map(([label, value, note, klass]) => `<article class="valuation-card ${klass}"><span>${label}</span><b>${money(value)}</b><small class="${Number(String(note).replace("%", "")) >= 0 ? "positive" : "negative"}">${esc(note)}</small></article>`).join("");

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
  $("matrix-table").innerHTML = `<thead><tr><th>情景</th><th>净利润</th><th>目标 PE</th><th>估值</th><th>取值说明</th></tr></thead><tbody>${scenariosList.map(([key, label]) => { const item = year[key] || {}; return `<tr><td><b>${label}</b></td><td>${num(item.profit)} 亿</td><td>${num(item.multiple, 1)}x</td><td><b>${num(item.valuation)} 亿</b></td><td>${esc(item.reason || item.profit_reason || "—")}</td></tr>`; }).join("")}</tbody>`;
}

function renderBusinessMap() {
  const pillars = report.business_map || report.business_pillars || [];
  const anchorLabels = { core_anchor: "核心估值锚", important_component: "重要组成", growth_option: "增长期权", observation: "观察事项", excluded: "不进入估值", unclear: "锚定待确认" };
  $("business-map").innerHTML = pillars.map((item, index) => {
    const profile = item.business_value_profile || {};
    const role = profile.role_in_company?.text || item.business_essence || "旧运行未保存该业务在公司中的作用。";
    const earnings = profile.earnings_model?.text || "旧运行未保存收入与利润形成链条。";
    const growth = profile.growth_potential || {};
    const anchor = profile.valuation_anchor || {};
    const metrics = asList(profile.core_metrics);
    const tracking = asList(profile.tracking_metrics);
    const gaps = asList(profile.data_gaps);
    const metricCards = metrics.map((metric) => `<li><div><b>${esc(metric.name)}</b><small>${esc(metric.period || "口径未注明")}</small></div><strong>${esc(metric.value)}${metric.unit ? ` ${esc(metric.unit)}` : ""}</strong><p>${esc(metric.scope || "口径未注明")}</p></li>`).join("");
    const drivers = asList(growth.drivers).map(esc).join("、");
    const constraints = asList(growth.constraints).map(esc).join("、");
    const trackingRows = tracking.map((metric) => `<li><b>${esc(metric.name)}</b><span>${esc(metric.direction)}</span><p>${esc(metric.why)}</p></li>`).join("");
    return `<article class="business-map-card">
      <div class="business-map-index">${String(index + 1).padStart(2, "0")}</div>
      <div class="business-value-body">
        <div class="card-top"><h3>${esc(item.name)}</h3><span class="badge anchor-${esc(anchor.level || "unclear")}">${esc(anchorLabels[anchor.level] || "画像待补")}</span></div>
        <span class="business-role-label">在公司中的作用</span><p class="business-role">${esc(role)}</p>
        <div class="business-value-grid">
          <section><span>赚钱逻辑</span><p>${esc(earnings)}</p></section>
          <section><span>关键指标</span>${metricCards ? `<ul class="core-metric-list">${metricCards}</ul>` : "<p>暂无可可靠归属的量化指标。</p>"}</section>
          <section class="growth-anchor"><span>增长与估值</span><p>${esc(growth.text || "尚未形成有证据支撑的增长判断。")}</p><small>${esc(growth.horizon || "兑现周期未明确")}</small>${drivers ? `<p class="compact-factor"><b>驱动</b>${drivers}</p>` : ""}${constraints ? `<p class="compact-factor constraint"><b>约束</b>${constraints}</p>` : ""}<div class="anchor-copy"><b>估值关系</b><p>${esc(anchor.text || item.institution_coverage || "尚未确认机构估值对该业务的锚定方式。")}</p></div></section>
        </div>
        ${(trackingRows || gaps.length) ? `<details class="business-more"><summary>跟踪指标与数据缺口</summary><div>${trackingRows ? `<ul class="pillar-tracking-list">${trackingRows}</ul>` : ""}${gaps.length ? `<p class="business-data-gaps"><b>尚缺：</b>${gaps.map(esc).join("、")}</p>` : ""}</div></details>` : ""}
      </div>
    </article>`;
  }).join("") || "<p class='section-note'>暂无结构化业务地图。</p>";
}

function renderProfitAnalysis() {
  const analyses = report.institution_profit_analysis || [];
  $("profit-analysis-list").innerHTML = analyses.map((item) => {
    const logic = item.profit_logic || {};
    const steps = asList(logic.steps);
    const missing = asList(logic.missing_links);
    const assumptions = asList(item.key_assumptions);
    const risks = asList(item.risks);
    const statusLabels = { complete: "链条较完整", partial: "部分链条", endpoint_only: "仅利润终点" };
    const stageLabels = { operating_driver: "经营驱动", volume: "业务量", price: "价格", business_mix: "业务结构", revenue: "营业收入", margin: "利润率", cost: "成本", expense: "费用", operating_profit: "经营利润", net_profit: "归母净利润", other: "其他" };
    const chain = steps.map((step) => `<li><div><b>${esc(stageLabels[step.stage] || step.stage || "节点")}</b><small>${esc(step.period || "")}</small></div><p>${esc(step.statement || "—")}</p></li>`).join("");
    const assumptionRows = assumptions.map((value) => {
      if (!value || typeof value !== "object") return `<li><b>${esc(value)}</b><p>旧运行未保存对应跟踪指标和失效信号</p></li>`;
      const target = value.explicit_target;
      const targetText = target && typeof target === "object" ? `；明确目标 ${num(target.value)}${esc(target.unit || "")}（${esc(target.period || "")}）` : "";
      const origin = value.tracking_origin === "report_explicit" ? "机构明确提出" : "由机构假设映射";
      return `<li><b>${esc(value.assumption || "—")}</b><p>跟踪：${esc(value.tracking_metric || "未明确")}；方向：${esc(value.expected_direction || "未明确")}${targetText}</p><p>周期：${esc(value.timeframe || "未明确")} · ${esc(origin)}</p><p class="failure-signal">失效：${esc(value.failure_signal || "研报未披露")}</p></li>`;
    }).join("");
    const gaps = missing.map((value) => `<li>${esc(itemText(value, ["statement", "description"]))}</li>`).join("");
    const riskRows = risks.map((value) => `<li>${esc(itemText(value, ["risk", "statement", "name", "description"]))}</li>`).join("");
    return `<article class="profit-analysis-card"><div class="profit-analysis-head"><div><h3>${esc(item.institution)}</h3><span>${esc(item.report_date || "—")}</span></div><div><b>2026E ${num(item.np_2026e)} 亿</b><b>2027E ${num(item.np_2027e)} 亿</b><i>${esc(statusLabels[logic.status] || "历史兼容")}</i></div></div><p class="profit-summary">${esc(logic.summary || item.summary || "研报未披露利润传导逻辑")}</p><div class="profit-analysis-grid"><section><span>利润预测逻辑链</span><ol class="profit-chain">${chain || "<li><p>研报未结构化披露</p></li>"}</ol>${gaps ? `<div class="logic-gaps"><b>尚未披露的环节</b><ul>${gaps}</ul></div>` : ""}</section><section><span>成立假设与后续跟踪</span><ul class="tracking-list">${assumptionRows || "<li><b>研报未结构化披露</b><p>不补写假设或数值阈值</p></li>"}</ul>${riskRows ? `<div class="logic-gaps"><b>补充风险</b><ul>${riskRows}</ul></div>` : ""}</section></div></article>`;
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
  $("research-stats").innerHTML = [[stats.consensus_count, "家机构"], [(report.business_map || report.business_pillars || []).length, "个业务支柱"], [(report.market_divergences || []).length, "项分歧"], [stats.verification_count, "个验证点"]].map(([value, label]) => `<div><b>${num(value, 0)}</b><span>${label}</span></div>`).join("");
  $("reverse-check").textContent = reverse.triggered ? `反向检查已触发 · ${reverse.discount_rate_desc || ""}` : "反向检查未触发";
  const summary = report.company_summary || {};
  $("summary-company-profile").textContent = summary.company_profile || "历史运行尚未保存结构化公司摘要";
  $("summary-earnings").textContent = summary.earnings_consensus || "未形成量化盈利摘要";
  $("summary-growth").textContent = summary.growth_logic || "未形成结构化增长逻辑";
  $("summary-uncertainties").textContent = summary.key_uncertainties || "未形成结构化不确定性摘要";
  $("summary-tracking").textContent = summary.tracking_focus || "未形成结构化跟踪重点";


  const consensus = report.consensus || [];
  const np26 = consensus.map((item) => Number(text(item, ["np_2026e", "net_profit_2026e"]))).filter(Number.isFinite);
  const np27 = consensus.map((item) => Number(text(item, ["np_2027e", "net_profit_2027e"]))).filter(Number.isFinite);
  $("consensus-summary").innerHTML = [["机构数量", consensus.length], ["2026E 中位利润", median(np26) == null ? "—" : `${num(median(np26))} 亿`], ["2026E 预测区间", np26.length ? `${num(Math.min(...np26))}–${num(Math.max(...np26))} 亿` : "—"], ["2027E 中位利润", median(np27) == null ? "—" : `${num(median(np27))} 亿`]].map(([label, value]) => `<div><span>${label}</span><b>${value}</b></div>`).join("");
  $("consensus-table").innerHTML = `<thead><tr><th>机构</th><th>报告日期</th><th>2026E 营收</th><th>2026E 净利</th><th>2027E 净利</th><th>目标价</th></tr></thead><tbody>${consensus.map((item) => `<tr><td><b>${esc(item.institution)}</b></td><td>${esc(item.report_date || "—")}</td><td>${numOrDash(item.revenue_2026e)}</td><td>${numOrDash(text(item, ["np_2026e", "net_profit_2026e"]))}</td><td>${numOrDash(text(item, ["np_2027e", "net_profit_2027e"]))}</td><td>${moneyOrDash(item.target_price)}</td></tr>`).join("") || "<tr><td colspan='6'>暂无结构化一致预期</td></tr>"}</tbody>`;

  renderBusinessMap();
  renderProfitAnalysis();
  $("divergence-cards").innerHTML = (report.market_divergences || []).map((item) => `<article class="divergence-card"><h3>${esc(item.name)}</h3><div class="case bull"><b>较高预测</b>${esc(item.bull_case)}</div><div class="case bear"><b>较低预测</b>${esc(item.bear_case)}</div><div class="case ours"><b>共识解读</b>${esc(item.our_judgement)}<p><b>根因：</b>${esc(item.root_cause || "原因未披露")}</p></div></article>`).join("") || "<p>暂无结构化分歧。</p>";
  $("verification-list").innerHTML = (report.verification_nodes || []).map((item) => `<article class="verification-item"><div class="verification-time">${esc(item.timeframe || "待定")}</div><div class="verification-line"></div><div class="verification-body"><h3>${esc(item.event)}</h3><div class="meaning-grid"><p><b>验证成功：</b>${esc(item.success_meaning || "—")}</p><p><b>验证失败：</b>${esc(item.failure_meaning || "—")}</p></div></div></article>`).join("");
  $("risks-list").innerHTML = (report.risks || []).map((item) => `<li>${esc(text(item, ["statement", "description", "risk"]))}</li>`).join("") || "<li>—</li>";
  $("run-id").textContent = `运行包 ${report.run_id}`;
  renderValuationYear();
  $("report-content").classList.remove("hidden");
}

function bindInteractions() {
  document.querySelectorAll("[data-year]").forEach((button) => button.addEventListener("click", () => { selectedYear = button.dataset.year; renderValuationYear(); }));
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
