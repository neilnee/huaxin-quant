#!/usr/bin/env python3
"""Publish verified Model 3 v2 runs as a read-only dashboard data package."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(os.path.abspath(__file__)).parents[1]))
from scripts.shared import PROJECT_ROOT, VALUATION_INDEX_PATH


ROOT = Path(PROJECT_ROOT)
RUNS_DIR = ROOT / "cache" / "valuation_runs"
DATA_DIR = ROOT / "dashboard" / "data"
VALUATION_DATA_DIR = DATA_DIR / "valuation"
REPORT_DATA_DIR = VALUATION_DATA_DIR / "reports"
EVIDENCE_DATA_DIR = VALUATION_DATA_DIR / "evidence"


def normalize_code(value) -> str:
    digits = re.sub(r"\D", "", str(value or ""))
    return digits[-6:].zfill(6) if digits else ""


def usable_name(value, code: str) -> str:
    name = str(value or "").strip()
    if not name or normalize_code(name) == code or name.lower() in {"unknown", "none", "null", "未知"}:
        return ""
    return name


def valuation_name_index() -> dict[str, str]:
    path = Path(VALUATION_INDEX_PATH)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = csv.DictReader(handle)
        return {
            normalize_code(row.get("股票代码")): str(row.get("股票名称") or "").strip()
            for row in rows if normalize_code(row.get("股票代码"))
        }


NAME_INDEX = valuation_name_index()


def resolve_identity(path: Path, manifest: dict, params: dict | None = None, card: dict | None = None) -> tuple[str, str]:
    meta = (params or {}).get("meta", {})
    code = normalize_code(meta.get("code") or manifest.get("code") or path.name.split("_", 1)[0])
    candidates = [meta.get("name"), manifest.get("name"), (card or {}).get("company_name"), NAME_INDEX.get(code)]
    name = next((usable_name(value, code) for value in candidates if usable_name(value, code)), "")
    return code, name or code


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def stamp(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if len(digits) == 8:
        return digits[2:]
    if len(digits) == 6:
        return digits
    raise ValueError("日期应为 YYMMDD 或 YYYY-MM-DD")


def run_date(run_dir: Path, manifest: dict) -> str:
    started = str(manifest.get("started_at", ""))
    try:
        return datetime.fromisoformat(started).strftime("%y%m%d")
    except ValueError:
        match = re.search(r"_(\d{8})_", run_dir.name)
        if not match:
            raise ValueError(f"无法从运行包解析日期: {run_dir.name}")
        return match.group(1)[2:]


def verified_runs():
    runs = []
    for path in sorted(RUNS_DIR.glob("*_*")):
        manifest_path = path / "manifest.json"
        required = [manifest_path, path / "research_card.json", path / "calc_params.json", path / "calc_results.json"]
        if not all(item.exists() for item in required):
            continue
        try:
            manifest = load_json(manifest_path)
        except (OSError, json.JSONDecodeError):
            continue
        # A terminal run with a validated research card and calculation is
        # publishable even when consensus is explicitly insufficient.  The
        # status is part of the research conclusion and must remain visible.
        if manifest.get("status") != "done":
            continue
        try:
            card = load_json(path / "research_card.json")
        except (OSError, json.JSONDecodeError):
            continue
        required_card_keys = ("investment_thesis", "business_pillars", "narrative_options", "verification_nodes")
        if not all(card.get(key) for key in required_card_keys):
            continue
        runs.append({"path": path, "manifest": manifest, "date": run_date(path, manifest)})
    return runs


def per_share(value, total_shares):
    try:
        # calc_valuation v2 uses 亿元 for value and 亿股 for total_shares.
        shares = float(total_shares)
        return round(float(value) / shares, 2) if shares else None
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def scenario_per_share(total: dict, shares, current_price) -> dict:
    values = {key: per_share(total.get(key), shares) for key in ("pessimistic", "base", "optimistic")}
    try:
        values["base_upside_pct"] = round((values["base"] / float(current_price) - 1) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        values["base_upside_pct"] = None
    return values


def primary_pillar(result: dict) -> dict:
    rows = result.get("matrix_2026e", {}).get("matrix_rows", [])
    ranked = []
    for row in rows:
        try:
            ranked.append((float(row.get("pillar_total", {}).get("base")), row))
        except (TypeError, ValueError):
            continue
    if not ranked:
        return {}
    value, row = max(ranked, key=lambda pair: pair[0])
    return {"name": row.get("pillar_name"), "base_value": round(value, 2), "unit": "亿元市值"}


def legacy_stage_two_baseline(card: dict, params: dict, result: dict) -> dict:
    """Expose old completed runs as a clearly labelled Layer-1 compatibility model."""
    card_by_name = {str(item.get("name")): item for item in card.get("business_pillars", []) if item.get("name")}
    matrix_by_year = {
        "2026e": {str(row.get("pillar_name")): row for row in result.get("matrix_2026e", {}).get("matrix_rows", [])},
        "2027e": {str(row.get("pillar_name")): row for row in result.get("matrix_2027e", {}).get("matrix_rows", [])},
    }
    pillars, assumptions = [], []
    totals = {year: {scenario: 0.0 for scenario in ("pessimistic", "base", "optimistic")}
              for year in ("2026e", "2027e")}
    for index, calc in enumerate(params.get("pillars", []), 1):
        name = str(calc.get("name") or f"支柱{index}")
        card_pillar = card_by_name.get(name, {})
        pillar_id = str(card_pillar.get("research_id") or f"legacy_pillar_{index:02d}")
        years = {}
        for year in ("2026e", "2027e"):
            suffix = year
            profits = {
                "pessimistic": calc.get(f"consensus_np_lower_{suffix}"),
                "base": calc.get(f"consensus_np_{suffix}"),
                "optimistic": calc.get(f"consensus_np_upper_{suffix}"),
            }
            row = matrix_by_year[year].get(name, {})
            layer1 = row.get("layer1", {})
            year_row = {}
            for scenario in ("pessimistic", "base", "optimistic"):
                profit = profits[scenario]
                value = layer1.get(scenario)
                try:
                    profit_number, value_number = float(profit), float(value)
                    multiple = value_number / profit_number if profit_number else 0.0
                except (TypeError, ValueError, ZeroDivisionError):
                    profit_number, value_number, multiple = 0.0, float(value or 0), 0.0
                year_row[scenario] = {
                    "profit": round(profit_number, 4), "multiple": round(multiple, 4),
                    "probability": 1.0,
                    "valuation": round(value_number, 2),
                    "profit_reason": "由旧运行最终映射参数反推，非阶段二原生基准",
                    "multiple_reason": "由旧运行Layer 1估值/利润反推",
                    "probability_reason": "经营支柱按100%纳入",
                    "source_ids": card_pillar.get("source_ids", []),
                }
                totals[year][scenario] += value_number
            years[year] = year_row
        route = str(calc.get("route") or "")
        pillars.append({
            "pillar_id": pillar_id, "pillar_type": "operating", "pricing_stage": "institution_baseline",
            "name": name,
            "profit_metric": "net_profit（旧运行计算参数）",
            "valuation_method": f"路线 {route}" if route else "未标注",
            "multiple_name": "隐含倍数", "data_confidence": "legacy_derived",
            "estimation_method": "兼容旧运行：仅取Layer 1并反推利润与倍数",
            "source_ids": card_pillar.get("source_ids", []), "years": years,
        })
        bridge = card_pillar.get("profit_bridge") or {}
        drivers = card_pillar.get("valuation_drivers") or []
        for year in ("2026e", "2027e"):
            bridge_items = bridge.get(f"items_{year}", []) or []
            for item_index, bridge_item in enumerate(bridge_items, 1):
                impact = bridge_item.get("profit_impact")
                try:
                    is_priced = float(impact) != 0
                except (TypeError, ValueError):
                    is_priced = False
                assumptions.append({
                    "assumption_id": f"{pillar_id}_{year}_{item_index:02d}", "pillar_id": pillar_id,
                    "year": year, "metric": str(bridge_item.get("item") or "利润桥条件"),
                    "baseline_value": impact, "unit": "亿元利润影响",
                    "condition": str(bridge_item.get("item") or "待核对"), "information_cutoff": "旧运行研究日",
                    "source_ids": bridge_item.get("source_ids", []),
                    "included_in_baseline": is_priced, "pricing_channel": "profit" if is_priced else "none",
                    "included_in_model": is_priced, "pricing_stage": "institution_baseline" if is_priced else "not_priced",
                    "pricing_reason": ("旧运行利润桥存在非零利润贡献，兼容判断为已纳入基准利润"
                                       if is_priced else "旧运行仅记录方向或条件，没有非零利润贡献，未纳入基准估值"),
                    "quantification_status": "quantitative" if is_priced else "missing_input",
                })
        for driver_index, driver in enumerate(drivers, 1):
            assumptions.append({
                "assumption_id": f"{pillar_id}_driver_{driver_index:02d}", "pillar_id": pillar_id,
                "year": "2026e/2027e", "metric": "估值驱动", "baseline_value": None, "unit": "—",
                "condition": str(driver), "information_cutoff": "旧运行研究日",
                "source_ids": card_pillar.get("source_ids", []),
                "included_in_baseline": True, "pricing_channel": "multiple",
                "included_in_model": True, "pricing_stage": "institution_baseline",
                "pricing_reason": "旧运行将该项列为估值驱动，兼容判断为已纳入Layer 1倍数依据",
                "quantification_status": "qualitative",
            })
    return {
        "version": 3, "status": "legacy_derived", "source": "legacy_layer1_derived",
        "note": "旧运行未生成阶段二原生模型；本表仅由最终映射的Layer 1兼容推导，不包含后续Layer 2/3调整。",
        "pillars": pillars, "assumption_ledger": assumptions,
        "totals": {year: {scenario: round(value, 2) for scenario, value in rows.items()}
                   for year, rows in totals.items()},
    }


def merge_priced_independent_items(baseline: dict, card: dict, params: dict, pe_by_year: dict | None = None) -> dict:
    """Apply final PE research and add priced projects to the single model table."""
    model = json.loads(json.dumps(baseline, ensure_ascii=False))
    model.setdefault("pillars", [])
    model.setdefault("assumption_ledger", [])
    historical = [item for item in model["pillars"] if item.get("profit_basis") == "historical_realized"]
    if historical:
        historical_ids = {str(item.get("pillar_id")) for item in historical}
        model.setdefault("excluded_items", []).extend([
            {**item, "valuation_role": "historical_excluded",
             "exclusion_reason": "历史已实现事项不进入前瞻估值表"}
            for item in historical
        ])
        model["pillars"] = [item for item in model["pillars"] if str(item.get("pillar_id")) not in historical_ids]
        model["assumption_ledger"] = [
            item for item in model["assumption_ledger"] if str(item.get("pillar_id")) not in historical_ids
        ]
    scenario_pe_fields = {
        "pessimistic": "pessimistic_pe", "base": "final_pe", "optimistic": "optimistic_pe",
    }
    for pillar in model["pillars"]:
        if pillar.get("pillar_type") != "operating" or pillar.get("profit_basis") != "company_consensus":
            continue
        for year in ("2026e", "2027e"):
            pe_result = (pe_by_year or {}).get(year) or {}
            if not pe_result:
                continue
            for scenario, field in scenario_pe_fields.items():
                multiple = pe_result.get(field)
                cell = pillar.get("years", {}).get(year, {}).get(scenario, {})
                if multiple is None or not cell:
                    continue
                cell["multiple"] = round(float(multiple), 4)
                cell["valuation"] = round(float(cell.get("profit") or 0) * float(multiple) * float(cell.get("probability") or 0), 2)
                cell["multiple_reason"] = "采用阶段五同口径公司级PE研究结果；合并经营支柱不再沿用未经清洗的分部倍数"
        pillar["pricing_stage"] = "final_research"

    def name_key(value):
        return re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", str(value or "")).lower()

    options = [item for item in card.get("narrative_options", []) or []
               if isinstance(item, dict) and item.get("included_in_valuation") is not False]
    existing_names = [name_key(item.get("name")) for item in model["pillars"]]
    scenarios = ("pessimistic", "base", "optimistic")
    for parent_index, calc_pillar in enumerate(params.get("pillars", []) or [], 1):
        for project_index, project in enumerate(calc_pillar.get("type_b_pipeline", []) or [], 1):
            project_name = str(project.get("name") or f"独立事项{parent_index}-{project_index}")
            key = name_key(project_name)
            if any(key and existing and (key in existing or existing in key) for existing in existing_names):
                continue
            option = next((item for item in options if name_key(item.get("name")) == key), {})
            sources = option.get("source_ids") or []
            profit = float(project.get("project_profit") or 0)
            multiple = float(project.get("pe") or 0)
            base_probability = min(1.0, max(0.0, float(project.get("probability") or 0)))
            years = {}
            researched_scenarios = option.get("valuation_scenarios") if isinstance(option, dict) else None
            if isinstance(researched_scenarios, dict):
                for year in ("2026e", "2027e"):
                    years[year] = {}
                    for scenario in scenarios:
                        researched = (researched_scenarios.get(year, {}).get(scenario, {}) or {})
                        scenario_profit = float(researched.get("profit") or 0)
                        scenario_multiple = float(researched.get("multiple") or 0)
                        scenario_probability = min(1.0, max(0.0, float(researched.get("probability") or 0)))
                        years[year][scenario] = {
                            "profit": round(scenario_profit, 4), "multiple": round(scenario_multiple, 4),
                            "probability": round(scenario_probability, 4),
                            "valuation": round(scenario_profit * scenario_multiple * scenario_probability, 2),
                            "profit_reason": str(researched.get("profit_reason") or "独立事项的潜在收益，不并入持续经营利润"),
                            "multiple_reason": str(researched.get("multiple_reason") or "沿用已审核独立事项估值参数"),
                            "probability_reason": str(researched.get("probability_reason") or "概率来自项目当前客观进度"),
                            "source_ids": researched.get("source_ids") or sources,
                        }
                base_cell = years["2026e"]["base"]
                profit = float(base_cell["profit"])
                multiple = float(base_cell["multiple"])
                base_probability = float(base_cell["probability"])
            else:
                for year, center in (("2026e", base_probability), ("2027e", min(1.0, base_probability + 0.15))):
                    probabilities = {
                        "pessimistic": max(0.0, center - 0.15),
                        "base": center,
                        "optimistic": min(1.0, center + 0.15),
                    }
                    years[year] = {
                        scenario: {
                            "profit": round(profit, 4), "multiple": round(multiple, 4),
                            "probability": round(probabilities[scenario], 4),
                            "valuation": round(profit * multiple * probabilities[scenario], 2),
                            "profit_reason": "独立事项的潜在收益，不并入持续经营利润",
                            "multiple_reason": "沿用已审核独立事项估值参数",
                            "probability_reason": ("基准概率来自项目研究；悲观/乐观用于表达审批和交割结果差异"
                                                   if year == "2026e" else "随验证窗口推进后的项目概率"),
                            "source_ids": sources,
                        } for scenario in scenarios
                    }
            parent_name = str(calc_pillar.get("name") or "")
            target_pillar = next((item for item in model["pillars"] if name_key(item.get("name")) == name_key(parent_name)), None)
            parent_is_empty = target_pillar is not None and all(
                float((target_pillar.get("years", {}).get(year, {}).get(scenario, {}) or {}).get("valuation") or 0) == 0
                for year in ("2026e", "2027e") for scenario in scenarios
            )
            if parent_is_empty and len(calc_pillar.get("type_b_pipeline", []) or []) == 1:
                project_id = str(target_pillar["pillar_id"])
                target_pillar.update({
                    "pillar_type": "independent_event", "pricing_stage": "incremental_evidence",
                    "profit_metric": "project_profit", "valuation_method": "概率加权独立估值",
                    "multiple_name": "独立事项倍数", "data_confidence": "derived_from_final_mapping",
                    "estimation_method": "潜在收益 × 独立事项倍数 × 情景概率；不进入主营利润或主营PE",
                    "source_ids": list(dict.fromkeys((target_pillar.get("source_ids") or []) + sources)), "years": years,
                })
            else:
                project_id = str(option.get("research_id") or f"priced_event_{parent_index:02d}_{project_index:02d}")
                model["pillars"].append({
                    "pillar_id": project_id, "pillar_type": "independent_event", "pricing_stage": "incremental_evidence",
                    "name": project_name, "profit_metric": "project_profit", "valuation_method": "概率加权独立估值",
                    "multiple_name": "独立事项倍数", "data_confidence": "derived_from_final_mapping",
                    "estimation_method": "潜在收益 × 独立事项倍数 × 情景概率；不进入主营利润或主营PE",
                    "source_ids": sources, "years": years,
                })
            model["assumption_ledger"].extend([
                {
                    "assumption_id": f"{project_id}_profit", "pillar_id": project_id, "year": "2026e/2027e",
                    "metric": "潜在项目收益", "baseline_value": profit, "unit": "亿元",
                    "condition": str(option.get("business_essence") or project_name), "information_cutoff": "最终研究日",
                    "source_ids": sources, "included_in_baseline": False, "included_in_model": True,
                    "pricing_stage": "incremental_evidence", "pricing_channel": "standalone",
                    "pricing_reason": "作为独立事项加入统一估值模型，未混入主营利润或主营倍数",
                    "quantification_status": "quantitative",
                },
                {
                    "assumption_id": f"{project_id}_probability", "pillar_id": project_id, "year": "2026e",
                    "metric": "基准实现概率", "baseline_value": round(base_probability * 100, 1), "unit": "%",
                    "condition": "以当前审批、发行和资产交割进度为判断基础；情景概率见估值表",
                    "information_cutoff": "最终研究日", "source_ids": sources,
                    "included_in_baseline": False, "included_in_model": True,
                    "pricing_stage": "incremental_evidence", "pricing_channel": "standalone",
                    "pricing_reason": "概率直接参与独立事项估值", "quantification_status": "quantitative",
                },
            ])
            existing_names.append(key)
    model["totals"] = {
        year: {
            scenario: round(sum(float((pillar.get("years", {}).get(year, {}).get(scenario, {}) or {}).get("valuation") or 0)
                                for pillar in model["pillars"]), 2)
            for scenario in scenarios
        } for year in ("2026e", "2027e")
    }
    if any(item.get("pricing_stage") == "incremental_evidence" for item in model["pillars"]):
        model["source"] = "unified_valuation_model"
        model["note"] = "机构基准与后续已计价独立事项统一展示；每项仅保留一个估值入口。"
    return model


def report_run(item: dict) -> dict:
    path = item["path"]
    card, params, result = (load_json(path / name) for name in ("research_card.json", "calc_params.json", "calc_results.json"))
    meta = params.get("meta", {})
    code, name = resolve_identity(path, item["manifest"], params, card)
    baseline_path = path / "stage_2_baseline.json"
    stage2_baseline = load_json(baseline_path) if baseline_path.exists() else legacy_stage_two_baseline(card, params, result)
    model_path = path / "valuation_model.json"
    valuation_model = (load_json(model_path) if model_path.exists()
                       else merge_priced_independent_items(stage2_baseline, card, params, {
                           "2026e": result.get("pe_2026e", {}), "2027e": result.get("pe_2027e", {}),
                       }))
    total_2026 = valuation_model.get("totals", {}).get("2026e", {})
    total_2027 = valuation_model.get("totals", {}).get("2027e", {})
    shares = meta.get("total_shares")
    current_price = meta.get("current_price")
    years = {
        "2026e": scenario_per_share(total_2026, shares, current_price),
        "2027e": scenario_per_share(total_2027, shares, current_price),
    }
    valuation = {
        "current_price": meta.get("current_price"),
        "price_date": meta.get("price_date"),
        "price_source": meta.get("price_source"),
        "pessimistic": years["2026e"]["pessimistic"],
        "base": years["2026e"]["base"],
        "optimistic": years["2026e"]["optimistic"],
        "base_2027": years["2027e"]["base"],
        "years": years,
    }
    valuation["base_upside_pct"] = years["2026e"]["base_upside_pct"]
    evidence = load_json(path / "evidence.json").get("evidence", [])
    risks = card.get("risks", [])
    verification_nodes = card.get("verification_nodes", [])
    return {
        "code": code, "name": name,
        "run_id": path.name, "run_at": item["manifest"].get("started_at"), "analysis_date": item["date"],
        "valuation": valuation,
        "status": {"run": item["manifest"].get("status"), "consensus": item["manifest"].get("stages", {}).get("consensus", {}),
                   "reverse_check": result.get("reverse_check", {})},
        "pe_2026e": result.get("pe_2026e", {}),
        "matrix_2026e": result.get("matrix_2026e", {}), "matrix_2027e": result.get("matrix_2027e", {}),
        "pillars_2026e": result.get("pillars_2026e", []),
        "calc_pillars": params.get("pillars", []),
        "investment_thesis": card.get("investment_thesis", {}),
        "business_pillars": card.get("business_pillars", []),
        "market_divergences": card.get("market_divergences", []),
        "narrative_options": card.get("narrative_options", []),
        "verification_nodes": card.get("verification_nodes", []),
        "consensus": card.get("consensus", []), "comparables": card.get("comparables", []),
        "valuation_inputs": card.get("valuation_inputs", {}),
        "stage2_baseline": valuation_model,
        "facts": card.get("facts", []), "assumptions": card.get("assumptions", []),
        "risks": card.get("risks", []), "catalysts": card.get("catalysts", []),
        "decision_summary": {
            "primary_pillar": max(
                ({"name": pillar.get("name"),
                  "base_value": float((pillar.get("years", {}).get("2026e", {}).get("base", {}) or {}).get("valuation") or 0),
                  "unit": "亿元市值"} for pillar in valuation_model.get("pillars", [])),
                key=lambda row: row["base_value"], default=primary_pillar(result)),
            "primary_risk": risks[0] if risks else {},
            "next_verification": verification_nodes[0] if verification_nodes else {},
        },
        "research_stats": {
            "evidence_count": len(evidence),
            "consensus_count": len(card.get("consensus", [])),
            "pillar_count": len(valuation_model.get("pillars", [])),
            "divergence_count": len(card.get("market_divergences", [])),
            "option_count": len(card.get("narrative_options", [])),
            "verification_count": len(card.get("verification_nodes", [])),
        },
    }


def catalog_row(report: dict) -> dict:
    run_id = report["run_id"]
    return {
        "code": report["code"], "name": report["name"], "run_id": run_id,
        "run_at": report.get("run_at"), "analysis_date": report.get("analysis_date"),
        "valuation": report.get("valuation", {}), "status": report.get("status", {}),
        "research_stats": report.get("research_stats", {}),
        "decision_summary": report.get("decision_summary", {}),
        "report_path": f"data/valuation/reports/{run_id}.js",
        "evidence_path": f"data/valuation/evidence/{run_id}.js",
    }


def evidence_package(item: dict, report: dict) -> dict:
    evidence = load_json(item["path"] / "evidence.json").get("evidence", [])
    return {"run_id": report["run_id"], "code": report["code"], "evidence": evidence}


def write_report_packages(item: dict, report: dict) -> None:
    REPORT_DATA_DIR.mkdir(parents=True, exist_ok=True)
    EVIDENCE_DATA_DIR.mkdir(parents=True, exist_ok=True)
    run_id = report["run_id"]
    report_target = REPORT_DATA_DIR / f"{run_id}.js"
    evidence_target = EVIDENCE_DATA_DIR / f"{run_id}.js"
    report_target.write_text(
        "window.QUANT_DASHBOARD_VALUATION_REPORTS = window.QUANT_DASHBOARD_VALUATION_REPORTS || {};\n"
        f"window.QUANT_DASHBOARD_VALUATION_REPORTS[{json.dumps(run_id)}] = "
        + json.dumps(report, ensure_ascii=False) + ";\n", encoding="utf-8",
    )
    evidence_target.write_text(
        "window.QUANT_DASHBOARD_VALUATION_EVIDENCE = window.QUANT_DASHBOARD_VALUATION_EVIDENCE || {};\n"
        f"window.QUANT_DASHBOARD_VALUATION_EVIDENCE[{json.dumps(run_id)}] = "
        + json.dumps(evidence_package(item, report), ensure_ascii=False) + ";\n", encoding="utf-8",
    )


def load_index():
    path = DATA_DIR / "index.js"
    if not path.exists():
        return {}
    match = re.search(r"=\s*(\{.*\});\s*$", path.read_text(encoding="utf-8"), re.S)
    return json.loads(match.group(1)) if match else {}


def write_index():
    index = load_index()
    dates = sorted(item.stem.rsplit("_", 1)[-1] for item in DATA_DIR.glob("*/valuation_context_*.js"))
    index["valuation"] = {"latest": dates[-1] if dates else None, "available": dates, "catalog": "data/valuation/catalog.js"}
    for kind in ("market", "vcp", "signals"):
        index.setdefault(kind, {"latest": None, "available": []})
    (DATA_DIR / "index.js").write_text("window.QUANT_DASHBOARD_INDEX = " + json.dumps(index, ensure_ascii=False) + ";\n", encoding="utf-8")


def publish(date: str) -> Path:
    selected = [item for item in verified_runs() if item["date"] == date]
    newest_by_code = {}
    for item in selected:
        code = str(item["manifest"].get("code", ""))
        if code not in newest_by_code or item["path"].name > newest_by_code[code]["path"].name:
            newest_by_code[code] = item
    valuations = sorted((catalog_row(report_run(item)) for item in newest_by_code.values()), key=lambda row: (row["valuation"].get("base_upside_pct") is None, -(row["valuation"].get("base_upside_pct") or -9999), row["code"]))
    context = {"meta": {"run_date": f"20{date[:2]}-{date[2:4]}-{date[4:]}", "source": "verified valuation v2 runs"},
               "summary": {"total": len(valuations), "with_consensus": sum(row["status"]["consensus"].get("status") == "done" for row in valuations)},
               "valuations": valuations}
    folder = DATA_DIR / f"20{date[:4]}"; folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"valuation_context_{date}.js"
    target.write_text("window.QUANT_DASHBOARD_VALUATION_CONTEXTS = window.QUANT_DASHBOARD_VALUATION_CONTEXTS || {};\n" +
                      f"window.QUANT_DASHBOARD_VALUATION_CONTEXTS[{json.dumps(date)}] = " + json.dumps(context, ensure_ascii=False) + ";\n", encoding="utf-8")
    write_index()
    return target


def publish_latest() -> Path:
    """Publish one newest verified report per company across all analysis dates."""
    newest_by_code = {}
    for item in verified_runs():
        code, _ = resolve_identity(item["path"], item["manifest"])
        if code not in newest_by_code or item["path"].name > newest_by_code[code]["path"].name:
            newest_by_code[code] = item
    reports = []
    for item in newest_by_code.values():
        report = report_run(item)
        write_report_packages(item, report)
        reports.append(report)
    valuations = sorted((catalog_row(report) for report in reports),
                        key=lambda row: (row.get("run_at") or "", row["code"]), reverse=True)
    context = {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"), "source": "newest verified valuation run per company"},
        "summary": {
            "total": len(valuations),
            "with_consensus": sum(row["status"]["consensus"].get("status") == "done" for row in valuations),
            "evidence_count": sum(row["research_stats"]["evidence_count"] for row in valuations),
        },
        "valuations": valuations,
    }
    VALUATION_DATA_DIR.mkdir(parents=True, exist_ok=True)
    target = VALUATION_DATA_DIR / "catalog.js"
    target.write_text("window.QUANT_DASHBOARD_VALUATION_CATALOG = " + json.dumps(context, ensure_ascii=False) + ";\n", encoding="utf-8")
    compatibility_target = DATA_DIR / "valuation_latest.js"
    compatibility_target.write_text(
        "window.QUANT_DASHBOARD_VALUATION_LATEST = " + json.dumps(context, ensure_ascii=False) + ";\n",
        encoding="utf-8",
    )
    return target


def publish_progress() -> Path:
    """Publish in-flight valuation run checkpoints without touching verified contexts."""
    rows = []
    stage_schema = [
        ("stage0", "阶段零 · 数据准备", [("基础数据快照", "briefing.json", "evidence")]),
        ("stage1", "阶段一 · 业务与利润支柱", [("原文分批阅读", "stage_1_readings.json", "阶段一"), ("业务拆解与专题计划", "stage_1_business.json", "stage_1_business")]),
        ("stage2", "阶段二 · 一致预期与基准估值", [("研报分批阅读", "stage_2_readings.json", "阶段二"), ("机构全集与口径审计", "stage_2_institutions.json", "stage_2_consensus"), ("分支柱模型输入", "stage_2_model_inputs.json", "stage_2_consensus"), ("共识与估值锚", "stage_2_consensus.json", "stage_2_consensus"), ("双年三情景统一模型", "stage_2_baseline.json", "stage_2_baseline")]),
        ("stage3", "阶段三 · 项目与预期差", [("专题缓存与去重", "evidence.json", "dynamic_search"), ("专题原文分批阅读", "stage_3_readings.json", "阶段三"), ("项目估值归类", "stage_3_expectations.json", "stage_3_expectations")]),
        ("stage4", "阶段四 · 催化剂与验证", [("复用项目阅读结论", "stage_4_readings.json", None), ("验证节点与风险", "stage_4_catalysts.json", "stage_4_catalysts")]),
        ("stage5", "阶段五 · 研究卡与通用映射", [("五A1 · 主营与共识", "stage_5_research_core.json", "stage_5_research_core"), ("五A2 · 分歧与期权", "stage_5_research_options.json", "stage_5_research_options"), ("五A3 · 验证与事实", "stage_5_research_validation.json", "stage_5_research_validation"), ("五A · 研究结论合并", "stage_5_research.json", "stage_5_research"), ("五B1 · PE输入", "stage_5_mapping_valuation.json", "stage_5_mapping_valuation"), ("五B2 · 支柱利润映射", "stage_5_mapping_pillars.json", "stage_5_mapping_pillars"), ("五B3 · 分歧与期权映射", "stage_5_mapping_adjustments_v3.json", "stage_5_mapping_adjustments"), ("五B · 参数映射", "stage_5_mapping.json", "stage_5_mapping"), ("五C · 确定性组装", "research_card.json", "stage_5_assembly"), ("参数契约校验", "calc_params.json", "parameter_validation")]),
        ("calculation", "估值计算", [("三情景估值引擎", "calc_results.json", "calculation")]),
    ]
    newest_paths = {}
    for candidate in sorted(RUNS_DIR.glob("*_*"), reverse=True):
        if not (candidate / "manifest.json").exists():
            continue
        candidate_manifest = load_json(candidate / "manifest.json")
        candidate_code, _ = resolve_identity(candidate, candidate_manifest)
        if candidate_code and candidate_code not in newest_paths:
            newest_paths[candidate_code] = candidate
    for path in newest_paths.values():
        manifest = load_json(path / "manifest.json") if (path / "manifest.json").exists() else {}
        code, name = resolve_identity(path, manifest)
        manifest_stages = manifest.get("stages", {})
        stages = []
        for stage_id, label, step_specs in stage_schema:
            steps = []
            for step_name, filename, checkpoint_key in step_specs:
                value = manifest_stages.get(checkpoint_key, {}) if checkpoint_key else {}
                file_done = (path / filename).exists()
                status = value.get("status") or ("done" if file_done else "pending")
                if status == "running" and value.get("total_batches") and value.get("completed_batches") == value.get("total_batches"):
                    status = "done"
                if status == "running" and file_done and not value.get("total_batches"):
                    status = "done"
                steps.append({"name": step_name, "file": filename, "status": status,
                              "current_batch": value.get("current_batch"), "completed_batches": value.get("completed_batches"),
                              "total_batches": value.get("total_batches"), "source_count": value.get("source_count")})
            if stage_id == "stage5" and "stage_5_audit" in manifest_stages:
                value = manifest_stages["stage_5_audit"]
                steps.insert(1, {"name": "研究完整性与估值语义审计", "file": value.get("file", "research_card_raw.json"),
                                 "status": value.get("status", "pending"), "current_batch": None,
                                 "completed_batches": None, "total_batches": None, "source_count": None})
            statuses = [step["status"] for step in steps]
            stage_status = "failed" if "failed" in statuses else "running" if "running" in statuses else "done" if all(status == "done" for status in statuses) else "pending"
            stages.append({"id": stage_id, "name": label, "status": stage_status, "steps": steps})
        if not any(stage["status"] in {"done", "running", "failed"} for stage in stages):
            continue
        rows.append({"run_id": path.name, "code": code, "name": name, "status": manifest.get("status", "running"),
                     "error": manifest.get("error"), "started_at": manifest.get("started_at"), "updated_at": datetime.now().isoformat(timespec="seconds"), "stages": stages})
    target = DATA_DIR / "valuation_progress.js"
    target.write_text("window.QUANT_DASHBOARD_VALUATION_PROGRESS = " + json.dumps({"generated_at": datetime.now().isoformat(timespec="seconds"), "runs": rows}, ensure_ascii=False) + ";\n", encoding="utf-8")
    return target


def main() -> int:
    parser = argparse.ArgumentParser(description="发布模型三估值 Dashboard 数据包")
    parser.add_argument("--date", help="运行日期，支持 YYMMDD 或 YYYY-MM-DD")
    parser.add_argument("--all", action="store_true", help="发布所有存在已验证运行的日期")
    parser.add_argument("--progress", action="store_true", help="发布运行中/失败运行的进度数据包")
    args = parser.parse_args()
    if args.progress:
        print(json.dumps({"output": str(publish_progress())}, ensure_ascii=False)); return 0
    runs = verified_runs()
    if args.all:
        dates = sorted({item["date"] for item in runs})
    elif args.date:
        dates = [stamp(args.date)]
    else:
        dates = [max(item["date"] for item in runs)] if runs else []
    if not dates:
        raise SystemExit("未找到完成的模型三 v2 运行包")
    outputs = [str(publish(date)) for date in dates]
    outputs.append(str(publish_latest()))
    outputs.append(str(publish_progress()))
    print(json.dumps({"dates": dates, "outputs": outputs}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
