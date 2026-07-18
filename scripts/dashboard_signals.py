#!/usr/bin/env python3
"""Publish Model 2 setup triggers and next-day plans for the dashboard."""
from __future__ import annotations
import argparse, json, os, re, sys
from pathlib import Path
sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.shared import PROJECT_ROOT

ROOT=Path(PROJECT_ROOT); RUNS=ROOT/"cache"/"quant_runs"; OUT=ROOT/"dashboard"/"data"; START="260716"
FIELDS=("code","name","structure_stage","setup_signal","action_hint","suggested_position","setup_score","setup_quality","setup_reasons","setup_misses","setup_risk_flags","structure_score","structure_risk_score","structure_risk_flags","close","MA20","MA60","pivot_price","support_price","invalid_price","pivot_distance","distance_ma20","volume_dry_up","vol_ratio","chg_5","chg_20","setup_plan_inputs","reason")
def stamp(v):
 d=re.sub(r"\D","",v); return d[2:] if len(d)==8 else d
def build(date):
 path=RUNS/f"quant_{date}.json"
 if not path.exists(): raise FileNotFoundError(path.name)
 raw=json.loads(path.read_text(encoding="utf-8")); rows=[]
 for item in raw.get("results",[]):
  plans=item.get("setup_plan_inputs") or {}
  triggered=item.get("setup_signal") not in (None,"","NONE")
  planned=bool(plans) and item.get("structure_stage") in {"VCP_FORMING","VCP_MATURE","VCP_TIGHT"}
  if triggered or planned:
   row={key:item.get(key) for key in FIELDS}; row["signal_kind"]="TRIGGERED" if triggered else "PLAN"; rows.append(row)
 rows.sort(key=lambda r:(r["signal_kind"]!="TRIGGERED", -(r.get("setup_score") or 0), -(r.get("structure_score") or 0)))
 return {"meta":{"run_date":raw.get("meta",{}).get("run_date",f"20{date[:2]}-{date[2:4]}-{date[4:]}") ,"source":path.name},"summary":{"triggered":sum(r["signal_kind"]=="TRIGGERED" for r in rows),"planned":sum(r["signal_kind"]=="PLAN" for r in rows),"total":len(rows)},"signals":rows}
def publish(date):
 data=build(date); folder=OUT/f"20{date[:4]}"; folder.mkdir(parents=True,exist_ok=True); target=folder/f"signals_context_{date}.js"
 target.write_text("window.QUANT_DASHBOARD_SIGNALS_CONTEXTS = window.QUANT_DASHBOARD_SIGNALS_CONTEXTS || {};\n"+f"window.QUANT_DASHBOARD_SIGNALS_CONTEXTS[{json.dumps(date)}] = "+json.dumps(data,ensure_ascii=False)+";\n",encoding="utf-8"); return target
def main():
 p=argparse.ArgumentParser();p.add_argument("--date");p.add_argument("--all",action="store_true");a=p.parse_args()
 dates=sorted(x.stem.rsplit("_",1)[-1] for x in RUNS.glob("quant_*.json") if x.stem.rsplit("_",1)[-1]>=START) if a.all else [stamp(a.date) if a.date else sorted(RUNS.glob("quant_*.json"))[-1].stem.rsplit("_",1)[-1]]
 print(json.dumps({"outputs":[str(publish(d)) for d in dates]},ensure_ascii=False))
if __name__=="__main__": main()
