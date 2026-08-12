#!/usr/bin/env python3
"""Publish Model 2 setup triggers and next-day plans for the dashboard."""
from __future__ import annotations
import argparse, csv, json, math, os, re, sqlite3, sys
from datetime import datetime
from pathlib import Path
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config
from scripts.capital_observer import classify_stock_capital
from scripts.data.capital_data_service import CapitalDataService
from scripts.data.capital_data_sources import MiaoxiangCapitalSource, RequestBudget
from scripts.plan_realization import realized_events_for_date
from scripts.dashboard_index import update_dashboard_module

ROOT=Path(PROJECT_ROOT); RUNS=ROOT/"cache"/"quant_runs"; PLAN_RUNS=ROOT/"signal_plan"; POOL_DIR=ROOT/"pool"; SIGNAL_FIN_DIR=ROOT/"cache"/"signal_fundamentals"; MARKET_DB=ROOT/"cache"/"market_data"/"market_data.sqlite"; MARKET_DIR=ROOT/"market"; MARKET_CONTEXT_DIR=MARKET_DIR/"data"; OUT=ROOT/"dashboard"/"data"; START="260506"
PLAN_CONFIG,_=load_strategy_config("04-signal-plan.json"); POSITION_CFG=PLAN_CONFIG["position_guidance"]
FIELDS=("code","name","structure_stage","setup_signal","action_hint","suggested_position","setup_pattern_score","setup_score","setup_quality","setup_structure_score","setup_structure_anchor_date","setup_structure_base","setup_action_score","setup_current_action_score","setup_breakout_action_score","setup_score_components","setup_reasons","setup_misses","setup_risk_flags","structure_score","structure_risk_score","structure_risk_flags","close","MA20","MA60","pivot_price","structure_pivot","support_price","invalid_price","breakout_level","last_contraction_low","pivot_distance","distance_ma20","volume","vol_ma5","vol_ma20","volume_dry_up","vol_ratio","volume_pattern","chg_5","chg_20","setup_plan_inputs","reason")
MARKET_ADVICE={
 "OFFENSIVE":("广泛参与","supportive","趋势与广度支持更广泛的板块机会，但仍须等待个股量价条件成立并遵守失效位。"),
 "SELECTIVE":("聚焦强势","selective","市场机会偏结构化，只对转强和主线板块计算条件仓位，避免只凭 VCP 形态执行。"),
 "RECOVERY_WATCH":("只选主线","caution","市场处于修复期，只有主线板块保留低仓条件预案，其余板块继续观察。"),
 "CONSOLIDATING":("暂停参与","caution","市场方向尚未明确，所有板块条件仓位归零，VCP 只用于建立观察顺序。"),
 "DEFENSIVE":("防守观望","blocked","市场处于弱势环境，所有板块条件仓位归零，等待波动、广度和趋势修复。"),
}
MARKET_LABEL_CODES={"趋势扩散":"OFFENSIVE","结构性强势":"OFFENSIVE","结构行情":"SELECTIVE","结构分化":"SELECTIVE","修复期":"RECOVERY_WATCH","修复观察":"RECOVERY_WATCH","弱势震荡":"CONSOLIDATING","弱势收敛":"CONSOLIDATING","防御期":"DEFENSIVE","弱势下行":"DEFENSIVE"}
SOURCE_LABELS={
 "CORE_QUALITY":("核心质量池","core"),
 "EXPANSION_RS":("RS扩展池","expansion"),
 "BOTH":("双通道","both"),
}
POOL_RISK_LABELS={
 "LOW_ROE":"低 ROE",
 "WEAK_ROE":"ROE 偏弱",
 "WEAK_CASHFLOW":"现金流偏弱",
 "SEMI_CASHFLOW_RELAX":"现金流门槛放宽",
 "HIGH_DEBT_EDGE":"负债率偏高",
 "HIGH_VALUATION":"估值偏高",
}
def stamp(v):
 d=re.sub(r"\D","",v); return d[2:] if len(d)==8 else d
def published_dates(kind):
 return sorted(path.stem.rsplit("_",1)[-1] for path in OUT.glob(f"*/{kind}_context_*.js"))
