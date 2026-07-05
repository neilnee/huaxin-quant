#!/usr/bin/env python3
"""
Bloom signal layer for Model 4.

Bloom consumes Model 2 quant JSON, maintains cross-day signal lifecycle state,
and writes Bloom-only outputs under bloom/. It does not run valuation, read
positions, or emit final trade actions.
"""

import argparse
import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import PROJECT_ROOT
from scripts.strategy_config import load_strategy_config


BLOOM_STRATEGY_FILE = "04-bloom.json"

CONFIG, STRATEGY_PATH = load_strategy_config(BLOOM_STRATEGY_FILE)
STRATEGY_VERSION = CONFIG["strategy_version"]

QUANT_RUNS_DIR = Path(PROJECT_ROOT) / CONFIG["inputs"]["quant_run_dir"]
BLOOM_STATE_PATH = Path(PROJECT_ROOT) / CONFIG["inputs"]["state_path"]
BLOOM_EVENTS_PATH = Path(PROJECT_ROOT) / CONFIG["inputs"]["events_path"]
BLOOM_INPUT_DIR = Path(PROJECT_ROOT) / CONFIG["outputs"]["review_input_dir"]
BLOOM_REPORT_DIR = Path(PROJECT_ROOT) / CONFIG["outputs"]["daily_report_dir"]

LEGACY_STATE_PATH = Path(PROJECT_ROOT) / "bloom" / "bloom_state.csv"

STATUS_RANK = CONFIG["statuses"]["rank"]
ACTIVE_STATUSES = set(CONFIG["statuses"]["active"])
HOLD_STATUSES = set(CONFIG["statuses"]["hold"])
HARD_RISK_FLAGS = set(CONFIG["risk_rules"]["hard_flags"])

STATE_FIELDS = [
    "code",
    "name",
    "first_seen",
    "last_seen",
    "days_tracked",
    "days_in_observation",
    "days_since_active",
    "consecutive_reject",
    "bloom_status",
    "bloom_signal",
    "event_type",
    "pool_decision",
    "signal_quality",
    "risk_level",
    "valuation_candidate",
    "valuation_priority",
    "model2_stage",
    "model2_setup_signal",
    "model2_action_hint",
    "structure_score",
    "structure_risk_score",
    "structure_risk_flags",
    "close",
    "MA20",
    "MA60",
    "distance_ma20",
    "volume_dry_up",
    "pivot_distance",
    "score_change",
    "best_status",
    "best_score",
    "best_date",
    "watch_reason",
    "next_watch_point",
    "contraction_count",
    "contraction_pcts",
    "contraction_days",
    "contraction_group",
    "volume_pattern",
    "strategy_version",
]


def ensure_dirs():
    BLOOM_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOOM_EVENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
    BLOOM_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    BLOOM_REPORT_DIR.mkdir(parents=True, exist_ok=True)


def normalize_date_arg(value):
    if not value:
        return None
    value = value.strip()
    if re.fullmatch(r"\d{6}", value):
        return value
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m-%d").strftime("%y%m%d")
    raise ValueError("date must be YYMMDD or YYYY-MM-DD")


def date_yy_to_iso(value):
    return datetime.strptime(value, "%y%m%d").strftime("%Y-%m-%d")


def latest_quant_run():
    files = sorted(QUANT_RUNS_DIR.glob("quant_*.json"))
    return files[-1] if files else None


def quant_run_for_date(date_yy):
    path = QUANT_RUNS_DIR / f"quant_{date_yy}.json"
    return path if path.exists() else None


def previous_quant_run(date_yy):
    files = sorted(QUANT_RUNS_DIR.glob("quant_*.json"))
    candidates = []
    for path in files:
        match = re.fullmatch(r"quant_(\d{6})\.json", path.name)
        if match and match.group(1) < date_yy:
            candidates.append(path)
    return candidates[-1] if candidates else None


