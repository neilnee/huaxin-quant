// Exercise the effective renderer so later overrides cannot hide score components.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8");
const helpers = source.slice(source.indexOf("function vcpStructureScoreItems("), source.indexOf("const VCP_CONTRACTION_META="));
const start = source.indexOf("renderVcpDetail=function(row){");
const end = source.indexOf("const renderVcpBase =", start);
assert(start > 0 && end > start);
let removedScore = false;
const detail = {
  innerHTML: "",
  querySelector: selector => selector === ".vcp-score-grid" ? {
    previousElementSibling: {remove() {}},
    remove() {removedScore = true;},
  } : null,
  querySelectorAll: () => [],
};
const context = {
  $: () => detail,
  esc: value => String(value ?? ""),
  vcpNumber: (value, digits = 1) => value == null ? "—" : Number(value).toFixed(digits),
  signalRiskLabel: value => value,
  vcpStatus: value => value,
  vcpPriorBonusMetric: () => "",
  vcpPriorBonusReasons: () => "",
  vcpContractionRows: () => "",
  vcpDestructiveResetPanel: () => "",
  sectorPhaseTag: () => "板块阶段",
  sectorHealthTag: () => "板块趋势",
  sectorStateTag: value => value,
  cls: () => "",
  pct: value => String(value ?? "—"),
};
vm.createContext(context);
vm.runInContext(helpers + source.slice(start, end), context);
const row = {
  code: "002543", name: "万和电气", structure_score: 78.43,
  score_components: {structure: 25, volume: 16.6667, impulse: 16.1283,
    trend: 5, position: 3.6364, contraction_extensions: 6, contraction_quality: 6},
  structure_score_details: {status: "COMPLETE",
    impulse: {status: "COMPLETE", base_date: "2026-08-07", peak_date: "2026-08-20",
      quality: 67.2317, retention_raw: 0.552381, retention_coefficient: 0.799637, contribution: 16.1283},
    contraction_quality: {status: "COMPLETE", score: 6,
      checks: {low_rising: true, drawdown_shrinking: true, volume_decreasing: true}},
  },
};
context.renderVcpDetail(row);
assert(detail.innerHTML.includes("<span>推进成果</span><b>16.13</b>"));
assert(detail.innerHTML.includes("<span>优质收缩序列</span><b>6.00</b>"));
assert(detail.innerHTML.includes("价量质量 67.23/100，成果保留 55.24%"));
assert(detail.innerHTML.includes("计分保留系数 79.96%"));
assert(detail.innerHTML.includes("首末低点抬升满足"));
context.renderVcpDetail({...row, score_components: {...row.score_components, impulse: 0, contraction_quality: 0}});
assert(detail.innerHTML.includes("<span>推进成果</span><b>0.00</b>"));
assert(detail.innerHTML.includes("<span>优质收缩序列</span><b>0.00</b>"));
context.renderVcpDetail({name: "旧包", score_components: {structure: 25, volume: 20, trend: 15, position: 10}});
assert(!detail.innerHTML.includes("推进成果"));
assert(!detail.innerHTML.includes("优质收缩序列"));
context.renderVcpDetail({...row, structure_score_details: {status: "INCOMPLETE"}});
assert(!detail.innerHTML.includes("价量质量"));
context.renderVcpDetail({...row, structure_score_details: {status: "COMPLETE",
  impulse: {status: "NO_IMPULSE"},
  contraction_quality: {status: "COMPLETE", checks: {}, score: 0, reasons: ["INSUFFICIENT_ROUNDS"]},
}});
assert(detail.innerHTML.includes("未识别到合格推进"));
assert(detail.innerHTML.includes("有效收缩不足两轮"));
context.renderVcpDetail({...row, tracking_scope: "POST_BREAKOUT", structure_breakout_score: 70});
assert(removedScore, "Post-breakout renderer must remove current score explanation");
if (process.argv[2]) {
  const packet = {window: {}};
  vm.createContext(packet);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), packet);
  const data = Object.values(Object.values(packet.window)[0])[0];
  for (const actual of data.candidates) {
    removedScore = false;
    context.renderVcpDetail(actual);
    if (actual.tracking_scope === "POST_BREAKOUT") {
      assert(removedScore);
      continue;
    }
    for (const [key, label] of [["impulse", "推进成果"], ["contraction_quality", "优质收缩序列"]]) {
      const value = actual.score_components?.[key];
      if (value != null) assert(detail.innerHTML.includes(`<span>${label}</span><b>${Number(value).toFixed(2)}</b>`));
    }
    assert(!detail.innerHTML.includes("NaN"));
  }
  console.log(`Published VCP packet: ${data.candidates.length} details passed.`);
}
console.log("VCP score renderer: new components, zero, legacy, missing data and frozen score passed.");
