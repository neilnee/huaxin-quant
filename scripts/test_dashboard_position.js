// Verify the actual signal detail renderer with new and historical data packets.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const source = fs.readFileSync(path.join(__dirname, "../dashboard/app.js"), "utf8");
const start = source.lastIndexOf("function renderSignalDetail(row){");
const end = source.indexOf("\nconst renderSignalDetailContent", start);
assert(start >= 0 && end > start);
const detail = {innerHTML: ""};
const context = {
  $: () => detail,
  signalsContext: {market_notice: {label: "防御期", tone: "caution"}},
  esc: value => String(value ?? ""),
  vcpNumber: (value, digits = 1) => value == null ? "—" : Number(value).toFixed(digits),
  vcpVolume: () => "—",
  signalRiskFlags: () => [],
  signalTypeLabel: value => value,
  signalPlanText: () => "量价条件",
  signalRiskLabel: value => value,
  priorBreakoutReasons: () => [],
  sectorHealthLevelText: row => String(row.sector_health_level),
  sectorHealthText: row => row.sector_health,
  capitalState: () => "待补",
  capitalPct: () => "—",
  capitalAmount: () => "—",
};
vm.createContext(context);
vm.runInContext(source.slice(start, end), context);
const row = {
  code: "000001", name: "测试", signal_kind: "TRIGGERED", setup_signal: "PULLBACK_BUY",
  setup_quality: "A", sector_phase: "NONE", sector_health_level: 1, sector_health: "温和改善",
  position_guidance_mode: "MARKET_RANGE_SECTOR_GATE", calculation_amount: 100000,
  market_position_range: [10, 20], sector_eligible: true, normal_maximum_symbols: 3,
  position_status: "ACTIONABLE", position_advice: "10%-20%（1—2万元）",
};
context.renderSignalDetail(row);
assert(detail.innerHTML.includes("单只计划额度 10.00万元"));
assert(detail.innerHTML.includes("市场配置 10%-20%"));
assert(detail.innerHTML.includes("板块资格 通过"));
assert(detail.innerHTML.includes("1—2万元"));
assert(!detail.innerHTML.includes("阶段系数"));
context.renderSignalDetail({...row, signal_kind: "PLAN", position_status: "PLAN_CONDITIONAL",
  market_position_range: [0, 80], position_advice: "触发后 ≤80%（≤8万元）"});
assert(detail.innerHTML.includes("市场配置 ≤80%"));
assert(detail.innerHTML.includes("条件仓位预案"));
context.renderSignalDetail({...row, position_status: "OBSERVE_SECTOR", sector_eligible: false,
  position_advice: "观察（弱势恶化）"});
assert(detail.innerHTML.includes("板块资格 未通过或待确认"));
assert(detail.innerHTML.includes("position-observe"));
context.renderSignalDetail({...row, position_guidance_mode: undefined, base_position: [20, 30],
  environment_factor: 0.5, position_advice: "10%-15%"});
assert(detail.innerHTML.includes("买点基础 20%-30%"));
assert(detail.innerHTML.includes("市场×板块阶段系数 50%"));
assert(!detail.innerHTML.includes("单只计划额度"));
if (process.argv[2]) {
  const packet = {window: {}};
  vm.createContext(packet);
  vm.runInContext(fs.readFileSync(process.argv[2], "utf8"), packet);
  const data = Object.values(packet.window.QUANT_DASHBOARD_SIGNALS_CONTEXTS)[0];
  context.signalsContext = data;
  for (const actualRow of data.signals) {
    context.renderSignalDetail(actualRow);
    assert(detail.innerHTML.includes(actualRow.position_advice));
    assert(detail.innerHTML.includes("单只计划额度"));
    assert(!detail.innerHTML.includes("市场×板块阶段系数"));
  }
  console.log(`Published packet: ${data.signals.length} signal details rendered.`);
}
console.log("Signal position renderer: new trigger, plan, blocked sector and legacy packet passed.");