def load_json(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def safe_int(value, default=0):
    try:
        if value in (None, ""):
            return default
        return int(float(value))
    except Exception:
        return default


def safe_float(value, default=0.0):
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def fmt_num(value):
    if value in (None, ""):
        return ""
    try:
        number = float(value)
    except Exception:
        return str(value)
    return f"{number:.2f}".rstrip("0").rstrip(".")


def as_list(value):
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    if isinstance(value, str) and ";" in value:
        return [item for item in value.split(";") if item]
    return [str(value)]


def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if value in ("True", "true", "TRUE", "1", 1):
        return True
    if value in ("False", "false", "FALSE", "0", 0):
        return False
    return None


def normalize_code(value):
    return str(value or "").replace('="', "").replace('"', "").strip()


def quant_stage(row):
    return row.get("structure_stage") or row.get("vcp_stage") or ""


def quant_signal(row):
    return row.get("setup_signal") or row.get("state") or ""


def quant_action(row):
    return row.get("action_hint") or row.get("pool_type") or ""


def quant_score(row):
    return row.get("structure_score", row.get("setup_score"))


def quant_risk_score(row):
    return row.get("structure_risk_score", row.get("risk_score"))


def quant_risk_flags(row):
    return as_list(row.get("structure_risk_flags", row.get("risk_flags")))


def quant_model2_include(row):
    value = normalize_bool(row.get("model2_include", row.get("model2_pass")))
    if value is not None:
        return value
    return row.get("pool_type") != "REJECT" and row.get("state") != "REJECT"


def normalize_status(value):
    value = str(value or "").strip()
    if not value:
        return ""
    upper = value.upper()
    legacy_map = {
        "RETEST": "TRIGGERED",
        "BREAKOUT": "COOLDOWN",
        "REJECTED": "COOLDOWN",
        "DATA_ISSUE": "DATA_ISSUE",
        "INVALID": "INVALID",
        "EARLY": "EARLY",
        "FORMING": "FORMING",
        "MATURE": "MATURE",
        "COOLDOWN": "COOLDOWN",
        "EXIT": "EXIT",
        "RISK_BLOCKED": "RISK_BLOCKED",
        "TRIGGERED": "TRIGGERED",
    }
    return legacy_map.get(upper, upper)


def is_active_status(status):
    return status in ACTIVE_STATUSES


def status_rank(status):
    return STATUS_RANK.get(status, 0)


def risk_level(row):
    score = safe_float(quant_risk_score(row), 0.0)
    flags = set(quant_risk_flags(row))
    if flags & HARD_RISK_FLAGS:
        return "HARD"
    if score >= CONFIG["risk_rules"]["high_min_score"]:
        return "HIGH"
    if score <= CONFIG["risk_rules"]["low_max_score"]:
        return "LOW"
    return "MEDIUM"


def risk_blocks(row):
    level = risk_level(row)
    score = safe_float(quant_risk_score(row), 0.0)
    return level == "HARD" or score >= CONFIG["risk_rules"]["risk_block_min_score"]


def base_status(row):
    stage = quant_stage(row)
    setup = quant_signal(row)
    action = str(quant_action(row)).upper()

    if stage in {"DATA_ISSUE", "DATA_INSUFFICIENT"}:
        return "DATA_ISSUE"

    setup_status = CONFIG["setup_mapping"].get(setup)
    if setup_status:
        status = setup_status
    else:
        if row.get("structure_valid") is False or stage == "STRUCTURE_INVALID":
            return "INVALID"
        status = CONFIG["stage_mapping"].get(stage)

    if not status:
        status = "COOLDOWN" if (not quant_model2_include(row) or action == "REJECT") else "FORMING"

    if status in {"FORMING", "MATURE", "TRIGGERED"} and risk_blocks(row):
        return "RISK_BLOCKED"
    return status


def read_state():
    path = BLOOM_STATE_PATH if BLOOM_STATE_PATH.exists() else LEGACY_STATE_PATH
    if not path.exists():
        return {}
    rows = {}
    with open(path, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = normalize_code(row.get("code"))
            if not code:
                continue
            status = normalize_status(row.get("bloom_status") or row.get("active_status"))
            row["code"] = code
            row["bloom_status"] = status
            row["structure_score"] = row.get("structure_score") or row.get("last_score", "")
            rows[code] = row
    return rows


def read_state_before(date_iso):
    rows = read_state()
    return {
        code: row for code, row in rows.items()
        if (row.get("last_seen") or "") < date_iso
    }


def write_state(rows):
    ordered = sorted(rows.values(), key=lambda r: (
        0 if is_active_status(r.get("bloom_status")) else 1,
        -safe_float(r.get("structure_score")),
        r.get("code", ""),
    ))
    with open(BLOOM_STATE_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STATE_FIELDS)
        writer.writeheader()
        for row in ordered:
            writer.writerow({field: row.get(field, "") for field in STATE_FIELDS})


def remove_events_for_date(date_iso):
    if not BLOOM_EVENTS_PATH.exists():
        return []
    kept = []
    with open(BLOOM_EVENTS_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("date") != date_iso:
                kept.append(event)
    return kept


def write_events(date_iso, events):
    kept = remove_events_for_date(date_iso)
    with open(BLOOM_EVENTS_PATH, "w", encoding="utf-8") as f:
        for event in kept + events:
            f.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + "\n")


def score_change(prev_row, row):
    if not prev_row:
        return None
    prev_score = safe_float(prev_row.get("structure_score") or prev_row.get("last_score"), None)
    if prev_score is None:
        return None
    return safe_float(quant_score(row), 0.0) - prev_score


def quality_for(status, risk, score):
    if status in {"EXIT", "INVALID", "DATA_ISSUE"}:
        return "NONE"
    if status == "RISK_BLOCKED" or risk in {"HIGH", "HARD"}:
        return "BLOCKED"
    if score >= CONFIG["score_rules"]["high_quality_score"] and status in {"MATURE", "TRIGGERED"}:
        return "HIGH"
    if score >= CONFIG["score_rules"]["medium_quality_score"]:
        return "MEDIUM"
    return "LOW"


def valuation_priority(status, risk, score):
    rules = CONFIG["valuation_candidate_rules"]
    if status in {"INVALID", "EXIT", "DATA_ISSUE", "RISK_BLOCKED"}:
        return "NONE"
    if risk in {"HIGH", "HARD"}:
        return "NONE"
    if status in {"TRIGGERED", "MATURE"} and score >= rules["high_min_score"] and risk == "LOW":
        return "HIGH"
    if status in {"FORMING", "MATURE"} and score >= rules["medium_min_score"]:
        if risk == "LOW" or (risk == "MEDIUM" and rules["allow_medium_risk_for_medium_priority"]):
            return "MEDIUM"
    if status == "EARLY":
        return "LOW"
    return "NONE"


def pool_decision(status, event_type):
    if event_type == "NEW_ENTRY":
        return "ADD"
    if status in {"TRIGGERED", "MATURE", "FORMING"}:
        return "KEEP_FOCUS"
    if status == "EARLY":
        return "KEEP_LOW"
    if status in {"RISK_BLOCKED", "COOLDOWN", "INVALID"}:
        return "COOLDOWN"
    if status == "DATA_ISSUE":
        return "DATA_HOLD"
    return "EXIT"


def lifecycle_event(prev_row, status):
    prev_status = normalize_status(prev_row.get("bloom_status")) if prev_row else ""
    prev_active = prev_status in ACTIVE_STATUSES or prev_status in HOLD_STATUSES
    curr_active = status in ACTIVE_STATUSES or status in HOLD_STATUSES

    if status == "DATA_ISSUE":
        return "DATA_HOLD"
    if status == "EXIT":
        return "EXIT"
    if not prev_row and status in ACTIVE_STATUSES:
        return "NEW_ENTRY"
    if prev_active and curr_active:
        if status_rank(status) > status_rank(prev_status):
            return "UPGRADE"
        if status_rank(status) < status_rank(prev_status):
            return "DOWNGRADE"
        return "CONTINUED"
    if prev_active and not curr_active:
        return "COOLDOWN"
    return "CONTINUED"


def bloom_signal(row, status, event_type, delta):
    if status == "DATA_ISSUE":
        return "DATA_HOLD"
    if status == "EXIT":
        return "EXIT"
    if status == "RISK_BLOCKED":
        return "RISK_BLOCK"
    if status == "TRIGGERED":
        return "SETUP_TRIGGER"
    if event_type in {"NEW_ENTRY", "UPGRADE", "DOWNGRADE", "COOLDOWN"}:
        return event_type
    if delta is not None and delta >= CONFIG["score_rules"]["upgrade_delta"]:
        return "UPGRADE"
    if delta is not None and delta <= CONFIG["score_rules"]["downgrade_delta"]:
        return "DOWNGRADE"
    return "CONTINUED"


def apply_exit_rules(prev_row, status):
    if status == "DATA_ISSUE":
        return status
    consecutive = safe_int(prev_row.get("consecutive_reject")) if prev_row else 0
    if status in {"COOLDOWN", "INVALID"}:
        consecutive += 1
    else:
        consecutive = 0

    if status == "INVALID":
        threshold = CONFIG["pool_rules"]["invalid_keep_days"]
    elif status == "COOLDOWN":
        threshold = CONFIG["pool_rules"]["cooldown_keep_days"]
    else:
        threshold = CONFIG["pool_rules"]["exit_after_consecutive_reject"]

    if status in {"COOLDOWN", "INVALID"} and consecutive >= threshold:
        return "EXIT"
    return status


def watch_text(status, signal, row, risk):
    stage = quant_stage(row)
    setup = quant_signal(row)
    reason = row.get("reason", "")
    flags = quant_risk_flags(row)

    if signal == "SETUP_TRIGGER":
        return (
            f"模型二触发 {setup}",
            "核对风险释放、回踩有效性和估值优先级，不直接转成买入动作",
        )
    if signal == "RISK_BLOCK":
        flag_text = ";".join(flags) if flags else risk
        return (
            f"结构仍需观察，但风险阻断: {flag_text}",
            "等待过热、趋势破坏或放量风险释放后再评估",
        )
    if status == "MATURE":
        return (
            f"结构成熟: {stage}",
            "观察是否出现 PULLBACK_BUY 或 RETEST_BUY",
        )
    if status == "FORMING":
        return (
            "结构形成中",
            "观察收缩轮次、缩量质量和 pivot 距离是否继续改善",
        )
    if status == "EARLY":
        return (
            "早期结构进入低优先级观察",
            "等待第二、第三轮收缩确认",
        )
    if status == "COOLDOWN":
        return (
            reason or "模型二临时出局，进入冷却观察",
            "观察是否重新形成有效结构，否则达到保留期后移出",
        )
    if status == "INVALID":
        return (
            reason or "结构失效",
            "等待趋势或结构重建",
        )
    if status == "DATA_ISSUE":
        return (
            reason or "数据异常",
            "等待下一次完整模型二结果，不因单日异常移出",
        )
    if status == "EXIT":
        return (
            reason or "达到移出规则",
            "从 Bloom 池移出，后续由模型二重新发现",
        )
    return (reason, "继续观察模型二结构变化")


def state_row(prev_row, row, date_iso, status):
    prev_row = prev_row or {}
    delta = score_change(prev_row, row)
    score = safe_float(quant_score(row), 0.0)
    risk_score = safe_float(quant_risk_score(row), 0.0)
    risk = risk_level(row)
    status = apply_exit_rules(prev_row, status)
    event_type = lifecycle_event(prev_row, status)
    signal = bloom_signal(row, status, event_type, delta)
    quality = quality_for(status, risk, score)
    val_priority = valuation_priority(status, risk, score)
    decision = pool_decision(status, event_type)
    watch_reason, next_watch_point = watch_text(status, signal, row, risk)

    best_score = safe_float(prev_row.get("best_score"), -1.0)
    best_status = prev_row.get("best_status", "")
    best_date = prev_row.get("best_date", "")
    if score >= best_score:
        best_score = score
        best_status = status
        best_date = date_iso

    days_tracked = safe_int(prev_row.get("days_tracked")) + 1 if prev_row else 1
    days_in_observation = safe_int(prev_row.get("days_in_observation"))
    if is_active_status(status):
        days_in_observation += 1

    previous_since_active = safe_int(prev_row.get("days_since_active"))
    days_since_active = 0 if is_active_status(status) else previous_since_active + 1
    consecutive_reject = safe_int(prev_row.get("consecutive_reject"))
    if status in {"COOLDOWN", "INVALID", "EXIT"}:
        consecutive_reject += 1
    elif status != "DATA_ISSUE":
        consecutive_reject = 0

    return {
        "code": row.get("code", ""),
        "name": row.get("name") or prev_row.get("name", ""),
        "first_seen": prev_row.get("first_seen") or date_iso,
        "last_seen": date_iso,
        "days_tracked": str(days_tracked),
        "days_in_observation": str(days_in_observation),
        "days_since_active": str(days_since_active),
        "consecutive_reject": str(consecutive_reject),
        "bloom_status": status,
        "bloom_signal": signal,
        "event_type": event_type,
        "pool_decision": decision,
        "signal_quality": quality,
        "risk_level": risk,
        "valuation_candidate": str(val_priority != "NONE").lower(),
        "valuation_priority": val_priority,
        "model2_stage": quant_stage(row),
        "model2_setup_signal": quant_signal(row),
        "model2_action_hint": quant_action(row),
        "structure_score": fmt_num(score),
        "structure_risk_score": fmt_num(risk_score),
        "structure_risk_flags": ";".join(quant_risk_flags(row)),
        "close": fmt_num(row.get("close")),
        "MA20": fmt_num(row.get("MA20")),
        "MA60": fmt_num(row.get("MA60")),
        "distance_ma20": fmt_num(row.get("distance_ma20")),
        "volume_dry_up": fmt_num(row.get("volume_dry_up")),
        "pivot_distance": fmt_num(row.get("pivot_distance")),
        "score_change": "" if delta is None else fmt_num(delta),
        "best_status": best_status,
        "best_score": fmt_num(best_score),
        "best_date": best_date,
        "watch_reason": watch_reason,
        "next_watch_point": next_watch_point,
        "contraction_count": str(row.get("contraction_count", "")),
        "contraction_pcts": str(row.get("contraction_pcts", "")),
        "contraction_days": str(row.get("contraction_days", "")),
        "contraction_group": json.dumps(row.get("contraction_group", []), ensure_ascii=False),
        "volume_pattern": str(row.get("volume_pattern", "")),
        "strategy_version": STRATEGY_VERSION,
    }


def row_event(row, date_iso):
    return {
        "date": date_iso,
        "code": row["code"],
        "name": row["name"],
        "bloom_status": row["bloom_status"],
        "bloom_signal": row["bloom_signal"],
        "event_type": row["event_type"],
        "pool_decision": row["pool_decision"],
        "structure_score": safe_float(row["structure_score"]),
        "structure_risk_score": safe_float(row["structure_risk_score"]),
        "risk_level": row["risk_level"],
        "valuation_candidate": row["valuation_candidate"],
        "valuation_priority": row["valuation_priority"],
        "watch_reason": row["watch_reason"],
        "next_watch_point": row["next_watch_point"],
        "strategy_version": STRATEGY_VERSION,
    }


def state_from_quant_results(results, date_iso):
    state = {}
    for source in results:
        code = normalize_code(source.get("code"))
        if not code:
            continue
        row = dict(source)
        row["code"] = code
        status = base_status(row)
        state[code] = state_row(None, row, date_iso, status)
    return state


def missing_data_row(prev_row, date_iso):
    row = {
        "code": prev_row.get("code", ""),
        "name": prev_row.get("name", ""),
        "structure_score": prev_row.get("structure_score", ""),
        "structure_risk_score": prev_row.get("structure_risk_score", ""),
        "structure_risk_flags": prev_row.get("structure_risk_flags", ""),
        "reason": "今日模型二结果缺失，可能为 API 失败或输入池缺失",
    }
    output = state_row(prev_row, row, date_iso, "DATA_ISSUE")
    output["bloom_signal"] = "DATA_HOLD"
    output["event_type"] = "DATA_HOLD"
    output["pool_decision"] = "DATA_HOLD"
    return output


def compact_row(row):
    return {field: row.get(field, "") for field in STATE_FIELDS}


def build_bloom(payload, previous_payload, date_yy, allow_partial=False):
    meta = payload.get("meta", {})
    run_date = meta.get("run_date") or date_yy_to_iso(date_yy)
    mode = meta.get("mode", "")
    if mode != "全量" and not allow_partial:
        raise RuntimeError(f"Bloom requires full quant run, got mode={mode!r}; use --allow-partial to bypass")

    results = payload.get("results", [])
    prev_state = read_state_before(run_date)
    if not prev_state and previous_payload:
        prev_date = previous_payload.get("meta", {}).get("run_date") or ""
        prev_state = state_from_quant_results(previous_payload.get("results", []), prev_date)

    by_code = {}
    for item in results:
        code = normalize_code(item.get("code"))
        if code:
            row = dict(item)
            row["code"] = code
            by_code[code] = row

    new_state = dict(prev_state)
    events = []

    for code, source in by_code.items():
        prev_row = prev_state.get(code)
        status = base_status(source)
        updated = state_row(prev_row, source, run_date, status)
        new_state[code] = updated
        if updated["bloom_signal"] != "CONTINUED" or updated["event_type"] != "CONTINUED":
            events.append(row_event(updated, run_date))

    for code, prev_row in prev_state.items():
        if code in by_code:
            continue
        prev_status = normalize_status(prev_row.get("bloom_status"))
        if prev_status in ACTIVE_STATUSES or prev_status in HOLD_STATUSES:
            updated = missing_data_row(prev_row, run_date)
            new_state[code] = updated
            events.append(row_event(updated, run_date))

    rows = [compact_row(row) for row in new_state.values()]
    rows.sort(key=lambda r: (
        0 if is_active_status(r.get("bloom_status")) else 1,
        -safe_float(r.get("structure_score")),
        r.get("code", ""),
    ))

    sections = {
        "new_entries": [r for r in rows if r["event_type"] == "NEW_ENTRY"],
        "upgrades": [r for r in rows if r["bloom_signal"] == "UPGRADE"],
        "triggered": [r for r in rows if r["bloom_status"] == "TRIGGERED" or r["bloom_signal"] == "SETUP_TRIGGER"],
        "focus": [r for r in rows if r["pool_decision"] == "KEEP_FOCUS"],
        "risk_blocked": [r for r in rows if r["bloom_status"] == "RISK_BLOCKED" or r["bloom_signal"] == "RISK_BLOCK"],
        "cooldown": [r for r in rows if r["pool_decision"] == "COOLDOWN"],
        "exits": [r for r in rows if r["pool_decision"] == "EXIT"],
        "data_issues": [r for r in rows if r["bloom_status"] == "DATA_ISSUE"],
        "valuation_candidates": [r for r in rows if r["valuation_candidate"] == "true"],
        "watching": [r for r in rows if r.get("model2_stage") in {"VCP_FORMING", "VCP_MATURE", "VCP_TIGHT"}
                     or r.get("bloom_status") == "TRIGGERED"
                     or r.get("model2_setup_signal") in {"PULLBACK_BUY", "RETEST_BUY"}],
    }

    summary = {
        "date": run_date,
        "mode": mode,
        "input_total": meta.get("total"),
        "result_total": len(results),
        "state_total": len(rows),
        "active_total": sum(1 for r in rows if is_active_status(r.get("bloom_status"))),
        "new_entries": len(sections["new_entries"]),
        "upgrades": len(sections["upgrades"]),
        "triggered": len(sections["triggered"]),
        "risk_blocked": len(sections["risk_blocked"]),
        "cooldown": len(sections["cooldown"]),
        "exits": len(sections["exits"]),
        "data_issues": len(sections["data_issues"]),
        "valuation_candidates": len(sections["valuation_candidates"]),
        "status_dist": {s: sum(1 for r in rows if r.get("bloom_status") == s)
                        for s in ["TRIGGERED", "MATURE", "FORMING", "EARLY",
                                  "RISK_BLOCKED", "COOLDOWN", "INVALID", "DATA_ISSUE", "EXIT"]},
        "strategy_version": STRATEGY_VERSION,
        "strategy_file": STRATEGY_PATH,
        "quant_stats": payload.get("stats", {}),
    }

    bloom = {
        "meta": {
            "schema": "huaxin_bloom_signal_v1",
            "date": run_date,
            "date_yy": date_yy,
            "quant_run": str(QUANT_RUNS_DIR / f"quant_{date_yy}.json"),
            "strategy_version": STRATEGY_VERSION,
            "strategy_file": STRATEGY_PATH,
        },
        "summary": summary,
        "top_candidates": rows[: CONFIG["reporting"]["top_candidates"]],
        "sections": sections,
    }
    return bloom, {row["code"]: row for row in rows}, events


def write_bloom_input(date_yy, bloom):
    path = BLOOM_INPUT_DIR / f"bloom_input_{date_yy}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(bloom, f, ensure_ascii=False, indent=2)
    return path


def table_lines(rows, columns):
    if not rows:
        return ["无"]
    header = "| " + " | ".join(title for _, title in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]
    for row in rows:
        values = []
        for key, _ in columns:
            value = row.get(key, "")
            values.append(str(value) if value is not None else "")
        lines.append("| " + " | ".join(values) + " |")
    return lines


def build_markdown(bloom):
    summary = bloom["summary"]
    sections = bloom["sections"]

    # ── 头部 ──
    lines = [
        f"# Bloom Signal Report {summary['date']}",
        "",
    ]

    # ── 📊 今日概要（一段话）──
    triggered_n = summary.get("triggered", 0)
    blocked_n = summary.get("risk_blocked", 0)
    new_n = summary.get("new_entries", 0)
    exit_n = summary.get("exits", 0)
    active = summary.get("active_total", 0)

    alert_parts = []
    if triggered_n:
        alert_parts.append(f"{triggered_n} 只触发")
    if blocked_n:
        alert_parts.append(f"{blocked_n} 只风险阻断")
    alert_text = "，".join(alert_parts) if alert_parts else "无触发或阻断"

    lines.extend([
        "## 📊 今日概要",
        "",
        f"模型二扫描 {summary.get('input_total')} 只 → 产出 {summary.get('result_total')} 只。"
        f"Bloom 活跃观察 **{active}** 只，{alert_text}。"
        f"新进入 {new_n} 只，移出 {exit_n} 只。",
        "",
    ])

    # ── 🔥 重点观察 ──
    watching = sections.get("watching", [])
    lines.append("## 🔥 重点观察")
    if not watching:
        lines.extend(["", "*今日无符合条件的结构*", ""])
    else:
        lines.append("")
        shown = watching[:20]
        for r in shown:
            code = r.get("code", "")
            name = r.get("name", "")
            stage = r.get("model2_stage", "")
            status = r.get("bloom_status", "")
            score = r.get("structure_score", "")
            risk = r.get("risk_level", "")
            if risk in ("HIGH", "HARD"):
                risk = f"⚠️{risk}"
            cc = r.get("contraction_count", "") or "0"
            reason = r.get("watch_reason", "")
            vp = r.get("volume_pattern", "") or ""

            # Build contraction detail
            cg_raw = r.get("contraction_group", "") or ""
            detail_parts = []
            try:
                cg = json.loads(cg_raw) if isinstance(cg_raw, str) else cg_raw
            except (json.JSONDecodeError, TypeError):
                cg = []
            for c in cg:
                sd = str(c.get("start_date", ""))[5:]
                ed = str(c.get("end_date", ""))[5:]
                pct = c.get("pullback_pct", 0)
                detail_parts.append(f"{sd}-{ed}({pct:.1f}%)")
            detail = " → ".join(detail_parts) if detail_parts else r.get("contraction_pcts", "")
            if vp:
                detail += f"，量能 {vp}"

            meta = f"{stage} | {status} | {score}分 | {risk} | {cc}段收缩"
            lines.append(f"**`{code}` {name}** — {meta}")
            lines.append(f"> ↳ {detail}")
            if reason:
                lines.append(f"> {reason}")
            lines.append("")

        if len(watching) > 20:
            lines.append(f"> 共 {len(watching)} 只，以上展示前 20。\n")
    lines.append("")

    # ── 📋 池子变化（紧凑多列表格）──
    lines.append("## 📋 池子变化")
    new_entries = sections.get("new_entries", [])
    exits = sections.get("exits", [])

    if new_entries:
        lines.append(f"**新进入 {len(new_entries)} 只**")
        lines.append("")
        cols_per_row = 8
        header = "| " + " | ".join(["股票"] * cols_per_row) + " |"
        sep = "| " + " | ".join(["---"] * cols_per_row) + " |"
        lines.append(header)
        lines.append(sep)
        for i in range(0, len(new_entries), cols_per_row):
            chunk = new_entries[i:i + cols_per_row]
            cells = []
            for r in chunk:
                code = r.get("code", "")
                name = r.get("name", "")
                status = r.get("bloom_status", "")
                cells.append(f"`{code}` {name}<br>{status}")
            while len(cells) < cols_per_row:
                cells.append("")
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    else:
        lines.extend(["", "*无新进入*", ""])

    if exits:
        exit_list = [f"`{r['code']}` {r['name']}" for r in exits]
        lines.append(f"**移出 {len(exits)} 只**：{'、'.join(exit_list)}")
    else:
        lines.append("**移出**：无")
    lines.append("")

    # ── 📖 字段说明 ──
    lines.extend([
        "## 📖 字段说明",
        "",
        "| 字段 | 说明 |",
        "|------|------|",
        "| `bloom_status` | EARLY=早期 / FORMING=形成中 / MATURE=成熟 / TRIGGERED=已触发 / RISK_BLOCKED=风险阻断 / COOLDOWN=冷却 / INVALID=失效 / EXIT=移出 / DATA_ISSUE=数据异常 |",
        "| `bloom_signal` | NEW_ENTRY=新进入 / UPGRADE=升级 / DOWNGRADE=降级 / SETUP_TRIGGER=交易触发 / RISK_BLOCK=风险阻断 / COOLDOWN=进入冷却 / EXIT=移出 / DATA_HOLD=数据维持 / CONTINUED=延续 |",
        "| `pool_decision` | ADD=入池 / KEEP_FOCUS=重点观察 / KEEP_LOW=低优先观察 / COOLDOWN=冷却保留 / EXIT=移出 / DATA_HOLD=维持 |",
        "| `risk_level` | LOW=低 / MEDIUM=中 / HIGH=高 / HARD=硬风险 |",
        "| `signal_quality` | HIGH / MEDIUM / LOW / BLOCKED / NONE |",
        "| `valuation_priority` | HIGH / MEDIUM / LOW / NONE（由 Bloom 层判断，不读取模型三估值） |",
        "",
        f"> strategy: {summary['strategy_version']}",
    ])
    return "\n".join(lines) + "\n"


def write_markdown(date_yy, markdown):
    path = BLOOM_REPORT_DIR / f"bloom_{date_yy}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    return path


def main():
    parser = argparse.ArgumentParser(description="Huaxin Quant Bloom signal layer")
    parser.add_argument("--date", help="Bloom 日期，支持 YYMMDD 或 YYYY-MM-DD；默认取最新 quant run")
    parser.add_argument("--allow-partial", action="store_true", help="允许单股/多股/测试模式生成 Bloom 输出")
    parser.add_argument("--no-state-update", action="store_true", help="只生成 bloom_input 和 Markdown，不更新状态层")
    args = parser.parse_args()

    ensure_dirs()

    date_yy = normalize_date_arg(args.date)
    quant_path = quant_run_for_date(date_yy) if date_yy else latest_quant_run()
    if not quant_path:
        print("错误: 未找到模型二 quant run JSON")
        sys.exit(1)
    match = re.fullmatch(r"quant_(\d{6})\.json", quant_path.name)
    if not match:
        print(f"错误: 不支持的 quant run 文件名: {quant_path}")
        sys.exit(1)
    date_yy = match.group(1)

    payload = load_json(quant_path)
    prev_path = previous_quant_run(date_yy)
    previous_payload = load_json(prev_path) if prev_path else None

    try:
        bloom, new_state, events = build_bloom(payload, previous_payload, date_yy, allow_partial=args.allow_partial)
    except Exception as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    if not args.no_state_update:
        write_state(new_state)
        write_events(bloom["summary"]["date"], events)

    input_path = write_bloom_input(date_yy, bloom)
    report_path = write_markdown(date_yy, build_markdown(bloom))

    print("=" * 70)
    print("Huaxin Bloom Signal")
    print("=" * 70)
    print(f"quant run: {quant_path}")
    print(f"previous: {prev_path if prev_path else 'none'}")
    print(f"bloom input: {input_path}")
    print(f"bloom report: {report_path}")
    print(f"bloom state: {BLOOM_STATE_PATH}")
    print(f"bloom events: {BLOOM_EVENTS_PATH}")
    print(f"summary: {bloom['summary']}")


if __name__ == "__main__":
    main()
