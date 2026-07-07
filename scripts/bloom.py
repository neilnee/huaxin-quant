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

import requests

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
    "days_in_data_issue",
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
    "llm_insight",
    "contraction_count",
    "contraction_pcts",
    "contraction_days",
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


def should_track_quant_row(row, prev_state):
    """Track new Model 2 includes, plus existing Bloom names that need lifecycle updates."""
    if quant_model2_include(row):
        return True
    code = normalize_code(row.get("code"))
    prev_row = prev_state.get(code)
    if not prev_row:
        return False
    return normalize_status(prev_row.get("bloom_status")) != "EXIT"


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
    active_rows = [
        row for row in rows.values()
        if normalize_status(row.get("bloom_status")) != "EXIT"
    ]
    ordered = sorted(active_rows, key=lambda r: (
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
        # 统计连续 DATA_ISSUE 天数，超期后移出观察池
        prev_days = safe_int(prev_row.get("days_in_data_issue")) if prev_row else 0
        days = prev_days + 1
        threshold = CONFIG["pool_rules"]["data_issue_keep_days"]
        if days > threshold:
            return "EXIT"
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
    prev_data_issue_days = safe_int(prev_row.get("days_in_data_issue"))
    days_in_data_issue = prev_data_issue_days + 1 if status == "DATA_ISSUE" else 0
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
        "days_in_data_issue": str(days_in_data_issue),
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
        if not should_track_quant_row(row, state):
            continue
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


# ── LLM 解读：术语翻译映射 ──

_STAGE_CN = {
    "VCP_MATURE": "VCP成熟期（收缩收敛、接近突破点）",
    "VCP_FORMING": "VCP形成期（收缩结构构建中）",
    "VCP_EARLY": "VCP早期（刚进入观察，结构尚不完整）",
    "VCP_TIGHT": "VCP紧凑期（波动极度收窄）",
}

_VOLUME_CN = {
    "drying": "缩量枯竭（近5日均量远低于20日均量，卖压衰竭）",
    "decreasing": "逐轮缩量（每轮收缩成交量递减）",
    "mixed": "量能不稳定（各轮收缩量能无明显规律）",
}

_RISK_FLAG_CN = {
    "DOWNTREND": "均线空排（MA20<MA60且价格在MA60下方）",
    "MA20_DECLINE": "MA20均线下行",
    "DEEP_FALL": "深度回撤（距60日高点超20%）",
    "OVERHEAT_CHG5": "短期过热（5日涨幅过大）",
    "FAR_ABOVE_MA20": "远离MA20均线",
    "EXTENDED_FROM_MA20": "远离MA20均线",
    "VOLUME_STALL": "放量滞涨",
    "LONG_UPPER_SHADOW": "长上影线抛压",
}

_RISK_LEVEL_CN = {
    "LOW": "低风险",
    "MEDIUM": "中风险",
    "HIGH": "高风险",
    "HARD": "硬风险阻断（存在结构性缺陷，不适合入场）",
}

_BLOOM_STATUS_CN = {
    "MATURE": "成熟观察",
    "FORMING": "形成中观察",
    "EARLY": "早期观察",
    "RISK_BLOCKED": "风险阻断",
    "TRIGGERED": "已触发信号",
}


def _translate_risk_flags(flags_str: str) -> str:
    """将 DOWNTREND;MA20_DECLINE 翻译为中文短语列表。"""
    if not flags_str:
        return "无"
    parts = [s.strip() for s in flags_str.split(";") if s.strip()]
    translated = [_RISK_FLAG_CN.get(p, p) for p in parts]
    return "；".join(translated)


def _describe_volume_trend(contractions: list) -> str:
    """从逐轮收缩的 avg_volume 提炼量能变化趋势，只描述数据不做判断。

    返回如：
      - "178万→138万→162万（末轮较首轮-9%，整体持平）"
      - "2.4千万→1.7千万→1.3千万（逐轮递减，末轮较首轮-46%）"
      - "1.5千万→2.1千万→1.4千万（波动，末轮较首轮-3%）"
    """
    if not contractions or len(contractions) < 2:
        return "量能数据不足"

    vols = []
    for c in contractions:
        v = c.get("avg_volume", 0)
        try:
            vols.append(float(v))
        except (ValueError, TypeError):
            vols.append(0)

    n = len(vols)

    def _fmt(v):
        if v >= 1e7:
            return f"{v/1e7:.1f}千万"
        elif v >= 1e4:
            return f"{v/1e4:.0f}万"
        else:
            return f"{v:.0f}"

    vols_str = "→".join(_fmt(v) for v in vols)
    first_v, last_v = vols[0], vols[-1]
    change = (last_v - first_v) / first_v * 100 if first_v > 0 else 0

    # 趋势定性
    if n >= 3:
        # 检查是否单调递减
        decreasing = all(vols[i] >= vols[i+1] for i in range(n-1))
        increasing = all(vols[i] <= vols[i+1] for i in range(n-1))
        if decreasing:
            return f"{vols_str}（逐轮递减，末轮较首轮{change:+.0f}%）"
        if increasing:
            return f"{vols_str}（逐轮递增，末轮较首轮{change:+.0f}%）"

    # 看首尾变化幅度
    if change > 30:
        return f"{vols_str}（末轮较首轮+{change:.0f}%，明显放量）"
    if change < -30:
        return f"{vols_str}（末轮较首轮{change:.0f}%，明显缩量）"
    if abs(change) <= 15:
        return f"{vols_str}（末轮较首轮{change:+.0f}%，整体持平）"

    return f"{vols_str}（末轮较首轮{change:+.0f}%）"


def _build_stock_context(r: dict, contractions: list = None) -> dict:
    """将 bloom 内部字段重组为 LLM 可理解的中文上下文。"""
    flags_cn = _translate_risk_flags(r.get("structure_risk_flags", ""))
    stage_raw = r.get("model2_stage", "")
    risk_raw = r.get("risk_level", "")
    bloom_raw = r.get("bloom_status", "")

    # 均线上下文
    close = r.get("close", "")
    ma20 = r.get("MA20", "")
    ma60 = r.get("MA60", "")
    if close and ma20 and ma60:
        try:
            c, m20, m60 = float(close), float(ma20), float(ma60)
            above_ma60 = "站上" if c > m60 else "低于"
            ma_context = f"收盘{c}，MA20={m20}，MA60={m60}（价格{above_ma60}MA60）"
        except (ValueError, TypeError):
            ma_context = f"收盘{close}，MA20={ma20}，MA60={ma60}"
    else:
        ma_context = ""

    # 枢轴上下文
    pivot = r.get("pivot_price", "")
    pivot_dist = r.get("pivot_distance", "")
    if pivot and pivot_dist is not None and pivot_dist != "":
        try:
            pd_val = float(pivot_dist)
            if pd_val < -2:
                pivot_context = f"枢轴{pivot}元，距突破位{pd_val:+.1f}%（尚未突破）"
            elif pd_val > 2:
                pivot_context = f"枢轴{pivot}元，距突破位{pd_val:+.1f}%（已突破）"
            else:
                pivot_context = f"枢轴{pivot}元，紧贴突破位（{pd_val:+.1f}%）"
        except (ValueError, TypeError):
            pivot_context = f"枢轴{pivot}元"
    else:
        pivot_context = ""

    # 量能趋势：用逐轮明细替代 volume_pattern 标签
    volume_detail = _describe_volume_trend(contractions) if contractions else "量能数据暂缺"

    return {
        "code": r.get("code", ""),
        "name": r.get("name", ""),
        "结构阶段": _STAGE_CN.get(stage_raw, stage_raw),
        "Bloom状态": _BLOOM_STATUS_CN.get(bloom_raw, bloom_raw),
        "综合评分": f"{r.get('structure_score', '')}分",
        "风险等级": _RISK_LEVEL_CN.get(risk_raw, risk_raw),
        "风险标记": flags_cn,
        "收缩与量能": (
            f"{r.get('contraction_count', '')}轮收缩，幅度{r.get('contraction_pcts', '')}；"
            f"量能趋势：{volume_detail}"
        ),
        "关键位置": "；".join(filter(None, [ma_context, pivot_context])),
    }


def call_llm_insights(watching_rows, quant_results=None):
    """为每只重点观察标的生成自然语言解读。

    调用 DeepSeek API，先将内部编码翻译为中文术语再传入，
    返回 ({code: insight_text}, status)；API 不可用时返回空字典和失败原因。
    """
    api_key = os.environ.get("DEEPSEEK_API_KEY")
    if not api_key:
        return {}, {
            "status": "skipped",
            "reason": "DEEPSEEK_API_KEY missing",
            "requested": len(watching_rows),
            "returned": 0,
        }

    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    status = {
        "status": "pending",
        "provider": "deepseek",
        "model": model,
        "base_url": base_url,
        "requested": len(watching_rows),
        "returned": 0,
    }

    # 建立 code → contractions 的查找表（从 quant 原始结果中取）
    contractions_by_code = {}
    if quant_results:
        for item in quant_results:
            code = normalize_code(item.get("code"))
            if code:
                cs = item.get("contractions", [])
                if cs:
                    contractions_by_code[code] = cs

    stocks_data = []
    for r in watching_rows:
        code = r.get("code", "")
        all_cs = contractions_by_code.get(code, [])
        # 只取 VCP 有效收缩轮次（与 contraction_count 对齐）
        cc = int(r.get("contraction_count", 0)) if r.get("contraction_count") else 0
        if cc > 0 and len(all_cs) >= cc:
            contractions = all_cs[-cc:]
        else:
            contractions = all_cs
        stocks_data.append(_build_stock_context(r, contractions))

    system_prompt = (
        "你是A股量价形态（VCP）解读助手。根据每只股票的结构化数据，"
        "生成一句简洁的中文解读（40-60字），涵盖三个要点：\n"
        "① 结构状态——收缩是否收敛、量能是否衰竭\n"
        "② 关键位置——与枢轴、均线的关系\n"
        "③ 观察方向——等突破确认 / 等风险释放 / 等结构改善\n\n"
        "术语参考：\n"
        "- VCP（波动收缩形态）：上升趋势中多轮回调，每轮波幅递减、量能萎缩，"
        "表明卖压衰竭、筹码锁定，是潜在突破前兆\n"
        "- 枢轴（pivot）：前期波段高点，股价放量突破此位视为结构完成\n"
        "- 缩量枯竭：成交量萎缩至极低水平，卖方力量耗尽，是正面信号\n"
        "- 逐轮缩量：每轮收缩的成交量递减，筹码趋于锁定\n"
        "- 均线空排：短期均线在长期均线下方，处于下跌趋势中\n"
        "- 短期过热：近期涨幅过大，追涨风险高，需等待回调\n\n"
        "规则：只使用输入中已有的数据，不得引入未提供的外部事实。"
        "必须输出合法 JSON 对象，格式为："
        '{"insights":[{"code":"...","insight":"..."}]}'
    )

    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": json.dumps(stocks_data, ensure_ascii=False)},
                ],
                "response_format": {"type": "json_object"},
                "stream": False,
                "temperature": 0.3,
                "max_tokens": 2000,
            },
            timeout=60,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content", "")
        parsed = json.loads(content)
        insights = {
            item["code"]: item["insight"]
            for item in parsed.get("insights", [])
            if item.get("code") and item.get("insight")
        }
        status["returned"] = len(insights)
        status["status"] = "success" if len(insights) == len(watching_rows) else "partial"
        if status["status"] == "partial":
            status["reason"] = f"returned {len(insights)} of {len(watching_rows)} insights"
        return insights, status
    except Exception as exc:
        status["status"] = "failed"
        status["reason"] = f"{type(exc).__name__}: {str(exc)[:300]}"
        return {}, status


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
            if not should_track_quant_row(row, prev_state):
                continue
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
                     or r.get("model2_setup_signal") in {"PULLBACK_BUY", "RETEST_BUY"}
                     or (r.get("model2_stage") == "VCP_EARLY"
                         and safe_float(r.get("structure_score")) >= 60)],
    }

    # ── LLM 解读：为重点观察标的生成自然语言洞察 ──
    watching = sections.get("watching", [])
    llm_status = {
        "status": "skipped",
        "reason": "no watching rows",
        "requested": 0,
        "returned": 0,
    }
    if watching:
        insights, llm_status = call_llm_insights(watching, results)
        for r in watching:
            code = r.get("code", "")
            if code in insights:
                r["llm_insight"] = insights[code]

    persisted_rows = [r for r in rows if r.get("bloom_status") != "EXIT"]

    summary = {
        "date": run_date,
        "mode": mode,
        "input_total": meta.get("total"),
        "result_total": len(results),
        "state_total": len(persisted_rows),
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
        "llm": llm_status,
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


def _format_contraction_detail(cc, pcts, days):
    """拼接触收缩详情行。将百分比和天数逐段嵌入，如：
    ↳ 3段：-21.34%（18天） -> -7.83%（3天） -> -6.93%（5天）
    """
    pct_parts = [p.strip() for p in pcts.split("->")]
    day_parts = [d.strip() for d in days.split("->")] if days else []

    segments = []
    for i, p in enumerate(pct_parts):
        if i < len(day_parts):
            segments.append(f"{p}（{day_parts[i]}天）")
        else:
            segments.append(p)

    return f"↳ {cc}段：" + " -> ".join(segments)


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
    llm = summary.get("llm") or {}
    llm_status = llm.get("status", "unknown")
    llm_requested = llm.get("requested", 0)
    llm_returned = llm.get("returned", 0)
    if llm_status == "success":
        llm_text = f"LLM 观察要点已生成 {llm_returned}/{llm_requested}。"
    elif llm_status == "partial":
        llm_text = f"LLM 观察要点部分生成 {llm_returned}/{llm_requested}，其余使用规则兜底。"
    elif llm_status == "failed":
        llm_text = f"LLM 观察要点生成失败，已使用规则兜底（{llm.get('reason', 'unknown')}）。"
    elif llm_status == "skipped":
        llm_text = f"LLM 观察要点已跳过，使用规则兜底（{llm.get('reason', 'unknown')}）。"
    else:
        llm_text = f"LLM 观察要点状态未知，使用规则兜底。"

    lines.extend([
        "## 📊 今日概要",
        "",
        f"模型二扫描 {summary.get('input_total')} 只 → 产出 {summary.get('result_total')} 只。"
        f"Bloom 活跃观察 **{active}** 只，{alert_text}。"
        f"新进入 {new_n} 只，移出 {exit_n} 只。",
        llm_text,
        "",
    ])

    # ── 🔥 重点观察 ──
    watching = sections.get("watching", [])
    lines.append("## 🔥 重点观察")
    if watching:
        watch_headers = ["代码", "名称", "结构", "Bloom", "分", "风险", "收缩", "观察要点"]
        watch_sep = ["---"] * len(watch_headers)
        lines.append("| " + " | ".join(watch_headers) + " |")
        lines.append("| " + " | ".join(watch_sep) + " |")

        def _mark_risk(rl):
            return f"⚠️{rl}" if rl in ("HIGH", "HARD") else rl

        for r in watching:
            risk = _mark_risk(r.get("risk_level", ""))
            cc = r.get("contraction_count", "") or ""
            pcts = r.get("contraction_pcts", "") or ""
            days = r.get("contraction_days", "") or ""
            insight = r.get("llm_insight") or r.get("watch_reason", "")

            main = [
                r.get("code", ""), r.get("name", ""),
                r.get("model2_stage", ""), r.get("bloom_status", ""),
                r.get("structure_score", ""), risk,
                cc, insight,
            ]
            lines.append("| " + " | ".join(str(c) for c in main) + " |")

            if cc and pcts:
                detail = _format_contraction_detail(cc, pcts, days)
                sub = [""] * 7 + [detail]
                lines.append("| " + " | ".join(sub) + " |")
    else:
        lines.extend(["", "*今日无符合条件的结构*", ""])
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


def _load_dotenv():
    """加载本地 .env 中的 KEY=VALUE，不覆盖已有环境变量。"""
    env_path = Path(PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def main():
    _load_dotenv()

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

    llm_status = (bloom.get("summary", {}).get("llm") or {}).get("status")
    if llm_status == "failed":
        sys.exit(3)


if __name__ == "__main__":
    main()
