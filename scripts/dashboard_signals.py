#!/usr/bin/env python3
"""Publish Model 2 setup triggers and next-day plans for the dashboard."""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.shared import PROJECT_ROOT

ROOT=Path(PROJECT_ROOT); RUNS=ROOT/"cache"/"quant_runs"; PLAN_RUNS=ROOT/"signal_plan"; MARKET_CONTEXT_DIR=ROOT/"market"/"data"; OUT=ROOT/"dashboard"/"data"; START="260709"
FIELDS=("code","name","structure_stage","setup_signal","action_hint","suggested_position","setup_pattern_score","setup_score","setup_quality","setup_reasons","setup_misses","setup_risk_flags","structure_score","structure_risk_score","structure_risk_flags","close","MA20","MA60","pivot_price","structure_pivot","support_price","invalid_price","breakout_level","last_contraction_low","pivot_distance","distance_ma20","volume","vol_ma5","vol_ma20","volume_dry_up","vol_ratio","volume_pattern","chg_5","chg_20","setup_plan_inputs","reason")
MARKET_ADVICE={
 "OFFENSIVE":("环境支持","supportive","市场趋势与广度支持信号验证，但仍须等待个股量价条件成立并遵守失效位。"),
 "SELECTIVE":("结合板块确认","selective","市场机会偏结构化，优先确认标的所属板块强度与个股量价条件，避免只凭 VCP 形态执行。"),
 "RECOVERY_WATCH":("谨慎试错","caution","市场处于修复观察期，信号可跟踪但确认度有限，等待趋势、广度与个股量价继续改善。"),
 "CONSOLIDATING":("等待趋势确认","caution","市场方向尚未明确，VCP 主要用于建立观察顺序，等待指数趋势与个股触发条件共同确认。"),
 "DEFENSIVE":("市场仅观察","blocked","市场处于弱势环境，VCP 以结构发现和观察为主；即使量价触发，也优先等待波动、广度和趋势修复确认。"),
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
 index=load_dashboard_index()
 for kind in ("signals","vcp"):
  dates=published_dates(kind)
  if dates: index[kind]={"latest":dates[-1],"available":dates}
 index.setdefault("market",{"latest":None,"available":[]})
 (OUT/"index.js").write_text("window.QUANT_DASHBOARD_INDEX = "+json.dumps(index,ensure_ascii=False)+";\n",encoding="utf-8")
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
 raw=str(state.get("raw_label") or "").upper()
 if raw not in MARKET_ADVICE: return {**fallback,"source":path.name}
 tag,tone,advice=MARKET_ADVICE[raw]
 return {"state":raw,"label":state.get("label") or raw,"tag":tag,"tone":tone,"advice":advice,"risk_tags":state.get("risk_tags") or [],"source":path.name}
def plan_row(plan, quant):
 row={key:quant.get(key) for key in FIELDS}
 row.update({"signal_kind":"PLAN","setup_signal":plan.get("setup_signal"),"action_hint":"次日计划","suggested_position":"触发后按模型二质量判定","setup_pattern_score":None,"setup_score":None,"setup_quality":plan.get("target_quality","—"),"setup_reasons":[plan.get("plan_reason","")],"setup_misses":[plan.get("risk_note","")],"setup_risk_flags":plan.get("structure_risk_flags",[]),"plan_action":plan.get("plan_action"),"plan_type":plan.get("plan_type"),"plan_priority":plan.get("plan_priority"),"plan_reason":plan.get("plan_reason"),"llm_note":plan.get("llm_note"),"trigger_price_low":plan.get("trigger_price_low"),"trigger_price_high":plan.get("trigger_price_high"),"ideal_price_low":plan.get("ideal_price_low"),"ideal_price_high":plan.get("ideal_price_high"),"plan_volume_text":format_plan_volume(plan.get("volume_max"),"max") if plan.get("volume_max") is not None else format_plan_volume(plan.get("volume_min"),"min"),"ideal_volume_text":format_plan_volume(plan.get("ideal_volume_max"),"max") if plan.get("ideal_volume_max") is not None else format_plan_volume(plan.get("ideal_volume_min"),"min"),"invalid_price":plan.get("invalid_price"),"plan_inputs":plan})
 return row
def build(date):
 path=RUNS/f"quant_{date}.json"
 if not path.exists(): raise FileNotFoundError(path.name)
 raw=json.loads(path.read_text(encoding="utf-8")); rows=[]; quant_by_code={str(item.get("code","")).zfill(6):item for item in raw.get("results",[])}
 for item in raw.get("results",[]):
  plans=item.get("setup_plan_inputs") or {}
  triggered=item.get("setup_signal") not in (None,"","NONE")
  base={key:item.get(key) for key in FIELDS}
  if triggered:
   row=dict(base); row["signal_kind"]="TRIGGERED"; row["plan_inputs"]=plans.get(item.get("setup_signal","").replace("_BUY","").lower(),{}); rows.append(row)
 plan_path=PLAN_RUNS/f"signal_plan_{date}.json"
 plan_payload=json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.exists() else {}
 for plan in plan_payload.get("plans",[]):
  code=str(plan.get("code","")).zfill(6)
  if code in quant_by_code: rows.append(plan_row(plan,quant_by_code[code]))
 rows.sort(key=lambda r:(r["signal_kind"]!="TRIGGERED", -(r.get("setup_score") or 0), -(r.get("structure_score") or 0), r["code"], r["setup_signal"]))
 return {"meta":{"run_date":raw.get("meta",{}).get("run_date",f"20{date[:2]}-{date[2:4]}-{date[4:]}") ,"source":path.name,"plan_source":plan_path.name if plan_path.exists() else None},"market_notice":market_notice(date),"summary":{"triggered":sum(r["signal_kind"]=="TRIGGERED" for r in rows),"planned":sum(r["signal_kind"]=="PLAN" for r in rows),"total":len(rows)},"signals":rows}
def publish(date):
 data=build(date); folder=OUT/f"20{date[:4]}"; folder.mkdir(parents=True,exist_ok=True); target=folder/f"signals_context_{date}.js"
 target.write_text("window.QUANT_DASHBOARD_SIGNALS_CONTEXTS = window.QUANT_DASHBOARD_SIGNALS_CONTEXTS || {};\n"+f"window.QUANT_DASHBOARD_SIGNALS_CONTEXTS[{json.dumps(date)}] = "+json.dumps(data,ensure_ascii=False)+";\n",encoding="utf-8"); write_dashboard_index(); return target
def main():
 p=argparse.ArgumentParser();p.add_argument("--date");p.add_argument("--all",action="store_true");a=p.parse_args()
 dates=sorted(x.stem.rsplit("_",1)[-1] for x in RUNS.glob("quant_*.json") if x.stem.rsplit("_",1)[-1]>=START) if a.all else [stamp(a.date) if a.date else sorted(RUNS.glob("quant_*.json"))[-1].stem.rsplit("_",1)[-1]]
 print(json.dumps({"outputs":[str(publish(d)) for d in dates]},ensure_ascii=False))
if __name__=="__main__": main()