def load_dashboard_index():
 path=OUT/"index.js"
 if not path.exists(): return {}
 match=re.search(r"=\s*(\{.*\});\s*$",path.read_text(encoding="utf-8"),re.S)
 return json.loads(match.group(1)) if match else {}
def write_dashboard_index():
 for kind in ("signals","vcp"):
  dates=published_dates(kind)
  if dates: update_dashboard_module(OUT,kind,dates,defaults=("market",))
def format_plan_volume(value, direction):
 if value is None: return "—"
 suffix="以下" if direction=="max" else "以上"
 return f"{float(value)/10000:.2f}万手{suffix}"
def market_notice(date):
 path=MARKET_CONTEXT_DIR/f"market_context_{date}.json"
 fallback={"state":"UNKNOWN","label":"市场状态待确认","tag":"环境待确认","tone":"caution","advice":"未找到有效的同日市场状态，信号仅按量价事实展示，执行前请先核对市场环境。","risk_tags":[],"source":None}
 if not path.exists(): return fallback
 try: state=json.loads(path.read_text(encoding="utf-8")).get("market_state") or {}
 except (OSError,json.JSONDecodeError): return fallback
 confirmed=str(state.get("confirmed_state") or MARKET_LABEL_CODES.get(str(state.get("label") or "").strip()) or "").upper()
 if confirmed not in MARKET_ADVICE: return {**fallback,"source":path.name}
 tag,tone,advice=MARKET_ADVICE[confirmed]
 return {"state":confirmed,"candidate_state":str(state.get("candidate_state") or state.get("raw_label") or "").upper() or None,"label":state.get("label") or confirmed,"tag":tag,"tone":tone,"advice":advice,"risk_tags":state.get("risk_tags") or [],"source":path.name}
def sector_notices(date):
 stock_path=MARKET_DIR/f"stock_strength_{date}.csv"; sector_path=MARKET_DIR/f"sector_heat_{date}.csv"
 if not stock_path.exists() or not sector_path.exists(): return {}
 sectors={}
 with sector_path.open(encoding="utf-8-sig",newline="") as handle:
  for row in csv.DictReader(handle):
   if row.get("block_type")=="industry_sw_l2": sectors[row.get("block_name","")]=row
 notices={}
 with stock_path.open(encoding="utf-8-sig",newline="") as handle:
  for row in csv.DictReader(handle):
   code=pool_code(row.get("code")); name=str(row.get("sw_l2_name") or "").strip(); sector=sectors.get(name,{})
   if not code: continue
   phase=str(sector.get("sector_phase") or "").strip()
   notices[code]={"sector_name":name or "板块待确认","sector_state":str(sector.get("sector_state") or "").strip() or "状态待确认","sector_phase":phase or None,"sector_health":str(sector.get("sector_health") or "").strip() or "数据不足","sector_health_level":pool_number(sector.get("sector_health_level")),"sector_health_score":pool_number(sector.get("sector_health_score")),"sector_policy_tier":str(sector.get("sector_policy_tier") or "").strip() or None,"sector_data_status":str(sector.get("data_status") or "").strip() or None,"sector_rank_20":pool_number(sector.get("rank_20")),"sector_history_basis":sector.get("history_basis") or None}
 return notices
def default_sector_notice():
 return {"sector_name":"板块待确认","sector_state":"状态待确认","sector_phase":None,"sector_health":"数据不足","sector_health_level":None,"sector_health_score":None,"sector_policy_tier":None,"sector_data_status":None,"sector_rank_20":None,"sector_history_basis":None}
def position_range(setup_signal,quality,factor):
 base=(POSITION_CFG["base_position_pct"].get(setup_signal) or {}).get(quality)
 if not base: return None,None,None
 step=float(POSITION_CFG["rounding_step_pct"])
 adjusted=[math.floor(float(value)*float(factor)/step+1e-9)*step for value in base]
 if adjusted[1]<step: adjusted=[0.0,0.0]
 return list(base),adjusted,float(factor)
