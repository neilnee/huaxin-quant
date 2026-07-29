const $ = (id) => document.getElementById(id);
const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
const number = (value, digits = 2) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : "—";
const query = new URLSearchParams(window.location.search);
const date = query.get("date") || window.QUANT_DASHBOARD_INDEX?.valuation?.latest;
const code = query.get("code");
const monthPath = (value) => `20${value.slice(0, 4)}`;
function showError(message) { $("report-error").textContent = message; $("report-error").classList.remove("hidden"); }
function loadContext(value) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `data/${monthPath(value)}/valuation_context_${value}.js`;
    script.onload = () => resolve((window.QUANT_DASHBOARD_VALUATION_CONTEXTS || {})[value]);
    script.onerror = () => reject(new Error("估值数据包不存在"));
    document.head.appendChild(script);
  });
}
function list(items, target, render) { $(target).innerHTML = (items || []).map(render).join("") || "<li>—</li>"; }
function render(row, context) {
  const value = row.valuation || {}, layers = row.matrix_2026e?.layers_summary || {}, reverse = row.status?.reverse_check || {};
  $("report-title").textContent = `${row.name}（${row.code}）投研分析`;
  $("report-meta").textContent = `研究日期 ${context.meta?.run_date || "—"}`;
  $("company-name").textContent = `${row.name}（${row.code}）`;
  $("company-status").textContent = `共识：${row.status?.consensus?.status || "—"} · 反向检查：${reverse.triggered ? "已触发" : "未触发"}`;
  $("valuation-range").innerHTML = [["当前价", value.current_price], ["悲观", value.pessimistic], ["2026E 基准", value.base], ["乐观", value.optimistic], ["2027E 基准", value.base_2027], ["基准空间", value.base_upside_pct == null ? null : `${number(value.base_upside_pct, 1)}%`]].map(([label, raw]) => `<div><span>${label}</span><b>${typeof raw === "string" ? raw : number(raw)}</b></div>`).join("");
  const thesis = row.investment_thesis || {};
  $("thesis-content").innerHTML = `<div><span>市场正在交易什么</span><b>${esc(thesis.market_trade || "—")}</b><p>${esc(thesis.core_judgement || "—")}</p><small>公司阶段：${esc(thesis.company_stage || "—")}</small></div>`;
  $("business-pillars").innerHTML = (row.business_pillars || []).map((item) => { const bridge = item.profit_bridge || {}; const calc = (row.calc_pillars || []).find((x) => x.name === item.name) || {}; const showBridge = (items) => (items || []).map((x) => `${esc(x.item)} ${number(x.profit_impact, 2)}亿`).join("；") || "—"; const pipeline = (calc.type_b_pipeline || []).map((x) => `${esc(x.name)}：${number(x.project_profit, 2)}亿 × ${number(x.pe, 1)}x × ${number(Number(x.probability) * 100, 0)}%`).join("；"); const l2 = calc.layer2 ? `Layer 2：${number(calc.layer2.base, 2)}亿（${esc(calc.layer2.description || "")})` : ""; const route = item.calculation_mapping?.route || "排除"; return `<article><h3>${esc(item.name)}</h3><p>${esc(item.business_essence)}</p><small>拆分理由：${esc(item.split_rationale)} · 路线：${esc(route)}</small><p><b>2025A：</b>${number(bridge.base_2025a, 2)}亿<br><b>2026E 利润桥：</b>${showBridge(bridge.items_2026e)}<br><b>2027E 利润桥：</b>${showBridge(bridge.items_2027e)}${pipeline ? `<br><b>Type B 管道：</b>${pipeline}` : ""}${l2 ? `<br><b>${l2}</b>` : ""}</p></article>`; }).join("") || "<p class='muted'>该运行包未包含新版利润支柱研究卡。</p>";
  $("layer-grid").innerHTML = ["layer1", "layer2", "layer3"].map((key) => `<div><span>${key.toUpperCase()}</span><b>${number(layers[key]?.base, 2)}</b><small>${esc(layers[key]?.pricing_status || "")}</small></div>`).join("");
  $("pillar-table").innerHTML = `<thead><tr><th>支柱</th><th>Layer 1</th><th>Layer 2</th><th>Layer 3</th><th>合计</th></tr></thead><tbody>${(row.matrix_2026e?.matrix_rows || []).map((item) => `<tr><td>${esc(item.pillar_name)}</td><td>${number(item.layer1?.base, 2)}</td><td>${number(item.layer2?.base, 2)}</td><td>${number(item.layer3?.base, 2)}</td><td>${number(item.pillar_total?.base, 2)}</td></tr>`).join("") || "<tr><td colspan='5' class='muted'>暂无支柱数据</td></tr>"}</tbody>`;
  const divergence = (row.market_divergences || []).map((item) => `<article><h3>${esc(item.name)} <small>${esc(item.pillar)}</small></h3><p><b>乐观：</b>${esc(item.bull_case)}<br><b>悲观：</b>${esc(item.bear_case)}<br><b>我的判断：</b>${esc(item.our_judgement)}</p></article>`);
  const options = (row.narrative_options || []).map((item) => `<article><h3>${esc(item.name)} <small>${item.included_in_valuation ? "计入估值" : "仅观察"}</small></h3><p>${esc(item.business_essence)}<br>潜在利润 ${number(item.potential_profit, 2)} 亿 · ${number(item.pe, 1)}x · 概率 ${number(Number(item.probability) * 100, 0)}%</p></article>`);
  $("divergence-content").innerHTML = [...divergence, ...options].join("") || "<p class='muted'>该运行包未包含新版分歧和叙事研究卡。</p>";
  $("verification-table").innerHTML = `<thead><tr><th>时间</th><th>待验证事件</th><th>支柱</th><th>成功 / 失败的含义</th></tr></thead><tbody>${(row.verification_nodes || []).map((item) => `<tr><td>${esc(item.timeframe)}</td><td>${esc(item.event)}</td><td>${esc(item.pillar)}</td><td>${esc(item.success_meaning)} / ${esc(item.failure_meaning)}</td></tr>`).join("") || "<tr><td colspan='4' class='muted'>该运行包未包含新版验证节点。</td></tr>"}</tbody>`;
  list(row.facts, "facts-list", (item) => `<li>${esc(item.statement)} <span class="muted">[${esc((item.source_ids || []).join(", "))}]</span></li>`);
  list(row.assumptions, "assumptions-list", (item) => `<li><b>${esc(item.name)}：</b>${esc(item.value)} <span class="muted">[${esc((item.source_ids || []).join(", "))}]</span></li>`);
  list(row.risks, "risks-list", (item) => `<li>${esc(item.statement)}</li>`);
  list(row.catalysts, "catalysts-list", (item) => `<li>${esc(item.timeframe)} ${esc(item.event)}</li>`);
  $("run-id").textContent = `运行包：${row.run_id}`;
  $("evidence-table").innerHTML = `<thead><tr><th>ID</th><th>类型</th><th>日期</th><th>缓存文件</th></tr></thead><tbody>${(row.evidence || []).map((item) => `<tr><td>${esc(item.source_id)}</td><td>${esc(item.source_type)}</td><td>${esc(item.published_at || "—")}</td><td>${esc(item.cache_path)}</td></tr>`).join("") || "<tr><td colspan='4' class='muted'>无证据目录</td></tr>"}</tbody>`;
  $("report-content").classList.remove("hidden");
}
if (!date || !code) showError("缺少估值日期或股票代码。");
else loadContext(date).then((context) => { const row = (context?.valuations || []).find((item) => item.code === code); if (!row) throw new Error("该标的在该日期没有完成的估值运行"); render(row, context); }).catch((error) => showError(error.message));
