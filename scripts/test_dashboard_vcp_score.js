// Exercise the effective renderer so later overrides cannot hide score components.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8");
const helpers = source.slice(source.indexOf("function vcpImpulsePanel("), source.indexOf("const VCP_CONTRACTION_META="));
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
  structure_score_details: {status: "COMPLETE", strategy_version: "model2_quant_score_v10",
    extension_score_adjustments: [{type: "TERMINAL_MICRO_CONTRACTION", trial_score: 6}],
    terminal_micro: {status: "COMPLETE", score: 6, checks: {}},
    impulse: {status: "COMPLETE", anchor_id: "test-anchor", base_date: "2026-08-07", peak_date: "2026-08-20",
      base_close: 6.55, peak_close: 7.6, retention_low_date: "2026-08-27", retention_low_close: 7.13,
      metrics: {gain_pct: 16.030534, up_volume_ratio: 4.588205, up_down_volume_ratio: 1.754281},
      quality: 67.2317, retention_raw: 0.552381, retention_coefficient: 0.799637, contribution: 16.1283},
    impulse_evidence: {selected: {anchor: {anchor_id: "test-anchor"}, price: {advance_days: 9},
      group_round_numbers: [1,2], retention: {current_close_retention_pct: 90}}},
    contraction_quality: {status: "COMPLETE", score: 6,
      checks: {low_rising: true, drawdown_shrinking: true, volume_decreasing: true}},
  },
};
const card = label => detail.innerHTML.match(new RegExp(`<div class="vcp-score-item[^\"]*"><span>${label}</span><div class="vcp-score-value">([\\s\\S]*?)<\\/div><\\/div>`))?.[1];
context.renderVcpDetail(row);
assert.equal(card("推进成果"), "<b>16.13</b><small>满分 30</small>");
assert.equal(card("优质收缩序列"), "<b>6.00</b>");
assert.equal(card("确认型重置"), "<b>0.00</b>");
assert.equal(card("末端承接"), "<b>6.00</b>");
assert.equal(card("扩展收缩"), undefined);
assert.equal((detail.innerHTML.match(/class="vcp-score-item score-extension"/g)||[]).length, 3);
for (const [label, maximum] of [["阶段基础",40],["整理量能",20],["趋势",5],["位置",5]]) {
  assert(card(label).includes(`<small>满分 ${maximum}</small>`));
}
assert(detail.innerHTML.includes("价量质量 67.23/100"));
assert(detail.innerHTML.includes("计分保留系数 79.96%"));
assert(detail.innerHTML.includes("首末低点抬升满足"));
assert(detail.innerHTML.includes('title="推进起点"><time>2026-08-07</time><b>6.55</b>'));
assert(detail.innerHTML.includes('title="推进终点"><time>2026-08-20</time><b>7.60</b>'));
assert(detail.innerHTML.includes('<b>+16.03%</b><small>9个交易日</small>'));
assert(detail.innerHTML.includes('<span>上涨日放量</span><b>4.59倍</b>'));
assert(detail.innerHTML.includes('title="收缩最低成果保留"><span>最低成果保留</span><b>55.24%</b>'));
assert(detail.innerHTML.includes('title="当前收盘成果保留"><span>当前成果保留</span><b>90.00%</b>'));
assert(detail.innerHTML.includes('关联当前第1、2轮标准收缩'));
assert(!detail.innerHTML.includes('附加原因'));
context.renderVcpDetail({...row, prior_breakout_bonus_score: 90, prior_breakout_bonus_reasons: ['旧摘要']});
assert(!detail.innerHTML.includes('旧摘要'));
assert(!detail.innerHTML.includes('突破前结构分'));
context.renderVcpDetail({...row, structure_score_details: {...row.structure_score_details,
  impulse: {...row.structure_score_details.impulse, no_down_days: true,
    metrics: {...row.structure_score_details.impulse.metrics, up_down_volume_ratio: null}},
}});
assert(detail.innerHTML.includes('<span>涨跌日均量比</span><b>无下跌日</b>'));
context.renderVcpDetail({...row, structure_score_details: {...row.structure_score_details,
  impulse_evidence: {selected: {anchor: {anchor_id: 'different'}}},
}});
assert(detail.innerHTML.includes('推进锚点与评分记录不一致'));
assert(!detail.innerHTML.includes('vcp-impulse-body'));
context.renderVcpDetail({...row, score_components: {...row.score_components, impulse: 0, contraction_quality: 0}});
assert.equal(card("推进成果"), "<b>0.00</b><small>满分 30</small>");
assert.equal(card("优质收缩序列"), "<b>0.00</b>");
context.renderVcpDetail({...row, structure_score_details: {...row.structure_score_details,
  extension_score_adjustments: [{type: "CONFIRMED_RESET_CONTRACTION", trial_score: 3},
    {type: "TERMINAL_MICRO_CONTRACTION", trial_score: 6}],
}});
assert.equal(card("确认型重置"), "<b>3.00</b>");
assert.equal(card("末端承接"), "<b>6.00</b>");
context.renderVcpDetail({...row, structure_score_details: {...row.structure_score_details,
  extension_score_adjustments: [], terminal_micro: {status: "COMPLETE", score: 0, checks: {}},
}});
assert.equal(card("确认型重置"), "<b>0.00</b>");
assert.equal(card("末端承接"), "<b>0.00</b>");
context.renderVcpDetail({name: "旧包", score_components: {structure: 25, volume: 20, trend: 15, position: 10}});
assert(!detail.innerHTML.includes("推进成果"));
assert(!detail.innerHTML.includes("优质收缩序列"));
assert(!detail.innerHTML.includes("满分"));
assert(detail.innerHTML.includes("历史记录未保存现行推进识别明细"));
context.renderVcpDetail({name: "旧扩展", score_components: {contraction_extensions: 12},
  contraction_extensions: [{type: "CONFIRMED_RESET_CONTRACTION", score: 6}]});