def position_range_text(values):
 if not values or values[1]<=0: return "观察"
 low,high=(int(value) if float(value).is_integer() else value for value in values)
 if low<=0: return f"≤{high}%"
 if low==high: return f"{high}%"
 return f"{low}%-{high}%"
def position_guidance(row,market,sector):
 state=market.get("state") or "UNKNOWN"; phase=sector.get("sector_phase")
 ready=sector.get("sector_data_status") in {None,"READY"} and phase in {"NONE","转强","主线","退潮"}
 factor=float((POSITION_CFG["phase_factors"].get(state) or {}).get(phase,POSITION_CFG["unknown_environment_factor"])) if ready else 0.0
 phase_label="观察" if phase=="NONE" else phase or "板块待确认"
 common={"market_state":state,"sector_phase":phase,"environment_factor":factor,"position_strategy_version":PLAN_CONFIG["strategy_version"]}
 signal=row.get("setup_signal")
 if row.get("signal_kind")=="PLAN":
  base_a,adjusted_a,_=position_range(signal,"A",factor); base_b,adjusted_b,_=position_range(signal,"B",factor)
  if state in {"CONSOLIDATING","DEFENSIVE"}: status,advice,reason="OBSERVE_MARKET","观察（市场弱势）","弱势收敛或弱势下行不配置仓位"
  elif state not in POSITION_CFG["phase_factors"]: status,advice,reason="OBSERVE_MARKET","观察（市场待确认）","缺少同日有效市场确认状态"
  elif not ready: status,advice,reason="OBSERVE_SECTOR","观察（板块待确认）","缺少同日有效板块阶段"
  elif factor<=0: status,advice,reason="OBSERVE_SECTOR",f"观察（{phase_label}）",f"当前市场阶段不开放{phase_label}板块仓位"
  else: status,advice,reason="PLAN_CONDITIONAL",f"A {position_range_text(adjusted_a)} / B {position_range_text(adjusted_b)}","实际触发后按触发日买点等级与环境重算"
  return {**common,"position_status":status,"position_advice":advice,"position_reason":reason,"base_position_a":base_a,"base_position_b":base_b,"plan_position_a":adjusted_a,"plan_position_b":adjusted_b,"base_position":None,"adjusted_position":None}
 quality=str(row.get("setup_quality") or "")
 base,adjusted,_=position_range(signal,quality,factor)
 if quality not in POSITION_CFG["eligible_setup_qualities"]: status,advice,reason="OBSERVE_QUALITY",f"观察（{quality or '未评级'}级）","仅 A/B 级买点进入仓位计算"
 elif state in {"CONSOLIDATING","DEFENSIVE"}: status,advice,reason="OBSERVE_MARKET","观察（市场弱势）","弱势收敛或弱势下行不配置仓位"
 elif state not in POSITION_CFG["phase_factors"]: status,advice,reason="OBSERVE_MARKET","观察（市场待确认）","缺少同日有效市场确认状态"
 elif not ready: status,advice,reason="OBSERVE_SECTOR","观察（板块待确认）","缺少同日有效板块阶段"
 elif factor<=0: status,advice,reason="OBSERVE_SECTOR",f"观察（{phase_label}）",f"当前市场阶段不开放{phase_label}板块仓位"
 elif not base: status,advice,reason="OBSERVE_QUALITY","观察（仓位规则缺失）","买点类型与等级未匹配基础仓位"
 else: status,advice,reason="ACTIONABLE",position_range_text(adjusted),f"基础{position_range_text(base)} × 环境{int(round(factor*100))}%"
 return {**common,"position_status":status,"position_advice":advice,"position_reason":reason,"base_position":base,"adjusted_position":adjusted,"base_position_a":None,"base_position_b":None,"plan_position_a":None,"plan_position_b":None}
def pool_code(value):
 match=re.search(r"\d{6}",str(value or ""))
 return match.group(0) if match else ""
def pool_number(value):
 try:
  number=float(str(value).replace(",","").strip())
  return number if number==number else None
 except (TypeError,ValueError): return None
