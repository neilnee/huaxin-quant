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


def compact_run(item: dict) -> dict:
    path = item["path"]
    card, params, result = (load_json(path / name) for name in ("research_card.json", "calc_params.json", "calc_results.json"))
    meta = params.get("meta", {})
    code, name = resolve_identity(path, item["manifest"], params, card)
    total_2026 = result.get("matrix_2026e", {}).get("total", {})
    total_2027 = result.get("matrix_2027e", {}).get("total", {})
    shares = meta.get("total_shares")
    valuation = {
        "current_price": meta.get("current_price"),
        "price_date": meta.get("price_date"),
        "price_source": meta.get("price_source"),
        "pessimistic": per_share(total_2026.get("pessimistic"), shares),
        "base": per_share(total_2026.get("base"), shares),
        "optimistic": per_share(total_2026.get("optimistic"), shares),
        "base_2027": per_share(total_2027.get("base"), shares),
    }
    try:
        valuation["base_upside_pct"] = round((valuation["base"] / float(valuation["current_price"]) - 1) * 100, 1)
    except (TypeError, ValueError, ZeroDivisionError):
        valuation["base_upside_pct"] = None
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
        "facts": card.get("facts", []), "assumptions": card.get("assumptions", []),
        "risks": card.get("risks", []), "catalysts": card.get("catalysts", []),
        "evidence": load_json(path / "evidence.json").get("evidence", []),
        "research_stats": {
            "evidence_count": len(load_json(path / "evidence.json").get("evidence", [])),
            "consensus_count": len(card.get("consensus", [])),
            "pillar_count": len(card.get("business_pillars", [])),
            "divergence_count": len(card.get("market_divergences", [])),
            "option_count": len(card.get("narrative_options", [])),
            "verification_count": len(card.get("verification_nodes", [])),
        },
    }


def load_index():
    path = DATA_DIR / "index.js"
    if not path.exists():
        return {}
    match = re.search(r"=\s*(\{.*\});\s*$", path.read_text(encoding="utf-8"), re.S)
    return json.loads(match.group(1)) if match else {}


def write_index():
    index = load_index()
    dates = sorted(item.stem.rsplit("_", 1)[-1] for item in DATA_DIR.glob("*/valuation_context_*.js"))
    index["valuation"] = {"latest": dates[-1] if dates else None, "available": dates, "catalog": "data/valuation_latest.js"}
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
    valuations = sorted((compact_run(item) for item in newest_by_code.values()), key=lambda row: (row["valuation"].get("base_upside_pct") is None, -(row["valuation"].get("base_upside_pct") or -9999), row["code"]))
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
    valuations = sorted(
        (compact_run(item) for item in newest_by_code.values()),
        key=lambda row: (row.get("run_at") or "", row["code"]), reverse=True,
    )
    context = {
        "meta": {"generated_at": datetime.now().isoformat(timespec="seconds"), "source": "newest verified valuation run per company"},
        "summary": {
            "total": len(valuations),
            "with_consensus": sum(row["status"]["consensus"].get("status") == "done" for row in valuations),
            "evidence_count": sum(row["research_stats"]["evidence_count"] for row in valuations),
        },
        "valuations": valuations,
    }
    target = DATA_DIR / "valuation_latest.js"
    target.write_text("window.QUANT_DASHBOARD_VALUATION_LATEST = " + json.dumps(context, ensure_ascii=False) + ";\n", encoding="utf-8")
    return target


def publish_progress() -> Path:
    """Publish in-flight valuation run checkpoints without touching verified contexts."""
    rows = []
    stage_schema = [
        ("stage0", "阶段零 · 数据准备", [("基础数据快照", "briefing.json", "evidence")]),
        ("stage1", "阶段一 · 业务与利润支柱", [("原文分批阅读", "stage_1_readings.json", "阶段一"), ("业务拆解与专题计划", "stage_1_business.json", "stage_1_business")]),
        ("stage2", "阶段二 · 一致预期与可比", [("研报分批阅读", "stage_2_readings.json", "阶段二"), ("共识、可比与分歧", "stage_2_consensus.json", "stage_2_consensus")]),
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
