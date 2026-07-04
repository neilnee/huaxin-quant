#!/usr/bin/env python3
"""
Huaxin Quant daily bloom review.

This layer runs after model 1 and model 2. It does not change model output.
It stores bloom observation history, updates the current bloom state, and
generates a compact review input plus a human-readable daily report.
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


QUANT_RUNS_DIR = Path(PROJECT_ROOT) / "cache" / "quant_runs"
BLOOM_DIR = Path(PROJECT_ROOT) / "bloom"
REVIEW_INPUT_DIR = Path(PROJECT_ROOT) / "cache" / "reviews"
DAILY_REPORT_DIR = Path(PROJECT_ROOT) / "reports" / "daily"

BLOOM_EVENTS_PATH = BLOOM_DIR / "bloom_events.jsonl"
BLOOM_STATE_PATH = BLOOM_DIR / "bloom_state.csv"

ACTIVE_STATUSES = {"early", "forming", "mature", "retest"}
EVENT_BUCKETS = {
    "new_entry": "new_entries",
    "upgrade": "upgrades",
    "downgrade": "downgrades",
    "invalidated": "invalidated",
    "continued": "continued",
    "data_issue": "data_issues",
    "ignored": "ignored",
}
STATUS_RANK = {
    "rejected": 0,
    "data_issue": 0,
    "invalid": 0,
    "breakout": 1,
    "early": 2,
    "forming": 3,
    "mature": 4,
    "retest": 5,
}

STATE_FIELDS = [
    "code",
    "name",
    "first_seen",
    "last_seen",
    "days_tracked",
    "days_in_observation",
    "active_status",
    "last_state",
    "last_vcp_stage",
    "last_pool_type",
    "last_score",
    "score_change",
    "best_status",
    "best_score",
    "best_date",
    "vcp_quality",
    "watch_priority",
    "contraction_count",
    "contraction_pcts",
    "volume_pattern",
    "pivot_price",
    "pivot_distance",
    "last_contraction_low",
    "structure_age_days",
    "structure_valid",
    "structure_invalid_reason",
    "risk_flags",
    "close",
    "MA20",
    "MA60",
    "last_reason",
    "exit_reason",
]


def ensure_dirs():
    BLOOM_DIR.mkdir(parents=True, exist_ok=True)
    REVIEW_INPUT_DIR.mkdir(parents=True, exist_ok=True)
    DAILY_REPORT_DIR.mkdir(parents=True, exist_ok=True)


def load_local_env():
    env_path = Path(PROJECT_ROOT) / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


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
    if value is None:
        return ""
    if isinstance(value, float):
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def as_list(value):
    if isinstance(value, list):
        return value
    if value in (None, ""):
        return []
    return [str(value)]


def classify_result(row):
    state = row.get("state") or ""
    stage = row.get("vcp_stage") or ""
    pool_type = row.get("pool_type") or ""

    if stage == "DATA_INSUFFICIENT" or state == "DATA_INSUFFICIENT":
        return "data_issue"
    if pool_type == "REJECT":
        if stage == "POST_BREAKOUT":
            return "breakout"
        if stage == "TREND_REBUILD" or row.get("structure_valid") is False:
            return "invalid"
        return "rejected"
    if state == "P3_RETEST" or stage == "P3_RETEST":
        return "retest"
    if state in {"P1_TIGHT", "P1_HIGH", "P1_MATURE"} or stage in {"P1_TIGHT", "P1_HIGH", "P1_MATURE"}:
        return "mature"
    if state == "P1_FORMING" or stage == "P1_FORMING":
        return "forming"
    if state == "P1_EARLY" or stage == "P1_EARLY":
        return "early"
    if stage == "POST_BREAKOUT":
        return "breakout"
    if stage == "TREND_REBUILD" or row.get("structure_valid") is False:
        return "invalid"
    if state == "REJECT":
        return "rejected"
    return "rejected"


def is_active_status(status):
    return status in ACTIVE_STATUSES


def status_rank(status):
    return STATUS_RANK.get(status, 0)


def read_state():
    if not BLOOM_STATE_PATH.exists():
        return {}
    rows = {}
    with open(BLOOM_STATE_PATH, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            code = row.get("code", "").strip()
            if code:
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
        0 if is_active_status(r.get("active_status")) else 1,
        -safe_float(r.get("last_score")),
        r.get("code", ""),
    ))
    with open(BLOOM_STATE_PATH, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=STATE_FIELDS)
        writer.writeheader()
        writer.writerows(ordered)


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


def compact_candidate(row, bloom_status=None, event_type=None):
    return {
        "code": row.get("code"),
        "name": row.get("name"),
        "state": row.get("state"),
        "vcp_stage": row.get("vcp_stage"),
        "pool_type": row.get("pool_type"),
        "bloom_status": bloom_status if bloom_status is not None else classify_result(row),
        "event_type": event_type,
        "setup_score": row.get("setup_score"),
        "risk_score": row.get("risk_score"),
        "vcp_quality": row.get("vcp_quality"),
        "watch_priority": row.get("watch_priority"),
        "contraction_count": row.get("contraction_count"),
        "contraction_pcts": row.get("contraction_pcts"),
        "contraction_days": row.get("contraction_days"),
        "volume_pattern": row.get("volume_pattern"),
        "pivot_price": row.get("pivot_price"),
        "pivot_distance": row.get("pivot_distance"),
        "last_contraction_low": row.get("last_contraction_low"),
        "structure_age_days": row.get("structure_age_days"),
        "structure_valid": row.get("structure_valid"),
        "structure_invalid_reason": row.get("structure_invalid_reason"),
        "risk_flags": as_list(row.get("risk_flags")),
        "close": row.get("close"),
        "MA20": row.get("MA20"),
        "MA60": row.get("MA60"),
        "reason": row.get("reason"),
    }


def state_from_quant_results(results, date_iso):
    state = {}
    for row in results:
        status = classify_result(row)
        code = row.get("code")
        if not code:
            continue
        state[code] = {
            "code": code,
            "name": row.get("name", ""),
            "first_seen": date_iso,
            "last_seen": date_iso,
            "days_tracked": "1",
            "days_in_observation": "1" if is_active_status(status) else "0",
            "active_status": status,
            "last_state": row.get("state", ""),
            "last_vcp_stage": row.get("vcp_stage", ""),
            "last_pool_type": row.get("pool_type", ""),
            "last_score": fmt_num(row.get("setup_score")),
            "score_change": "",
            "best_status": status,
            "best_score": fmt_num(row.get("setup_score")),
            "best_date": date_iso,
            "vcp_quality": row.get("vcp_quality", ""),
            "watch_priority": row.get("watch_priority", ""),
            "contraction_count": fmt_num(row.get("contraction_count")),
            "contraction_pcts": row.get("contraction_pcts", ""),
            "volume_pattern": row.get("volume_pattern", ""),
            "pivot_price": fmt_num(row.get("pivot_price")),
            "pivot_distance": fmt_num(row.get("pivot_distance")),
            "last_contraction_low": fmt_num(row.get("last_contraction_low")),
            "structure_age_days": fmt_num(row.get("structure_age_days")),
            "structure_valid": str(row.get("structure_valid", "")),
            "structure_invalid_reason": row.get("structure_invalid_reason", ""),
            "risk_flags": ";".join(as_list(row.get("risk_flags"))),
            "close": fmt_num(row.get("close")),
            "MA20": fmt_num(row.get("MA20")),
            "MA60": fmt_num(row.get("MA60")),
            "last_reason": row.get("reason", ""),
            "exit_reason": "" if is_active_status(status) else row.get("reason", ""),
        }
    return state


def event_type(prev_row, curr_status):
    prev_status = prev_row.get("active_status") if prev_row else None
    prev_active = is_active_status(prev_status)
    curr_active = is_active_status(curr_status)
    if curr_status == "data_issue":
        return "data_issue"
    if not prev_active and curr_active:
        return "new_entry"
    if prev_active and curr_active:
        if status_rank(curr_status) > status_rank(prev_status):
            return "upgrade"
        if status_rank(curr_status) < status_rank(prev_status):
            return "downgrade"
        return "continued"
    if prev_active and not curr_active:
        return "invalidated"
    return "ignored"


def update_state_row(prev_row, row, date_iso, curr_status, event):
    prev_row = prev_row or {}
    prev_score = safe_float(prev_row.get("last_score"), None)
    curr_score = safe_float(row.get("setup_score"), 0.0)
    best_score = safe_float(prev_row.get("best_score"), -1.0)
    best_status = prev_row.get("best_status", "")
    best_date = prev_row.get("best_date", "")
    if curr_score >= best_score:
        best_score = curr_score
        best_status = curr_status
        best_date = date_iso

    days_tracked = safe_int(prev_row.get("days_tracked")) + 1 if prev_row else 1
    days_in_observation = safe_int(prev_row.get("days_in_observation"))
    if is_active_status(curr_status):
        days_in_observation += 1

    return {
        "code": row.get("code", ""),
        "name": row.get("name") or prev_row.get("name", ""),
        "first_seen": prev_row.get("first_seen") or date_iso,
        "last_seen": date_iso,
        "days_tracked": str(days_tracked),
        "days_in_observation": str(days_in_observation),
        "active_status": curr_status,
        "last_state": row.get("state", ""),
        "last_vcp_stage": row.get("vcp_stage", ""),
        "last_pool_type": row.get("pool_type", ""),
        "last_score": fmt_num(curr_score),
        "score_change": "" if prev_score is None else fmt_num(curr_score - prev_score),
        "best_status": best_status,
        "best_score": fmt_num(best_score),
        "best_date": best_date,
        "vcp_quality": row.get("vcp_quality", ""),
        "watch_priority": row.get("watch_priority", ""),
        "contraction_count": fmt_num(row.get("contraction_count")),
        "contraction_pcts": row.get("contraction_pcts", ""),
        "volume_pattern": row.get("volume_pattern", ""),
        "pivot_price": fmt_num(row.get("pivot_price")),
        "pivot_distance": fmt_num(row.get("pivot_distance")),
        "last_contraction_low": fmt_num(row.get("last_contraction_low")),
        "structure_age_days": fmt_num(row.get("structure_age_days")),
        "structure_valid": str(row.get("structure_valid", "")),
        "structure_invalid_reason": row.get("structure_invalid_reason", ""),
        "risk_flags": ";".join(as_list(row.get("risk_flags"))),
        "close": fmt_num(row.get("close")),
        "MA20": fmt_num(row.get("MA20")),
        "MA60": fmt_num(row.get("MA60")),
        "last_reason": row.get("reason", ""),
        "exit_reason": row.get("reason", "") if event in {"invalidated", "data_issue"} else "",
    }


def missing_active_event(prev_row, date_iso):
    row = dict(prev_row)
    row["last_seen"] = date_iso
    row["days_tracked"] = str(safe_int(row.get("days_tracked")) + 1)
    row["active_status"] = "data_issue"
    row["exit_reason"] = "今日模型二结果缺失，可能为 API 失败或输入池缺失"
    return row


def build_review(payload, previous_payload, date_yy, allow_partial=False):
    meta = payload.get("meta", {})
    run_date = meta.get("run_date") or date_yy_to_iso(date_yy)
    mode = meta.get("mode", "")
    if mode != "全量" and not allow_partial:
        raise RuntimeError(f"daily review requires full quant run, got mode={mode!r}; use --allow-partial to bypass")

    results = payload.get("results", [])
    prev_state = read_state_before(run_date)
    if not prev_state and previous_payload:
        prev_date = previous_payload.get("meta", {}).get("run_date") or ""
        prev_state = state_from_quant_results(previous_payload.get("results", []), prev_date)

    by_code = {r.get("code"): r for r in results if r.get("code")}
    new_state = dict(prev_state)
    events = []
    buckets = {
        "new_entries": [],
        "upgrades": [],
        "downgrades": [],
        "invalidated": [],
        "continued": [],
        "data_issues": [],
        "ignored": [],
    }

    for code, row in by_code.items():
        curr_status = classify_result(row)
        prev_row = prev_state.get(code)
        event = event_type(prev_row, curr_status)
        if event != "ignored" or is_active_status(curr_status):
            new_state[code] = update_state_row(prev_row, row, run_date, curr_status, event)

        item = compact_candidate(row, curr_status, event)
        buckets.setdefault(EVENT_BUCKETS.get(event, event), []).append(item)
        if event != "ignored":
            events.append({
                "date": run_date,
                "event": event,
                "code": code,
                "name": row.get("name", ""),
                "bloom_status": curr_status,
                "state": row.get("state", ""),
                "vcp_stage": row.get("vcp_stage", ""),
                "setup_score": row.get("setup_score"),
                "vcp_quality": row.get("vcp_quality"),
                "watch_priority": row.get("watch_priority"),
                "reason": row.get("reason", ""),
            })

    for code, prev_row in prev_state.items():
        if code in by_code or not is_active_status(prev_row.get("active_status")):
            continue
        updated = missing_active_event(prev_row, run_date)
        new_state[code] = updated
        item = {
            "code": code,
            "name": prev_row.get("name", ""),
            "bloom_status": "data_issue",
            "event_type": "data_issue",
            "reason": updated["exit_reason"],
        }
        buckets["data_issues"].append(item)
        events.append({
            "date": run_date,
            "event": "data_issue",
            "code": code,
            "name": prev_row.get("name", ""),
            "bloom_status": "data_issue",
            "reason": updated["exit_reason"],
        })

    active = [
        compact_candidate(r, classify_result(r))
        for r in results
        if is_active_status(classify_result(r))
    ]
    active.sort(key=lambda r: safe_float(r.get("setup_score")), reverse=True)
    need_review = [
        r for r in active
        if r.get("volume_pattern") in {"failed", "mixed"} or as_list(r.get("risk_flags")) or r.get("structure_valid") is False
    ][:12]

    summary = {
        "date": run_date,
        "mode": mode,
        "input_total": meta.get("total"),
        "result_total": len(results),
        "stats": payload.get("stats", {}),
        "active_total": len(active),
        "new_entries": len(buckets["new_entries"]),
        "upgrades": len(buckets["upgrades"]),
        "downgrades": len(buckets["downgrades"]),
        "invalidated": len(buckets["invalidated"]),
        "continued": len(buckets["continued"]),
        "data_issues": len(buckets["data_issues"]),
    }

    review = {
        "meta": {
            "schema": "huaxin_bloom_review_v1",
            "date": run_date,
            "date_yy": date_yy,
            "quant_run": str(QUANT_RUNS_DIR / f"quant_{date_yy}.json"),
        },
        "summary": summary,
        "top_candidates": active[:20],
        "new_entries": sorted(buckets["new_entries"], key=lambda r: safe_float(r.get("setup_score")), reverse=True),
        "upgrades": sorted(buckets["upgrades"], key=lambda r: safe_float(r.get("setup_score")), reverse=True),
        "downgrades": sorted(buckets["downgrades"], key=lambda r: safe_float(r.get("setup_score")), reverse=True),
        "invalidated": buckets["invalidated"],
        "continued": sorted(buckets["continued"], key=lambda r: safe_float(r.get("setup_score")), reverse=True)[:30],
        "data_issues": buckets["data_issues"],
        "need_human_review": need_review,
    }
    return review, new_state, events


def write_review_input(date_yy, review):
    path = REVIEW_INPUT_DIR / f"review_input_{date_yy}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(review, f, ensure_ascii=False, indent=2)
    return path


def table_lines(rows, columns):
    if not rows:
        return ["无"]
    header = "| " + " | ".join(title for _, title in columns) + " |"
    sep = "| " + " | ".join("---" for _ in columns) + " |"
    lines = [header, sep]
    for row in rows:
        vals = []
        for key, _ in columns:
            val = row.get(key, "")
            if isinstance(val, list):
                val = ";".join(str(x) for x in val)
            vals.append(str(val) if val is not None else "")
        lines.append("| " + " | ".join(vals) + " |")
    return lines


def default_markdown(review):
    summary = review["summary"]
    lines = [
        f"# Huaxin Daily Review {summary['date']}",
        "",
        "## 今日概览",
        f"- 模型二输入标的：{summary.get('input_total')} 只",
        f"- 模型二输出结果：{summary.get('result_total')} 只",
        f"- bloom 当前活跃观察：{summary.get('active_total')} 只",
        f"- 新进入：{summary.get('new_entries')} 只；升级：{summary.get('upgrades')} 只；降级：{summary.get('downgrades')} 只；失效：{summary.get('invalidated')} 只；数据问题：{summary.get('data_issues')} 只",
        "",
        "## 今日重点观察",
    ]
    columns = [
        ("code", "代码"),
        ("name", "名称"),
        ("bloom_status", "状态"),
        ("state", "模型状态"),
        ("setup_score", "分数"),
        ("vcp_quality", "质量"),
        ("watch_priority", "优先级"),
        ("contraction_pcts", "收缩"),
        ("pivot_distance", "距pivot"),
        ("reason", "脚本结论"),
    ]
    lines.extend(table_lines(review["top_candidates"][:12], columns))

    sections = [
        ("新进入观察", "new_entries"),
        ("状态升级", "upgrades"),
        ("状态降级", "downgrades"),
        ("结构失效或退出观察", "invalidated"),
        ("数据问题", "data_issues"),
        ("需要人工看图复核", "need_human_review"),
    ]
    for title, key in sections:
        lines.extend(["", f"## {title}"])
        lines.extend(table_lines(review.get(key, [])[:20], columns))

    lines.extend([
        "",
        "## 明日观察原则",
        "- early：继续观察是否出现第二、第三轮收缩，暂不当作成熟结构。",
        "- forming：重点看最近收缩低点是否守住、量能是否继续下降、是否接近 pivot。",
        "- mature/retest：等待突破确认或突破后缩量回踩，不因单日强势追认买点。",
        "- invalid/breakout：不再当作当前正在形成的 VCP，等待重新构建结构。",
        "",
        "> 本报告由脚本基于模型二结构化结果生成；LLM 如启用，只负责解释，不改变模型判定。",
    ])
    return "\n".join(lines) + "\n"


def call_llm_markdown(review):
    import requests

    api_key = os.environ.get("DEEPSEEK_API_KEY")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    if not api_key:
        return None, {"status": "skipped", "reason": "DEEPSEEK_API_KEY missing"}

    system_prompt = (
        "你是 Huaxin Quant 的每日花期复盘助手。只解释输入 JSON 中已有的模型一/模型二结果，"
        "不得改变 state、vcp_stage、pool_type，不得自造价格、成交量或财务数据。"
        "输出 Markdown，结构包括：今日概览、重点观察、新进入/升级/失效、需要人工看图、明日观察清单。"
    )
    user_prompt = json.dumps(review, ensure_ascii=False)
    try:
        resp = requests.post(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "stream": False,
                "temperature": 0.2,
                "max_tokens": 3500,
            },
            timeout=90,
        )
        resp.raise_for_status()
        data = resp.json()
        content = data["choices"][0]["message"].get("content", "").strip()
        if not content:
            raise ValueError("empty LLM response")
        return content + "\n", {"status": "ok", "provider": "deepseek", "model": model, "usage": data.get("usage", {})}
    except Exception as exc:
        return None, {"status": "failed", "provider": "deepseek", "model": model, "reason": str(exc)}


def write_markdown(date_yy, markdown):
    path = DAILY_REPORT_DIR / f"review_{date_yy}.md"
    with open(path, "w", encoding="utf-8") as f:
        f.write(markdown)
    return path


def main():
    parser = argparse.ArgumentParser(description="Huaxin Quant 每日花期复盘层")
    parser.add_argument("--date", help="复盘日期，支持 YYMMDD 或 YYYY-MM-DD；默认取最新 quant run")
    parser.add_argument("--with-llm", action="store_true", help="调用 DeepSeek 生成 Markdown 复盘")
    parser.add_argument("--allow-partial", action="store_true", help="允许单股/多股/测试模式生成复盘")
    parser.add_argument("--no-state-update", action="store_true", help="只生成 review_input 和 Markdown，不更新 bloom 状态层")
    args = parser.parse_args()

    load_local_env()
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
        review, new_state, events = build_review(payload, previous_payload, date_yy, allow_partial=args.allow_partial)
    except Exception as exc:
        print(f"错误: {exc}")
        sys.exit(1)

    if not args.no_state_update:
        write_state(new_state)
        write_events(review["summary"]["date"], events)

    input_path = write_review_input(date_yy, review)

    llm_status = {"status": "skipped", "reason": "not_requested"}
    markdown = None
    if args.with_llm:
        markdown, llm_status = call_llm_markdown(review)
    if markdown is None:
        markdown = default_markdown(review)
    report_path = write_markdown(date_yy, markdown)

    print("=" * 70)
    print("Huaxin Daily Review")
    print("=" * 70)
    print(f"quant run: {quant_path}")
    print(f"previous: {prev_path if prev_path else 'none'}")
    print(f"review input: {input_path}")
    print(f"daily report: {report_path}")
    print(f"bloom state: {BLOOM_STATE_PATH}")
    print(f"bloom events: {BLOOM_EVENTS_PATH}")
    print(f"LLM: {llm_status}")
    print(f"summary: {review['summary']}")


if __name__ == "__main__":
    main()