def financial_notice(row):
 status=str(row.get("fundamental_status") or "").strip()
 if status!="CORE_VERIFIED":
  return {"financial_status":"FUNDAMENTAL_UNVERIFIED","financial_tone":"unverified","financial_tags":["财务未查询"],"financial_report_period":None,"financial_source":None}
 tags=[]
 checks=(
  ("归母净利润_元",lambda v:v<0,"净利润为负"),
  ("资产负债率_pct",lambda v:v>=70,"高负债"),
  ("经营现金流_元",lambda v:v<0,"经营现金流为负"),
  ("营收同比增速_pct",lambda v:v<=-20,"营收明显下滑"),
  ("净利润同比增速_pct",lambda v:v<=-30,"利润明显下滑"),
  ("毛利率_pct",lambda v:v<=0,"毛利率为负"),
  ("毛利率_pct",lambda v:0<v<10,"毛利率偏低"),
 )
 for field,predicate,label in checks:
  value=pool_number(row.get(field))
  if value is not None and predicate(value) and label not in tags: tags.append(label)
 for raw_tag in str(row.get("风险标签") or "").split(";"):
  label=POOL_RISK_LABELS.get(raw_tag.strip())
  if label and label not in tags: tags.append(label)
 return {"financial_status":"CORE_VERIFIED","financial_tone":"risk" if tags else "verified","financial_tags":tags or ["财务硬筛通过"],"financial_report_period":row.get("数据周期") or None,"financial_source":"模型一同日财务筛选"}
def pool_notices(date):
 path=POOL_DIR/f"pool_{date}.csv"; notices={}
 if not path.exists(): return notices
 with path.open(encoding="utf-8-sig",newline="") as handle:
  for row in csv.DictReader(handle):
   code=pool_code(row.get("股票代码"))
   if not code: continue
   channel=str(row.get("pool_channel") or "").strip()
   label,tone=SOURCE_LABELS.get(channel,("来源待确认","unknown"))
   notices[code]={"pool_channel":channel or "UNKNOWN","source_label":label,"source_tone":tone,**financial_notice(row)}
 return notices
def missing_pool_notice():
 return {"pool_channel":"UNKNOWN","source_label":"来源待确认","source_tone":"unknown","financial_status":"FUNDAMENTAL_UNVERIFIED","financial_tone":"unverified","financial_tags":["财务未查询"],"financial_report_period":None,"financial_source":None}
def capital_notice(rows):
 result=classify_stock_capital({"amount":None,"amount_ratio_20d":None},rows)
 valid_main=[item for item in rows if item.get("main_net_inflow") is not None]
 main_available=result["main_order_state"]!="INSUFFICIENT"; margin_available=result["margin_state"]!="INSUFFICIENT"
 return {
  "status":"complete" if main_available and margin_available else "partial" if main_available else "unavailable",
  "main_order_state":result["main_order_state"],
  "main_net_inflow_ratio":result["main_net_inflow_ratio"],
  "main_positive_days_3d":sum(float(item["main_net_inflow"])>0 for item in valid_main[-3:]),
  "main_observation_days_3d":len(valid_main[-3:]),
  "main_data_date":result["main_data_date"],
  "margin_state":result["margin_state"],
  "financing_balance_change":result["financing_balance_change"],
  "financing_net_buy":result["financing_net_buy"],
  "margin_data_date":result["margin_data_date"],
  "source":"capital_data.sqlite",
 }
def missing_capital_notice():
 return capital_notice([])
def capital_notices(date,codes,fetch=False,maximum_requests=None):
 cfg=PLAN_CONFIG["capital_support"]
 requested=int(maximum_requests if maximum_requests is not None else cfg["default_maximum_requests_per_publish"])
 hard=int(cfg["hard_maximum_requests_per_publish"])
 if requested<0 or requested>hard: raise ValueError(f"max-mx-requests 必须在 0 到 {hard} 之间")
 budget=RequestBudget(requested if fetch else 0)
 service=CapitalDataService(capital_source=MiaoxiangCapitalSource(request_budget=budget))
 target=datetime.strptime(date,"%y%m%d").strftime("%Y-%m-%d")
 with sqlite3.connect(str(service.market_db_path)) as conn:
  dates=[row[0] for row in conn.execute(
   "SELECT DISTINCT trade_date FROM daily_bars WHERE trade_date<=? ORDER BY trade_date DESC LIMIT ?",
   (target,int(cfg["lookback_trade_days"])),
  )][::-1]
 if not dates:
  return ({code:missing_capital_notice() for code in dict.fromkeys(codes)},budget.used,[{"error":"缺少交易日行情"}])
 result=service.fetch_stock_capital(list(dict.fromkeys(codes)),dates[0],dates[-1])
 notices={code:capital_notice(item.get("rows",[])) for code,item in result.get("stocks",{}).items()}
 return notices,budget.used,result.get("errors",[])