assert.equal(card("扩展收缩"), "<b>12.00</b>");
assert.equal(card("确认型重置"), undefined);
context.renderVcpDetail({...row, structure_score_details: {status: "INCOMPLETE"}});
assert(!detail.innerHTML.includes("价量质量"));
context.renderVcpDetail({...row, structure_score_details: {status: "INCOMPLETE", strategy_version: "model2_quant_score_v10"}});
assert(detail.innerHTML.includes("推进段数据不足"));
context.renderVcpDetail({...row, structure_score_details: {status: "COMPLETE", strategy_version: "model2_quant_score_v10",
  impulse: {status: "NO_IMPULSE"},
  contraction_quality: {status: "COMPLETE", checks: {}, score: 0, reasons: ["INSUFFICIENT_ROUNDS"]},
}});
assert(detail.innerHTML.includes("未识别到合格推进"));
assert(!detail.innerHTML.includes("vcp-impulse-body"));
assert(detail.innerHTML.includes("有效收缩不足两轮"));
context.renderVcpDetail({...row, tracking_scope: "POST_BREAKOUT", structure_breakout_score: 70});
assert(removedScore, "Post-breakout renderer must remove current score explanation");
assert(detail.innerHTML.includes("展示当前识别的推进，不用于解释突破时冻结结构分"));
if (process.argv[2]) {
  const packet = {window: {}};
  vm.createContext(packet);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), packet);
  const data = Object.values(Object.values(packet.window)[0])[0];
  for (const actual of data.candidates) {
    removedScore = false;
    context.renderVcpDetail(actual);
    assert(!detail.innerHTML.includes('附加原因'));
    const scored = actual.structure_score_details;
    if (scored?.strategy_version === 'model2_quant_score_v10' && scored.status === 'COMPLETE'
        && scored.impulse?.status === 'COMPLETE') {
      assert(detail.innerHTML.includes('vcp-impulse-body'));
      assert(detail.innerHTML.includes(scored.impulse.base_date));
      assert(detail.innerHTML.includes(scored.impulse.peak_date));
      assert(detail.innerHTML.includes(`<span>最低成果保留</span><b>${(scored.impulse.retention_raw * 100).toFixed(2)}%</b>`));
    }
    if (actual.tracking_scope === "POST_BREAKOUT") {
      assert(removedScore);
      continue;
    }
    for (const [key, label] of [["impulse", "推进成果"], ["contraction_quality", "优质收缩序列"]]) {
      const value = actual.score_components?.[key];
      if (value != null) assert(card(label).includes(`<b>${Number(value).toFixed(2)}</b>`));
    }
    const details = actual.structure_score_details;
    if (details?.status === "COMPLETE" && Array.isArray(details.extension_score_adjustments)
        && details.terminal_micro?.status === "COMPLETE") {
      const displayed = Number(card("确认型重置").match(/<b>(.*?)<\/b>/)[1])
        + Number(card("末端承接").match(/<b>(.*?)<\/b>/)[1]);
      assert(Math.abs(displayed - actual.score_components.contraction_extensions) < 0.011);
      assert.equal(card("扩展收缩"), undefined);
    }
    assert(!detail.innerHTML.includes("NaN"));
  }
  console.log(`Published VCP packet: ${data.candidates.length} details passed.`);
}
console.log("VCP score renderer: new components, zero, legacy, missing data and frozen score passed.");
