const q = new URLSearchParams(location.search), run = q.get("run"), esc = (value) => String(value ?? "").replace(/[&<>"']/g, (char) => ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[char]));
const statusLabel = (status) => ({done:"已完成",running:"进行中",failed:"失败",pending:"等待中",skipped:"已跳过"}[status] || status);
function render(data) {
  const row = (data.runs || []).find((item) => item.run_id === run);
  if (!row) { document.getElementById("meta").textContent = "未找到运行进度数据。"; return; }
  document.getElementById("title").textContent = `${row.name || row.code} 分析进度`;
  document.getElementById("meta").textContent = `状态：${statusLabel(row.status)} · 更新时间：${data.generated_at || row.updated_at || "—"}${row.error ? ` · ${row.error}` : ""}`;
  document.getElementById("steps").innerHTML = row.stages.map((stage) => `<li class="progress-stage progress-${esc(stage.status)}"><div class="progress-stage-title"><b>${esc(stage.name)}</b><span>${statusLabel(stage.status)}</span></div><ol>${stage.steps.map((step) => { const batch = step.completed_batches ?? step.current_batch; const detail = step.total_batches ? `第 ${batch || 0}/${step.total_batches} 批` : step.source_count ? `${step.source_count} 份证据` : ""; return `<li><span>${esc(step.name)}</span><b>${statusLabel(step.status)}</b>${detail ? `<small>${esc(detail)}</small>` : ""}</li>`; }).join("")}</ol></li>`).join("");
}
function refresh() {
  const script = document.createElement("script");
  script.src = `data/valuation_progress.js?t=${Date.now()}`;
  script.onload = () => { render(window.QUANT_DASHBOARD_VALUATION_PROGRESS || {}); script.remove(); };
  script.onerror = () => { document.getElementById("meta").textContent = "进度数据加载失败。"; script.remove(); };
  document.head.appendChild(script);
}
refresh(); setInterval(refresh, 5000);