def signal_financial_notice(date,code):
 path=SIGNAL_FIN_DIR/f"{code}.json"
 if not path.exists(): return None
 try: payload=json.loads(path.read_text(encoding="utf-8"))
 except (OSError,json.JSONDecodeError): return None
 iso=datetime.strptime(date,"%y%m%d").strftime("%Y-%m-%d")
 snapshots=[item for item in payload.get("snapshots",[]) if item.get("available_from","9999")<=iso]
 if snapshots:
  item=max(snapshots,key=lambda value:value.get("report_end","")); tags=item.get("risk_tags") or []
  return {"financial_status":"SIGNAL_VERIFIED","financial_tone":"risk" if tags else "verified","financial_tags":tags or ["暂无明显财务风险"],"financial_report_period":item.get("label"),"financial_source":item.get("source") or "信号财务缓存"}
 attempt=payload.get("last_attempt") or {}
 if attempt.get("as_of_date")!=iso: return None
 status=attempt.get("status")
 label={"failed":"财务查询失败","insufficient":"财务数据不足"}.get(status)
 return {"financial_status":str(status or "FUNDAMENTAL_UNVERIFIED").upper(),"financial_tone":"unverified","financial_tags":[label],"financial_report_period":None,"financial_source":"信号财务缓存"} if label else None
def plan_row(plan, quant):
 row={key:quant.get(key) for key in FIELDS}
 row.update({"signal_kind":"PLAN","setup_signal":plan.get("setup_signal"),"action_hint":"次日计划","suggested_position":"触发后按模型二质量判定","setup_pattern_score":None,"setup_score":None,"setup_quality":plan.get("target_quality","—"),"setup_reasons":[plan.get("plan_reason","")],"setup_misses":[plan.get("risk_note","")],"setup_risk_flags":plan.get("structure_risk_flags",[]),"plan_action":plan.get("plan_action"),"plan_type":plan.get("plan_type"),"plan_priority":plan.get("plan_priority"),"plan_reason":plan.get("plan_reason"),"llm_note":plan.get("llm_note"),"trigger_price_low":plan.get("trigger_price_low"),"trigger_price_high":plan.get("trigger_price_high"),"ideal_price_low":plan.get("ideal_price_low"),"ideal_price_high":plan.get("ideal_price_high"),"plan_volume_text":format_plan_volume(plan.get("volume_max"),"max") if plan.get("volume_max") is not None else format_plan_volume(plan.get("volume_min"),"min"),"ideal_volume_text":format_plan_volume(plan.get("ideal_volume_max"),"max") if plan.get("ideal_volume_max") is not None else format_plan_volume(plan.get("ideal_volume_min"),"min"),"invalid_price":plan.get("invalid_price"),"plan_inputs":plan})
 return row
def build(date,fetch_capital=False,max_mx_requests=None):
 path=RUNS/f"quant_{date}.json"
 if not path.exists(): raise FileNotFoundError(path.name)
 raw=json.loads(path.read_text(encoding="utf-8")); rows=[]; quant_by_code={str(item.get("code","")).zfill(6):item for item in raw.get("results",[])}
 for item in raw.get("results",[]):
  plans=item.get("setup_plan_inputs") or {}
  triggered=item.get("setup_signal") not in (None,"","NONE")
  base={key:item.get(key) for key in FIELDS}
  if triggered:
   row=dict(base); row["signal_kind"]="TRIGGERED"; row["signal_source"]="MODEL2"; row["plan_inputs"]=plans.get(item.get("setup_signal","").replace("_BUY","").lower(),{}); rows.append(row)
 realized=realized_events_for_date(date,PLAN_RUNS,RUNS,MARKET_DB)
 triggered_by_key={(str(row.get("code","")).zfill(6),row.get("setup_signal")):row for row in rows}
 plan_hits=0
 for event in realized:
  code=str(event.get("code","")).zfill(6); key=(code,event.get("setup_type")); existing=triggered_by_key.get(key)
  if existing is not None:
   existing.update({"signal_source":"MODEL2_AND_PLAN","previous_plan_hit":True,"previous_plan_source_date":event.get("plan_date"),"previous_plan_action":event.get("entry_action")})
   plan_hits+=1
 plan_path=PLAN_RUNS/f"signal_plan_{date}.json"
 plan_payload=json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
 for plan in plan_payload.get("plans",[]):
  code=str(plan.get("code","")).zfill(6)
  if code in quant_by_code: rows.append(plan_row(plan,quant_by_code[code]))
 notices=pool_notices(date); market=market_notice(date); sectors=sector_notices(date)
 capital,capital_requests,capital_errors=capital_notices(date,[str(row.get("code","")).zfill(6) for row in rows],fetch_capital,max_mx_requests)
 for row in rows:
  code=str(row.get("code","")).zfill(6); notice=dict(notices.get(code,missing_pool_notice()))
  if notice.get("financial_status")!="CORE_VERIFIED": notice.update(signal_financial_notice(date,code) or {})
  sector=dict(sectors.get(code,default_sector_notice())); row.update(notice); row.update(sector); row["capital_support"]=capital.get(code,missing_capital_notice()); row.update(position_guidance(row,market,sector))
 rows.sort(key=lambda r:(r["signal_kind"]!="TRIGGERED", -(r.get("setup_score") or 0), -(r.get("structure_score") or 0), r["code"], r["setup_signal"]))
 return {"meta":{"run_date":raw.get("meta",{}).get("run_date",f"20{date[:2]}-{date[2:4]}-{date[4:]}") ,"source":path.name,"plan_source":plan_path.name if plan_path.exists() else None,"position_strategy_version":PLAN_CONFIG["strategy_version"],"capital_fetch_enabled":fetch_capital,"capital_requests_used":capital_requests,"capital_errors":capital_errors},"market_notice":market,"summary":{"triggered":sum(r["signal_kind"]=="TRIGGERED" for r in rows),"plan_hits":plan_hits,"planned":sum(r["signal_kind"]=="PLAN" for r in rows),"total":len(rows)},"signals":rows}
def publish(date,fetch_capital=False,max_mx_requests=None):
 data=build(date,fetch_capital,max_mx_requests); folder=OUT/f"20{date[:4]}"; folder.mkdir(parents=True,exist_ok=True); target=folder/f"signals_context_{date}.js"
 target.write_text("window.QUANT_DASHBOARD_SIGNALS_CONTEXTS = window.QUANT_DASHBOARD_SIGNALS_CONTEXTS || {};\n"+f"window.QUANT_DASHBOARD_SIGNALS_CONTEXTS[{json.dumps(date)}] = "+json.dumps(data,ensure_ascii=False)+";\n",encoding="utf-8"); write_dashboard_index(); return target
def main():
 p=argparse.ArgumentParser();p.add_argument("--date");p.add_argument("--all",action="store_true");p.add_argument("--fetch-capital",action="store_true");p.add_argument("--max-mx-requests",type=int);a=p.parse_args()
 dates=sorted(x.stem.rsplit("_",1)[-1] for x in RUNS.glob("quant_*.json") if x.stem.rsplit("_",1)[-1]>=START) if a.all else [stamp(a.date) if a.date else sorted(RUNS.glob("quant_*.json"))[-1].stem.rsplit("_",1)[-1]]
 print(json.dumps({"outputs":[str(publish(d,a.fetch_capital,a.max_mx_requests)) for d in dates]},ensure_ascii=False))
if __name__=="__main__": main()
