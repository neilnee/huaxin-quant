#!/usr/bin/env python3
"""Model 3 v3: auditable, stage-driven single-stock valuation pipeline.

The pipeline deliberately fails closed: an LLM can only return a research card
backed by the evidence snapshot prepared by this script.  It never writes the
final report directly.
"""

import argparse
import csv
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib import error, request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.calc_valuation import (
    calc_pe_three_step,
    generate_ranking_row,
    generate_report_fragments,
    parse_params,
    reverse_check,
    run_valuation,
    update_ranking_csv,
)
from scripts.dashboard_valuation import merge_priced_independent_items
from scripts.data.market_data_service import MarketDataService
from scripts.shared import PROJECT_ROOT, VALUATION_INDEX_PATH, VALUATION_REPORTS_DIR, expected_trade_date
from scripts.strategy_config import load_strategy_config

ROOT = Path(PROJECT_ROOT)
BRIEFING_DIR = ROOT / "cache" / "briefing"
RUNS_DIR = ROOT / "cache" / "valuation_runs"
RESEARCH_DIR = ROOT / "cache" / "research"
CALC_PARAMS_DIR = ROOT / "cache" / "calc_params"
CALC_RESULTS_DIR = ROOT / "cache" / "calc_results"
MX_SEARCH_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/news-search"
MARKET_DATA_CONFIG, _ = load_strategy_config("market-regime.json")

SEARCH_TOPICS = (
    ("business", "{name} {code} 2025年报 主营业务 营收构成"),
    ("consensus", "{name} {code} 券商研报 盈利预测 2026 2027 目标价"),
    ("announcements", "{name} {code} 2026 公告 订单 中标 签约"),
    ("management", "{name} {code} 2026 增持 回购 股权激励 投资者交流"),
    ("market", "{name} {code} 最新股价 总市值"),
)


class PipelineError(RuntimeError):
    pass


def load_dotenv():
    path = ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def checkpoint(run_dir, stage, status="done", **extra):
    path = run_dir / "manifest.json"
    manifest = read_json(path) if path.exists() else {"version": "valuation-v3", "code": run_dir.name[:6], "status": "running", "stages": {}}
    manifest.setdefault("stages", {})[stage] = {"status": status, "updated_at": datetime.now().isoformat(timespec="seconds"), **extra}
    manifest["status"] = "running" if status != "failed" else "failed"
    write_json(path, manifest)
    subprocess.run([sys.executable, "scripts/dashboard_valuation.py", "--progress"], cwd=ROOT, capture_output=True)


def checkpoint_current(stage, status="running", **extra):
    value = os.environ.get("VALUATION_PROGRESS_RUN_DIR")
    if value:
        checkpoint(Path(value), stage, status, **extra)


def merge_manifest_stages(disk_manifest, local_manifest):
    """Merge independently checkpointed and locally accumulated stages by progress rank."""
    rank = {"pending": 0, "running": 1, "skipped": 2, "done": 3, "failed": 4}
    merged = dict(local_manifest.get("stages", {}))
    for key, value in disk_manifest.get("stages", {}).items():
        current = merged.get(key)
        disk_rank = rank.get(value.get("status"), -1)
        local_rank = rank.get(current.get("status"), -1) if current else -1
        disk_updated = str(value.get("updated_at") or "")
        local_updated = str(current.get("updated_at") or "") if current else ""
        if current is None or disk_rank > local_rank or (disk_rank == local_rank and disk_updated > local_updated):
            merged[key] = value
    disk_manifest["stages"] = merged
    return disk_manifest


def read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"无法读取 JSON {path}: {exc}") from exc


def latest_briefing(code):
    files = sorted(BRIEFING_DIR.glob(f"{code}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        raise PipelineError(f"找不到 {code} 的 briefing；先运行 python3 scripts/valuate.py --code {code}")
    return files[0], read_json(files[0])


def name_from_index(code):
    path = Path(VALUATION_INDEX_PATH)
    if not path.exists():
        return ""
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            stored = row.get("股票代码", "").replace('="', "").replace('"', "")
            if stored == code:
                return str(row.get("股票名称", "")).strip()
    return ""


def current_market_quote(code, name, as_of=None, service=None):
    """Load the latest valid close from the canonical market-data service."""
    target_date = as_of or expected_trade_date()
    market_service = service or MarketDataService(MARKET_DATA_CONFIG)
    frames, status = market_service.get_daily_bars([(code, name)], target_date, 1)
    frame = frames.get(code)
    if frame is None or frame.empty:
        reason = status.get(code, {}).get("error") or "无行情数据"
        raise PipelineError(f"{code} 当前价获取失败: {reason}")
    row = frame.iloc[-1]
    quote_date = str(row.get("date", ""))[:10]
    price = _number(row.get("close"), "market_quote.close")
    if quote_date != target_date:
        raise PipelineError(f"{code} 行情过期: 期望 {target_date}，实际 {quote_date or '未知'}")
    if price <= 0:
        raise PipelineError(f"{code} 最新收盘价必须大于零")
    return {
        "current_price": round(price, 4),
        "price_date": quote_date,
        "price_source": str(row.get("source") or status.get(code, {}).get("source") or "market_data_service"),
    }


def text_excerpt(value, limit=None):
    text = json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value)
    text = re.sub(r"\s+", " ", text).strip()
    return text if limit is None else text[:limit]


def search_items(raw):
    """Return search result rows from the mx-search cache shape, if present."""
    current = raw
    # MXSearch.format_pretty uses raw.data.data.llmSearchResponse.data.
    # Keep this narrow so an unexpected envelope is rejected rather than treated
    # as proof that a search returned no evidence.
    for key in ("data", "data", "llmSearchResponse", "data"):
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current if isinstance(current, list) else None


def latest_result_date(items):
    dates = []
    for item in items or []:
        value = item.get("date") if isinstance(item, dict) else None
        if value:
            try:
                dates.append(datetime.strptime(str(value)[:10], "%Y-%m-%d").date())
            except ValueError:
                pass
    return max(dates).isoformat() if dates else None


def evidence_from_cache(code, name, evidence_dir, allowed_paths=None):
    """Collect only successful cached search responses; failed responses stay in manifest."""
    entries, rejected = [], []
    tokens = [code, name]
    allowed = {Path(path).resolve() for path in allowed_paths} if allowed_paths else None
    seen_results = {}
    for path in sorted(evidence_dir.glob("*.json")):
        if allowed is not None and path.resolve() not in allowed:
            continue
        if not any(token and token in path.name for token in tokens):
            continue
        try:
            raw = read_json(path)
        except PipelineError as exc:
            rejected.append({"path": str(path), "reason": str(exc)})
            continue
        if isinstance(raw, dict) and raw.get("success") is False:
            rejected.append({"path": str(path), "reason": raw.get("message", "search_failed"),
                             "status": raw.get("status")})
            continue
        items = search_items(raw)
        if items == []:
            rejected.append({"path": str(path), "reason": "search_empty"})
            continue
        published_at = latest_result_date(items)
        if "研报" in path.name or "consensus" in path.name:
            if not published_at:
                rejected.append({"path": str(path), "reason": "research_date_missing"})
                continue
            if datetime.strptime(published_at, "%Y-%m-%d").date() < datetime.now().date() - timedelta(days=183):
                rejected.append({"path": str(path), "reason": "research_stale", "published_at": published_at})
                continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:12]
        for index, item in enumerate(items or []):
            item_date = str(item.get("date", ""))[:10] if isinstance(item, dict) else None
            result_key = (str(item.get("code", "")) if isinstance(item, dict) else "",
                          str(item.get("title", "")) if isinstance(item, dict) else "",
                          item_date or "")
            source_id = f"ev_{digest}_{index + 1:02d}"
            duplicate_of = seen_results.get(result_key)
            entry = {
                "source_id": source_id,
                "source_type": str(item.get("informationType", "search_result")).lower() if isinstance(item, dict) else "search_result",
                "title": str(item.get("title", path.stem)) if isinstance(item, dict) else path.stem,
                "published_at": item_date or published_at,
                "cache_path": str(path.relative_to(ROOT)),
                "result_index": index,
                # Keep the complete result text in the audit package.  Nodes receive
                # only their selected topic set, never a global short excerpt.
                "excerpt": text_excerpt(item),
            }
            if duplicate_of:
                entry["duplicate_of"] = duplicate_of
            else:
                seen_results[result_key] = source_id
            entries.append(entry)
    return entries, rejected


def build_evidence(briefing_path, briefing, code, name, evidence_dir, allowed_paths=None):
    external, rejected = evidence_from_cache(code, name, evidence_dir, allowed_paths)
    digest = hashlib.sha256(briefing_path.read_bytes()).hexdigest()[:12]
    evidence = [{
        "source_id": f"briefing_{digest}", "source_type": "briefing",
        "title": briefing_path.name, "published_at": briefing.get("_meta", {}).get("data_date"),
        "cache_path": str(briefing_path.relative_to(ROOT)), "excerpt": text_excerpt(briefing),
    }] + external
    return evidence, rejected


def preserve_cached_reading_sources(run_dir, evidence):
    """Keep source IDs used by reused LLM readings auditable across cache refreshes."""
    existing = {item["source_id"] for item in evidence}
    preserved = []

    def collect(value, relative_path):
        if isinstance(value, dict):
            rows = value.get("source_readings")
            if isinstance(rows, list):
                for row in rows:
                    if not isinstance(row, dict) or not row.get("source_id") or row["source_id"] in existing:
                        continue
                    existing.add(row["source_id"])
                    preserved.append({"source_id": row["source_id"], "source_type": "cached_llm_reading",
                                      "title": f"复用阅读结论 · {relative_path}", "published_at": None,
                                      "cache_path": relative_path, "excerpt": text_excerpt(row),
                                      "preserved_from_completed_stage": True})
            for child in value.values():
                collect(child, relative_path)
        elif isinstance(value, list):
            for child in value:
                collect(child, relative_path)

    for filename in ("stage_1_readings.json", "stage_2_readings.json", "stage_3_readings.json"):
        path = run_dir / filename
        if path.exists():
            collect(read_json(path), str(path.relative_to(ROOT)))
    evidence.extend(preserved)
    return len(preserved)


def search_cache_path(evidence_dir, code, topic):
    return evidence_dir / f"valuation_search_{code}_{topic}.json"


def cached_search_usable(path, max_age_hours):
    if not path.exists():
        return False
    try:
        raw = read_json(path)
    except PipelineError:
        return False
    if not isinstance(raw, dict) or raw.get("success") is False or search_items(raw) in (None, []):
        return False
    age_seconds = datetime.now().timestamp() - path.stat().st_mtime
    return age_seconds <= max_age_hours * 3600


def fetch_one_search(query, output_path, retries=3):
    api_key = os.environ.get("MX_APIKEY", "").strip()
    if not api_key:
        raise PipelineError("未配置 MX_APIKEY；无法执行模型三脚本化检索")
    payload = json.dumps({"query": query}).encode("utf-8")
    last_error = "unknown"
    for attempt in range(1, retries + 1):
        req = request.Request(MX_SEARCH_URL, data=payload,
                              headers={"apikey": api_key, "Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=45) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except (error.URLError, error.HTTPError, json.JSONDecodeError, OSError) as exc:
            last_error = str(exc)
        else:
            if isinstance(raw, dict) and (raw.get("status") in (0, None) or raw.get("code") == 0):
                write_json(output_path, raw)
                if search_items(raw):
                    return {"status": "done", "query": query, "path": str(output_path.relative_to(ROOT)), "attempt": attempt}
                return {"status": "empty", "query": query, "path": str(output_path.relative_to(ROOT)), "attempt": attempt}
            write_json(output_path, raw)
            last_error = str(raw.get("message", raw.get("status", "search_failed")))
        if attempt < retries:
            time.sleep(min(2 ** attempt, 8))
    return {"status": "failed", "query": query, "path": str(output_path.relative_to(ROOT)), "attempt": retries, "reason": last_error}


def fetch_evidence(code, name, evidence_dir, refresh, max_age_hours, dry_run):
    """Fetch the fixed evidence checklist; failures are returned, never hidden."""
    evidence_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for topic, template in SEARCH_TOPICS:
        query = template.format(code=code, name=name)
        output_path = search_cache_path(evidence_dir, code, topic)
        if not refresh and cached_search_usable(output_path, max_age_hours):
            results.append({"status": "cached", "topic": topic, "query": query,
                            "path": str(output_path.relative_to(ROOT))})
            continue
        if dry_run:
            results.append({"status": "planned", "topic": topic, "query": query,
                            "path": str(output_path.relative_to(ROOT))})
            continue
        item = fetch_one_search(query, output_path)
        item["topic"] = topic
        results.append(item)
        time.sleep(1.2)
    return results


def parse_json_object(content):
    content = content.strip()
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise PipelineError("LLM 未返回 JSON 对象")
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise PipelineError(f"LLM JSON 无法解析: {exc}") from exc
    if not isinstance(parsed, dict):
        raise PipelineError("LLM 输出必须为 JSON 对象")
    return parsed


def select_topic_evidence(evidence, *topics):
    """Return complete result-level evidence for one research topic."""
    selected = []
    for item in evidence:
        if item.get("duplicate_of"):
            continue
        if item["source_type"] == "briefing":
            selected.append(item)
            continue
        path = item.get("cache_path", "").lower()
        if any(topic.lower() in path for topic in topics):
            selected.append(item)
    return selected


def llm_stage(stage, task, inputs, evidence, include_full_evidence=True, max_tokens=None):
    """Run one bounded research stage; evidence is full-text and topic-scoped."""
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        raise PipelineError(f"未配置 DEEPSEEK_API_KEY；{stage} 无法执行")
    source_ids = [item["source_id"] for item in evidence]
    prompt = (
        f"你是模型三估值的 {stage} 节点。{task}\n"
        "只能使用下列输入和证据；所有事实、数值、判断、查询计划和参数映射必须附 source_ids。"
        "不得压缩或跳过旧指令卡要求的研究步骤，不得使用记忆或联网补数。"
        "先在内部完成推理，但最终只输出紧凑、合法的 JSON 对象：不要 Markdown、不要推理过程、"
        "不要示例、不要注释、不要省略号或伪 JSON；字符串须简短，数组只保留决策所需字段。\n"
        f"允许 source_id: {source_ids}\n"
        f"输入:\n{json.dumps(inputs, ensure_ascii=False)}\n"
        f"本节点{'完整专题证据' if include_full_evidence else '证据目录'}:\n{json.dumps(evidence, ensure_ascii=False)}"
    )
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")
    payload = json.dumps({
        "model": os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"),
        "response_format": {"type": "json_object"},
        "temperature": 0.1,
        "max_tokens": int(max_tokens or os.environ.get("VALUATION_LLM_MAX_TOKENS", "16000")),
        "messages": [{"role": "system", "content": "Return valid JSON only."}, {"role": "user", "content": prompt}],
    }).encode("utf-8")
    req = request.Request(f"{base_url}/chat/completions", data=payload,
                          headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"})
    # Each node is independently auditable and retriable.  Keeping the raw
    # response is essential when a provider returns an empty/non-JSON answer:
    # a failed stage must be diagnosable without silently weakening the task.
    trace_dir_value = os.environ.get("VALUATION_LLM_TRACE_DIR", "").strip()
    trace_dir = Path(trace_dir_value) if trace_dir_value else None
    trace_path = None
    if trace_dir:
        trace_dir.mkdir(parents=True, exist_ok=True)
        readable = re.sub(r"[^a-zA-Z0-9]+", "_", stage).strip("_")[:40] or "llm_stage"
        slug = f"{readable}_{hashlib.sha256(stage.encode('utf-8')).hexdigest()[:10]}"
        trace_path = trace_dir / f"{slug}.json"
    last_error = "unknown"
    attempts = int(os.environ.get("VALUATION_LLM_RETRIES", "2"))
    for attempt in range(1, attempts + 1):
        record = {"stage": stage, "attempt": attempt, "started_at": datetime.now().isoformat(),
                  "evidence_count": len(evidence), "source_ids": source_ids, "request_bytes": len(payload)}
        try:
            with request.urlopen(req, timeout=int(os.environ.get("VALUATION_LLM_TIMEOUT_SECONDS", "180"))) as response:
                response_text = response.read().decode("utf-8")
                record["http_status"] = response.status
            record["response_text"] = response_text
            record["response_bytes"] = len(response_text.encode("utf-8"))
            body = json.loads(response_text)
            content = body["choices"][0]["message"]["content"]
            result = parse_json_object(content)
            record["status"] = "done"
            if trace_path:
                write_json(trace_path.with_name(f"{trace_path.stem}_attempt{attempt}.json"), record)
            return result
        except error.HTTPError as exc:
            last_error = f"HTTP {exc.code}: {exc.reason}"
            record["status"] = "failed"; record["error"] = last_error; record["http_status"] = exc.code
            try:
                record["response_text"] = exc.read().decode("utf-8", errors="replace")
                record["response_bytes"] = len(record["response_text"].encode("utf-8"))
            except OSError:
                pass
            if trace_path:
                write_json(trace_path.with_name(f"{trace_path.stem}_attempt{attempt}.json"), record)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 8))
        except (error.URLError, KeyError, IndexError, json.JSONDecodeError, OSError, PipelineError) as exc:
            last_error = str(exc)
            record["status"] = "failed"; record["error"] = last_error
            if trace_path:
                write_json(trace_path.with_name(f"{trace_path.stem}_attempt{attempt}.json"), record)
            if attempt < attempts:
                time.sleep(min(2 ** attempt, 8))
    raise PipelineError(f"{stage} LLM 调用失败（已重试 {attempts} 次）: {last_error}")


def evidence_catalog(evidence):
    return [{key: item.get(key) for key in ("source_id", "source_type", "title", "published_at", "cache_path", "result_index")} for item in evidence]


def read_evidence_batches(stage, task, inputs, evidence, batch_size=4):
    """Read every result in bounded full-text batches before synthesis."""
    readings = []
    run_dir_value = os.environ.get("VALUATION_PROGRESS_RUN_DIR", "").strip()
    batch_dir = Path(run_dir_value) / "batch_readings" if run_dir_value else None
    stage_slug = hashlib.sha256(stage.encode("utf-8")).hexdigest()[:10]
    for start in range(0, len(evidence), batch_size):
        batch_number = start // batch_size + 1
        total_batches = (len(evidence) + batch_size - 1) // batch_size
        batch_path = batch_dir / f"{stage_slug}_{batch_number:03d}.json" if batch_dir else None
        checkpoint_current(stage, "running", current_batch=batch_number, completed_batches=batch_number - 1, total_batches=total_batches)
        batch = evidence[start:start + batch_size]
        expected_ids = [item["source_id"] for item in batch]
        cached = read_json(batch_path) if batch_path and batch_path.exists() else None
        if isinstance(cached, dict) and cached.get("source_ids") == expected_ids and isinstance(cached.get("reading"), dict):
            reading = cached["reading"]
        else:
            reading = llm_stage(
                f"{stage}·原文阅读 {batch_number}",
                task + "逐条完整阅读本批证据；输出 source_readings[]，每条保留 source_id、关键事实、数值、项目/业务归属、时间、可用于估值的含义和不确定性。不得遗漏本批任何 source_id。",
                inputs, batch,
            )
            if batch_path:
                write_json(batch_path, {"stage": stage, "batch": batch_number, "source_ids": expected_ids, "reading": reading})
        readings.append(reading)
        checkpoint_current(stage, "running", current_batch=batch_number, completed_batches=batch_number, total_batches=total_batches)
    if evidence:
        checkpoint_current(stage, "done", completed_batches=len(readings), total_batches=len(readings))
    return readings


def dynamic_search_plan(stage_one, code, name):
    """Validate the business node's project-specific search plan."""
    plan = stage_one.get("search_plan", []) if isinstance(stage_one, dict) else []
    required = [
        {"topic": "announcements", "query": f"{name} {code} 公告 2026 订单 中标 签约", "reason": "路径B时间差"},
        {"topic": "management", "query": f"{name} {code} 增持 回购 股权激励 投资者交流", "reason": "路径D管理层"},
        {"topic": "capital_operations", "query": f"{name} {code} 资产平台 资产证券化 扩募 并购 重组 资产处置 公告",
         "reason": "通用资本运作与独立事项完整性检查"},
    ]
    results, seen = [], set()
    for item in required + plan:
        if not isinstance(item, dict):
            continue
        query = str(item.get("query", "")).strip()
        topic = re.sub(r"[^a-z0-9_]+", "_", str(item.get("topic", "project")).lower()).strip("_") or "project"
        if not query or query in seen:
            continue
        if name not in query or code not in query:
            raise PipelineError(f"专题检索必须同时包含公司名和代码: {query}")
        results.append({"topic": topic[:40], "query": query, "reason": str(item.get("reason", ""))})
        seen.add(query)
    if len(results) > 24:
        raise PipelineError("阶段一专题检索计划超过 24 项；请先合并事实、估值路径和验证节点完全相同的重复项目")
    return results


def fetch_dynamic_evidence(plan, code, evidence_dir, refresh, max_age_hours, dry_run, allow_fetch=True):
    results = []
    for index, item in enumerate(plan, 1):
        slug = re.sub(r"[^a-z0-9]+", "_", item["topic"].lower()).strip("_") or "project"
        output_path = evidence_dir / f"valuation_search_{code}_stage3_{index:02d}_{slug}.json"
        if not refresh and cached_search_usable(output_path, max_age_hours):
            results.append({"status": "cached", **item, "path": str(output_path.relative_to(ROOT))})
        elif not allow_fetch and output_path.exists() and search_items(read_json(output_path)):
            # Offline resume deliberately accepts an older successful snapshot.
            # Its provenance remains in the run package; no network call occurs.
            results.append({"status": "cached_offline", **item, "path": str(output_path.relative_to(ROOT))})
        elif not allow_fetch:
            results.append({"status": "missing_offline", **item, "path": str(output_path.relative_to(ROOT))})
        elif dry_run:
            results.append({"status": "planned", **item, "path": str(output_path.relative_to(ROOT))})
        else:
            fetched = fetch_one_search(item["query"], output_path)
            results.append({**item, **fetched})
            time.sleep(1.2)
    return results


def normalize_research_ids(card):
    """Assign stable IDs to stage-five research items without changing research content."""
    prefixes = {"business_pillars": "pillar", "market_divergences": "divergence", "narrative_options": "option"}
    for collection, prefix in prefixes.items():
        seen = set()
        for index, item in enumerate(card.get(collection, []) or [], 1):
            if not isinstance(item, dict):
                continue
            research_id = str(item.get("research_id") or f"{prefix}_{index:02d}").strip()
            if research_id in seen:
                research_id = f"{prefix}_{index:02d}"
            item["research_id"] = research_id
            seen.add(research_id)


def normalize_stage_five_research(card):
    """Normalize harmless shape omissions while retaining hierarchical provenance."""
    normalize_research_ids(card)
    for pillar in card.get("business_pillars", []) or []:
        if not isinstance(pillar, dict):
            continue
        drivers = pillar.get("valuation_drivers")
        if isinstance(drivers, str):
            pillar["valuation_drivers"] = [drivers]
        parent_sources = list(pillar.get("source_ids", []) or [])
        bridge = pillar.get("profit_bridge", {})
        bridge.setdefault("profit_metric", "gross_profit")
        for year in ("items_2026e", "items_2027e"):
            for item in bridge.get(year, []) or []:
                if isinstance(item, dict) and not item.get("source_ids") and parent_sources:
                    item["source_ids"] = parent_sources
                    item["source_inherited_from"] = pillar.get("research_id")
    for item in card.get("narrative_options", []) or []:
        if isinstance(item, dict) and item.get("included_in_valuation") is False and item.get("pe") is None:
            item["pe"] = 0


def assemble_stage_five(research, mapping):
    """Deterministically join the narrative and numeric stage-five contracts by research_id."""
    if not isinstance(research, dict) or not isinstance(mapping, dict):
        raise PipelineError("阶段五 A/B 输出必须为对象")
    normalize_research_ids(research)
    result = json.loads(json.dumps(research, ensure_ascii=False))
    valuation_inputs = mapping.get("valuation_inputs")
    if not isinstance(valuation_inputs, dict):
        raise PipelineError("阶段五B缺少 valuation_inputs")
    result["valuation_inputs"] = valuation_inputs
    specs = (
        ("business_pillars", "pillar_mappings"),
        ("market_divergences", "divergence_mappings"),
        ("narrative_options", "option_mappings"),
    )
    for collection, mapping_key in specs:
        items = result.get(collection, []) or []
        mappings = mapping.get(mapping_key)
        if not isinstance(mappings, list):
            raise PipelineError(f"阶段五B缺少 {mapping_key}")
        by_id = {str(item.get("research_id")): item for item in mappings if isinstance(item, dict)}
        expected = {str(item.get("research_id")) for item in items if isinstance(item, dict)}
        if set(by_id) != expected:
            missing = sorted(expected - set(by_id)); extra = sorted(set(by_id) - expected)
            raise PipelineError(f"{mapping_key} research_id 不匹配；缺少={missing}，多余={extra}")
        for item in items:
            patch = by_id[item["research_id"]]
            for field in ("classification", "profit_timing", "accounting_treatment", "calculation_mapping"):
                if patch.get(field) is None:
                    raise PipelineError(f"{mapping_key}.{item['research_id']} 缺少 {field}")
                item[field] = patch[field]
    result["status"] = "ready"
    return result


def validate_stage_five_mapping(mapping, research):
    """Validate stage five B before it can be persisted or assembled."""
    inputs = mapping.get("valuation_inputs")
    required_inputs = ("growth_quality", "market_position", "comparable_pe_lower", "comparable_pe_median",
                       "comparable_pe_upper", "company_growth_rate", "comp_growth_rate", "qualitative_adjustments",
                       "stage2_recommended_pe", "selected_pe", "pe_selection_method", "pe_selection_reason",
                       "pe_scope_basis", "excluded_anchor_names", "source_ids")
    if not isinstance(inputs, dict):
        raise PipelineError("阶段五B缺少 valuation_inputs")
    missing = [field for field in required_inputs if inputs.get(field) in (None, "") or (field == "source_ids" and not inputs.get(field))]
    if missing:
        raise PipelineError("阶段五B valuation_inputs 缺少: " + ", ".join(missing))
    if inputs["growth_quality"] not in {"structural", "cyclical", "mature"}:
        raise PipelineError("阶段五B growth_quality 必须为标准枚举")
    if inputs["market_position"] not in {"leader", "mid", "follower"}:
        raise PipelineError("阶段五B market_position 必须为标准枚举")
    for field in ("company_growth_rate", "comp_growth_rate"):
        value = _number(inputs[field], f"valuation_inputs.{field}")
        if not 1 <= value <= 300:
            raise PipelineError(f"阶段五B {field} 必须为百分比数值")
    specs = (("business_pillars", "pillar_mappings"), ("market_divergences", "divergence_mappings"),
             ("narrative_options", "option_mappings"))
    pillar_names = {item["research_id"]: item.get("name") for item in research.get("business_pillars", []) or []}
    pillar_profit = 0.0
    for collection, mapping_key in specs:
        expected = {item["research_id"] for item in research.get(collection, []) or []}
        rows = mapping.get(mapping_key)
        if not isinstance(rows, list) or {item.get("research_id") for item in rows if isinstance(item, dict)} != expected:
            raise PipelineError(f"阶段五B {mapping_key} research_id 不完整")
        research_by_id = {item["research_id"]: item for item in research.get(collection, []) or []}
        for item in rows:
            label = f"{mapping_key}.{item.get('research_id')}"
            if item.get("classification") not in CLASSIFICATIONS or item.get("profit_timing") not in PROFIT_TIMINGS or item.get("accounting_treatment") not in ACCOUNTING_TREATMENTS:
                raise PipelineError(f"{label} 三个研究枚举无效")
            calc = item.get("calculation_mapping")
            if not isinstance(calc, dict) or calc.get("target") not in MAPPING_TARGETS:
                raise PipelineError(f"{label}.calculation_mapping.target 无效")
            target = calc["target"]
            if collection == "market_divergences":
                if item.get("driver_type") not in {"profit", "multiple", "margin", "other"}:
                    raise PipelineError(f"{label}.driver_type 无效")
                if target != "exclude":
                    raise PipelineError(f"{label} 分歧必须反映在基准模型利润/倍数/概率情景中，不得另加Layer2市值")
                if target != "exclude" and pillar_names.get(calc.get("pillar_id")) != item.get("pillar_name"):
                    raise PipelineError(f"{label} pillar_id 与 pillar_name 不匹配")
            if collection == "business_pillars" and target not in {"pillar", "exclude"}:
                raise PipelineError(f"{label} 只能映射 pillar|exclude")
            if collection != "business_pillars" and target not in {"layer2", "type_b_pipeline", "layer3_item", "exclude"}:
                raise PipelineError(f"{label} 映射目标无效")
            if collection == "narrative_options" and research_by_id[item["research_id"]].get("included_in_valuation") is False and target != "exclude":
                raise PipelineError(f"{label} 未计入估值项目必须 exclude")
            if (collection == "narrative_options" and int(research.get("contract_version") or 0) < 5
                    and research_by_id[item["research_id"]].get("included_in_valuation") is True):
                if target == "exclude":
                    raise PipelineError(f"{label} 已通过概率事项门禁，不得在阶段五降级为 exclude")
                if research_by_id[item["research_id"]].get("valuation_role") == "probabilistic_event" and target != "type_b_pipeline":
                    raise PipelineError(f"{label} 概率事项必须映射为统一表中的 type_b_pipeline 独立支柱")
            if target == "exclude":
                continue
            if not calc.get("pillar_id") or not isinstance(calc.get("engine_values"), dict):
                raise PipelineError(f"{label} 缺少 pillar_id/engine_values")
            values = calc["engine_values"]
            if target == "pillar":
                route = calc.get("route")
                if route not in VALUATION_ROUTES:
                    raise PipelineError(f"{label}.route 无效")
                missing_route = [field for field in ROUTE_ENGINE_FIELDS[route] if values.get(field) is None]
                if missing_route:
                    raise PipelineError(f"{label} 路线 {route} 缺少: " + ", ".join(missing_route))
                if route in {"A1", "B", "C"}:
                    pillar_profit += _number(values["consensus_np_2026e"], f"{label}.consensus_np_2026e")
            elif target == "layer2" and any(values.get(field) is None for field in ("pessimistic", "base", "optimistic", "description")):
                raise PipelineError(f"{label} layer2 字段不完整")
            elif target == "type_b_pipeline" and any(values.get(field) is None for field in ("project_profit", "pe", "probability")):
                raise PipelineError(f"{label} type_b_pipeline 字段不完整")
            elif target == "layer3_item" and any(values.get(field) is None for field in ("pessimistic", "base", "optimistic", "probability")):
                raise PipelineError(f"{label} layer3_item 字段不完整")
    institution_values = [_number(item.get("np_2026e"), "consensus.np_2026e") for item in research.get("consensus", []) or []]
    if int(research.get("contract_version") or 0) < 5 and pillar_profit and institution_values:
        consensus_mean = sum(institution_values) / len(institution_values)
        if abs(pillar_profit - consensus_mean) / consensus_mean > 0.35:
            raise PipelineError(f"阶段五B支柱2026E净利合计 {pillar_profit:.2f} 亿偏离机构共识均值 {consensus_mean:.2f} 亿超过35%")


def normalize_pe_scope_fallback(inputs, stage_two):
    """Prevent cross-check-only anchors from masquerading as direct PE inputs."""
    if not isinstance(inputs, dict) or not isinstance(stage_two, dict):
        return
    if int(stage_two.get("contract_version") or 0) >= 5:
        return
    forecasts = [item for item in stage_two.get("institution_forecasts", []) or [] if isinstance(item, dict)]
    usable = [item for item in forecasts
              if item.get("pe_usability") in {"direct_usable", "convertible"} and item.get("pe_2026e") is not None]
    numeric_comparables = [item for item in stage_two.get("comparable_companies", []) or []
                           if isinstance(item, dict) and item.get("pe") is not None]
    cross_checks = [item for item in forecasts
                    if item.get("pe_usability") == "cross_check_only" and item.get("pe_2026e") is not None]
    excluded = list(inputs.get("excluded_anchor_names") or [])
    for item in cross_checks:
        name = str(item.get("institution") or "未命名机构锚")
        if not any(name in str(existing) for existing in excluded):
            excluded.append(name)
    inputs["excluded_anchor_names"] = excluded
    if len(usable) + len(numeric_comparables) >= 2:
        return
    pe_values = sorted(_number(item.get("pe_2026e"), "cross_check.pe_2026e") for item in cross_checks)
    if len(pe_values) < 3:
        raise PipelineError("同口径PE锚不足2项，且交叉检查PE不足3项，无法形成保守回退倍数")
    lower_half = pe_values[: (len(pe_values) + 1) // 2]
    fallback = round(_median(lower_half), 2)
    inputs.update({
        "comparable_pe_lower": round(fallback * 0.9, 2),
        "comparable_pe_median": fallback,
        "comparable_pe_upper": round(fallback * 1.2, 2),
        "stage2_recommended_pe": fallback,
        "selected_pe": fallback,
        "pe_selection_method": "override",
        "pe_override": fallback,
        "pe_selection_reason": (
            f"同口径直接/可转换PE锚与数值公司可比合计不足2项；"
            f"脚本以{len(pe_values)}个cross_check_only PE的保守下半区中位数{fallback:.2f}x显式回退，"
            "不将其表述为机构目标估值"
        ),
        "pe_scope_basis": "持续经营公司级利润；cross_check_only仅用于锚不足时的保守区间回退，不作为直接可用锚",
    })
    sources = list(inputs.get("source_ids") or [])
    for item in cross_checks:
        for source_id in item.get("source_ids") or []:
            if source_id not in sources:
                sources.append(source_id)
    inputs["source_ids"] = sources


def normalize_stage_five_mapping(mapping, research, stage_two=None):
    """Apply deterministic cross-item constraints that need no research judgement."""
    inputs = mapping.get("valuation_inputs") if isinstance(mapping.get("valuation_inputs"), dict) else {}
    normalize_pe_scope_fallback(inputs, stage_two)
    for adjustment in inputs.get("qualitative_adjustments", []) or []:
        if isinstance(adjustment, dict) and not adjustment.get("item") and adjustment.get("reason"):
            adjustment["item"] = adjustment.pop("reason")
    if inputs.get("pe_selection_method") == "three_step":
        mechanical = calc_pe_three_step(
            inputs.get("comparable_pe_median"), inputs.get("comparable_pe_lower"), inputs.get("comparable_pe_upper"),
            inputs.get("company_growth_rate"), inputs.get("comp_growth_rate"), inputs.get("growth_quality"),
            inputs.get("qualitative_adjustments") or [],
        )
        selected = _number(inputs.get("selected_pe"), "valuation_inputs.selected_pe")
        mechanical_pe = mechanical.get("final_pe")
        if mechanical_pe and abs(mechanical_pe - selected) / selected > 0.05:
            inputs["pe_selection_method"] = "override"
            inputs["pe_override"] = selected
            inputs["pe_selection_reason"] = (
                f"{inputs.get('pe_selection_reason') or ''}；机械三步走为{mechanical_pe:.1f}x，"
                f"与已清洗同口径PE {selected:.1f}x 不一致，按研究选择显式覆盖"
            )
    research_options = {item.get("research_id"): item for item in research.get("narrative_options", []) or []}
    for item in mapping.get("option_mappings", []) or []:
        source = research_options.get(item.get("research_id"), {})
        if source.get("included_in_valuation") is False:
            item["calculation_mapping"] = {"target": "exclude"}
    for item in mapping.get("divergence_mappings", []) or []:
        if isinstance(item, dict):
            item["calculation_mapping"] = {"target": "exclude"}


def repair_stage_five_mapping(mapping, research, evidence, reason):
    """Use one compact repair node for schema-valid but semantically invalid mappings."""
    repaired = llm_stage("阶段五B：参数映射契约修复", (
        f"原映射未通过门禁：{reason}。只修复映射，不改研究事实，只返回完整阶段五B对象。"
        "valuation_inputs 字段名必须精确为 growth_quality(structural|cyclical|mature),market_position(leader|mid|follower),"
        "comparable_pe_lower,comparable_pe_median,comparable_pe_upper,company_growth_rate,comp_growth_rate,qualitative_adjustments,"
        "stage2_recommended_pe,selected_pe,pe_selection_method(three_step|override),pe_selection_reason,pe_scope_basis,excluded_anchor_names,source_ids；增长率用41而非0.41。"
        "PE只可使用同利润/估值范围的direct_usable锚或已可靠转换的convertible锚，cross_check_only必须列入excluded_anchor_names。"
        "如最终明确采用机构/阶段二建议附近的PE，使用 override 并令 pe_override=selected_pe。"
        "每条映射都必须给标准枚举：classification=core_operating|asset_pipeline|narrative_option|non_recurring；"
        "profit_timing=realized|contracted|pipeline|long_term；accounting_treatment=recurring|non_recurring|consolidated。"
        "A1/B/C engine_values 字段必须精确为 consensus_np_2026e,consensus_np_2027e,consensus_np_lower_2026e,"
        "consensus_np_upper_2026e,consensus_np_lower_2027e,consensus_np_upper_2027e,deducted_np；各支柱净利合计必须接近公司级机构共识，不得把毛利润当净利润。"
        "所有分歧映射必须给 driver_type=profit|multiple|margin|other 且target=exclude，不得再添加Layer2市值；未计入估值的 narrative_options 必须 target=exclude。"
        "exclude 仍须保留三个标准枚举，calculation_mapping={target:'exclude'}。非exclude项目必须引用已存在的主营 pillar_id。"
    ), {"invalid_mapping": mapping, "stage_5_research": research}, evidence_catalog(evidence), False)
    return repaired


def validate_stage_five_research(research, evidence):
    """Fail before mapping when the narrative contract is not render-safe."""
    if research.get("status") not in {"ready", "complete"}:
        raise PipelineError("阶段五A研究状态不是 ready")
    valid_ids = {item["source_id"] for item in evidence}
    thesis = research.get("investment_thesis")
    if not isinstance(thesis, dict) or not all(thesis.get(key) for key in ("market_trade", "company_stage", "core_judgement", "source_ids")):
        raise PipelineError("阶段五A缺少结构化 investment_thesis")
    require_sources([thesis], "investment_thesis", valid_ids)
    for key in ("facts", "assumptions", "risks", "catalysts"):
        _validate_statement_collection(research, key, valid_ids)
    for index, pillar in enumerate(research.get("business_pillars", []) or []):
        if not isinstance(pillar, dict) or not pillar.get("research_id"):
            raise PipelineError(f"阶段五A business_pillars[{index}] 缺少 research_id")
        missing = [field for field in ("name", "business_essence", "split_rationale", "source_ids") if not pillar.get(field)]
        if missing:
            raise PipelineError(f"阶段五A business_pillars[{index}] 缺少: " + ", ".join(missing))
        require_sources([pillar], f"business_pillars[{index}]", valid_ids)
        bridge = pillar.get("profit_bridge")
        if not isinstance(bridge, dict):
            raise PipelineError(f"阶段五A business_pillars[{index}] 缺少 profit_bridge")
        _canonical_billion(bridge.get("base_2025a"), f"business_pillars[{index}].profit_bridge.base_2025a")
        for year in ("items_2026e", "items_2027e"):
            require_sources(bridge.get(year), f"business_pillars[{index}].profit_bridge.{year}", valid_ids)
            for item_index, item in enumerate(bridge.get(year, [])):
                _canonical_billion(item.get("profit_impact"), f"business_pillars[{index}].profit_bridge.{year}[{item_index}].profit_impact")
    if len(research.get("consensus", []) or []) < 3:
        raise PipelineError("阶段五A逐机构共识不足3家")
    for index, item in enumerate(research.get("consensus", []) or []):
        if item.get("pe_usability") == "cross_check_only" and not item.get("valuation_scope"):
            item["valuation_scope"] = "unknown"
        consensus_fields = (("institution", "report_date", "np_2026e", "np_2027e", "profit_scope", "scope_reason", "source_ids")
                            if int(research.get("contract_version") or 0) >= 5 else
                            ("institution", "report_date", "np_2026e", "np_2027e", "profit_scope",
                             "valuation_scope", "pe_usability", "scope_reason", "source_ids"))
        missing = [field for field in consensus_fields
                   if item.get(field) in (None, "", [])]
        if missing:
            raise PipelineError(f"阶段五A consensus[{index}] 缺少: " + ", ".join(missing))
        require_sources([item], f"consensus[{index}]", valid_ids)
    if int(research.get("contract_version") or 0) < 5 and len(research.get("comparables", []) or []) < 2:
        raise PipelineError("阶段五A估值锚不足2项")
    for collection, fields in (
        ("market_divergences", ("research_id", "name", "pillar", "bull_case", "bear_case", "root_cause", "our_judgement", "pricing_status", "source_ids")),
        ("narrative_options", ("research_id", "name", "pillar", "business_essence", "pricing_status", "potential_profit", "pe", "probability", "included_in_valuation", "verification_nodes", "source_ids")),
    ):
        for index, item in enumerate(research.get(collection, []) or []):
            missing = [field for field in fields if item.get(field) in (None, "", [])]
            if missing:
                raise PipelineError(f"阶段五A {collection}[{index}] 缺少: " + ", ".join(missing))
            require_sources([item], f"{collection}[{index}]", valid_ids)
            if collection == "narrative_options":
                role = item.get("valuation_role")
                if role not in {"probabilistic_event", "narrative_observation"}:
                    raise PipelineError(f"阶段五A narrative_options[{index}].valuation_role 无效")
                if role == "probabilistic_event":
                    if item.get("included_in_valuation") is not True or item.get("incremental_to_baseline") is not True:
                        raise PipelineError(f"阶段五A narrative_options[{index}] 概率事项必须增量计价")
                    if item.get("quantification_status") != "quantitative" or not isinstance(item.get("valuation_scenarios"), dict):
                        raise PipelineError(f"阶段五A narrative_options[{index}] 概率事项缺少量化情景")
                elif item.get("included_in_valuation") is not False:
                    raise PipelineError(f"阶段五A narrative_options[{index}] 叙事观察项不得计价")
    for index, item in enumerate(research.get("verification_nodes", []) or []):
        missing = [field for field in ("node_id", "timeframe", "event", "pillar", "success_meaning", "failure_meaning", "source_ids") if item.get(field) in (None, "", [])]
        if missing:
            raise PipelineError(f"阶段五A verification_nodes[{index}] 缺少: " + ", ".join(missing))
        require_sources([item], f"verification_nodes[{index}]", valid_ids)


def repair_stage_five_thesis(research, evidence):
    """Repair only a scalar thesis into the required render-safe object."""
    thesis = research.get("investment_thesis")
    if isinstance(thesis, dict):
        return research
    if not isinstance(thesis, str) or not thesis.strip():
        return research
    referenced = set()

    def collect(value):
        if isinstance(value, dict):
            referenced.update(str(item) for item in value.get("source_ids", []) if item)
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(research)
    catalog = [item for item in evidence_catalog(evidence) if item["source_id"] in referenced]
    repaired = llm_stage("阶段五A：投资主线契约修复", (
        "只把原 investment_thesis 字符串拆为结构化对象，不得修改或新增事实。"
        "只输出 investment_thesis={market_trade,company_stage,core_judgement,source_ids}；"
        "source_ids 必须来自允许目录，三个文本字段均不得为空。"
    ), {"investment_thesis": thesis, "referenced_source_ids": sorted(referenced)}, catalog, False)
    if isinstance(repaired.get("investment_thesis"), dict):
        research["investment_thesis"] = repaired["investment_thesis"]
    return research


def build_stage_five_research(run_dir, briefing, stage_one, stage_two, stage_three, stage_four, evidence, resume):
    """Build stage five A from three bounded, independently resumable research parts."""
    catalog = evidence_catalog(evidence)

    def part(filename, checkpoint_key, stage, task, inputs):
        path = run_dir / filename
        manifest = read_json(run_dir / "manifest.json") if (run_dir / "manifest.json").exists() else {}
        stage_status = (manifest.get("stages", {}).get(checkpoint_key, {}) or {}).get("status")
        if resume and path.exists() and stage_status == "done":
            value = read_json(path)
            checkpoint(run_dir, checkpoint_key, "done", file=filename, reused=True)
            return value
        value = llm_stage(stage, task, inputs, catalog, False)
        write_json(path, value)
        checkpoint(run_dir, checkpoint_key, "done", file=filename)
        return value

    stage_two_baseline = read_json(run_dir / "stage_2_baseline.json")
    core = part("stage_5_research_core.json", "stage_5_research_core", "阶段五A1：业务地图与共识", (
        "不新增事实，禁止输出计算映射。输出 contract_version=5、investment_thesis、business_pillars、consensus、profit_analysis；comparables输出空数组。"
        "investment_thesis={market_trade,company_stage,core_judgement,source_ids}，绝对不能是字符串。"
        "business_pillars必须逐一继承stage_1_business完整业务地图，research_id原样使用阶段一pillar_id；业务地图只解释利润来源，不分配利润或估值。"
        "business_pillars 每项完整包含 research_id,name,business_essence,split_rationale,valuation_drivers,source_ids、"
        "classification,profit_timing,accounting_treatment 及 profit_bridge={profit_metric:net_profit|gross_profit|operating_profit,base_2025a,items_2026e:[{item,profit_impact,source_ids}],items_2027e:[...]}；"
        "profit_bridge只整理已披露信息；无法量化填0并写明未量化，不得把公司共识利润拆给业务支柱。"
        "金额统一亿元，原始元值除以1亿。"
        "consensus 至少3家，每项含 institution,report_date:YYYY-MM-DD,np_2026e,np_2027e,revenue_2026e,revenue_2027e,pe_2026e,pe_2027e,target_price,"
        "profit_scope,valuation_scope,pe_semantics_2026e,pe_semantics_2027e,target_pe_2026e,target_pe_2027e,scope_reason,source_ids。"
        "profit_analysis逐一继承阶段二A，含institution,report_date,np_2026e,np_2027e,profit_scope,summary,profit_drivers,key_assumptions,included_business_items,excluded_or_unclear_items,risks,source_ids，不得重新推演。"
    ), {"briefing": briefing, "stage_1_business": stage_one, "stage_2_consensus": stage_two,
        "stage_2_baseline": stage_two_baseline})
    options = part("stage_5_research_options.json", "stage_5_research_options", "阶段五A2：分歧与期权", (
        "不新增事实，禁止输出计算映射。只输出 market_divergences、narrative_options。"
        "market_divergences 每项含 research_id,name,pillar,bull_case,bear_case,root_cause,our_judgement,pricing_status,source_ids及三个标准枚举。"
        "非经常项目是否纳入属于口径差异，不得当作经营分歧；只有至少两家同口径机构对同一变量有原文支持的不同假设才保留。只看到预测数值不同不得反推根因。"
        "narrative_options必须逐一覆盖阶段三valuation_role为probabilistic_event或narrative_observation的项目，research_id必须原样等于project_id。"
        "每项含 research_id,name,pillar,business_essence,valuation_role,pricing_status,potential_profit,pe,probability,included_in_valuation,"
        "incremental_to_baseline,quantification_status,valuation_scenarios,verification_nodes,source_ids及三个标准枚举。"
        "probabilistic_event必须保留阶段三双年三情景参数、included_in_valuation=true且不得降级；"
        "narrative_observation必须included_in_valuation=false。利润亿元、概率0~1。"
    ), {"stage_1_business": stage_one, "stage_2_consensus": stage_two, "stage_3_expectations": stage_three})
    validation = part("stage_5_research_validation.json", "stage_5_research_validation", "阶段五A3：验证与事实", (
        "不新增事实，禁止输出计算映射。只输出 verification_nodes、facts、assumptions、risks、catalysts。catalysts固定为空数组，页面只保留verification_nodes与risks。"
        "verification_nodes 为扁平数组，每项={node_id,timeframe,event,pillar,success_meaning,failure_meaning,source_ids}，禁止嵌套 catalyst。"
        "facts/assumptions/risks/catalysts 必须为 {statement,source_ids} 对象数组，不得为字符串数组。"
    ), {"stage_3_expectations": stage_three, "stage_4_catalysts": stage_four})
    research = {"status": "ready", "contract_version": int(stage_two.get("contract_version") or 5), **core, **options, **validation}
    if int(research.get("contract_version") or 0) < 5:
        validate_stage_five_baseline_pillar_coverage(research, stage_two_baseline)
    validate_stage_three_research_coverage(stage_three, research)
    return research


def build_stage_five_mapping(run_dir, research, stage_two, stage_three, evidence, resume):
    """Build stage five B from bounded valuation, pillar, and adjustment mappings."""
    if int(stage_two.get("contract_version") or 0) >= 5:
        baseline = read_json(run_dir / "stage_2_baseline.json")
        company = (baseline.get("pillars") or [])[0]
        years = company.get("years", {})
        y26, y27 = years.get("2026e", {}), years.get("2027e", {})
        base26 = _number((y26.get("base") or {}).get("profit"), "stage2.2026e.base.profit")
        base27 = _number((y27.get("base") or {}).get("profit"), "stage2.2027e.base.profit")
        growth = max(1.0, min(300.0, (base27 / base26 - 1) * 100 if base26 else 1.0))
        sources = list(company.get("source_ids") or [])
        pe_values = {scenario: _number((y26.get(scenario) or {}).get("multiple"), f"stage2.2026e.{scenario}.multiple")
                     for scenario in ("pessimistic", "base", "optimistic")}
        legacy_pe_lower = min(pe_values.values())
        legacy_pe_upper = max(pe_values.values())
        valuation_inputs = {
            "growth_quality": "structural" if growth >= 20 else "mature", "market_position": "mid",
            "comparable_pe_lower": legacy_pe_lower, "comparable_pe_median": pe_values["base"],
            "comparable_pe_upper": legacy_pe_upper, "company_growth_rate": round(growth, 2),
            "comp_growth_rate": round(growth, 2), "qualitative_adjustments": [],
            "stage2_recommended_pe": pe_values["base"], "selected_pe": pe_values["base"],
            "pe_selection_method": "override", "pe_override": pe_values["base"],
            "pe_selection_reason": "仅为旧计算引擎适配；页面与最终估值直接采用阶段二机构一致性预期，不在阶段五重选PE",
            "pe_scope_basis": "公司整体机构目标估值组合", "excluded_anchor_names": [], "source_ids": sources,
        }
        engine_values = {
            "consensus_np_2026e": base26, "consensus_np_2027e": base27,
            "consensus_np_lower_2026e": _number((y26.get("pessimistic") or {}).get("profit"), "stage2.2026e.pessimistic.profit"),
            "consensus_np_upper_2026e": _number((y26.get("optimistic") or {}).get("profit"), "stage2.2026e.optimistic.profit"),
            "consensus_np_lower_2027e": _number((y27.get("pessimistic") or {}).get("profit"), "stage2.2027e.pessimistic.profit"),
            "consensus_np_upper_2027e": _number((y27.get("optimistic") or {}).get("profit"), "stage2.2027e.optimistic.profit"),
            "deducted_np": 0,
        }
        pillar_mappings = []
        for index, item in enumerate(research.get("business_pillars", []) or []):
            common = {
                "research_id": item["research_id"], "classification": item.get("classification") or "core_operating",
                "profit_timing": item.get("profit_timing") or "realized",
                "accounting_treatment": item.get("accounting_treatment") or "recurring",
            }
            common["calculation_mapping"] = ({"target": "pillar", "pillar_id": item["research_id"], "route": "A1", "engine_values": engine_values}
                                                 if index == 0 else {"target": "exclude"})
            pillar_mappings.append(common)
        divergence_mappings = [{
            "research_id": item["research_id"], "classification": item.get("classification") or "core_operating",
            "profit_timing": item.get("profit_timing") or "realized", "accounting_treatment": item.get("accounting_treatment") or "recurring",
            "driver_type": "other", "calculation_mapping": {"target": "exclude"},
        } for item in research.get("market_divergences", []) or []]
        option_mappings = [{
            "research_id": item["research_id"], "classification": item.get("classification") or "narrative_option",
            "profit_timing": item.get("profit_timing") or "long_term", "accounting_treatment": item.get("accounting_treatment") or "non_recurring",
            "calculation_mapping": {"target": "exclude"},
        } for item in research.get("narrative_options", []) or []]
        result = {"valuation_inputs": valuation_inputs, "pillar_mappings": pillar_mappings,
                  "divergence_mappings": divergence_mappings, "option_mappings": option_mappings}
        write_json(run_dir / "stage_5_mapping_valuation.json", {"valuation_inputs": valuation_inputs})
        write_json(run_dir / "stage_5_mapping_pillars.json", {"pillar_mappings": pillar_mappings})
        write_json(run_dir / "stage_5_mapping_adjustments_v3.json", {
            "contract_version": 5, "divergence_mappings": divergence_mappings, "option_mappings": option_mappings,
        })
        return result
    catalog = evidence_catalog(evidence)

    def part(filename, checkpoint_key, stage, task, inputs):
        path = run_dir / filename
        manifest = read_json(run_dir / "manifest.json") if (run_dir / "manifest.json").exists() else {}
        stage_status = (manifest.get("stages", {}).get(checkpoint_key, {}) or {}).get("status")
        if resume and path.exists() and stage_status == "done":
            value = read_json(path)
            checkpoint(run_dir, checkpoint_key, "done", file=filename, reused=True)
            return value
        value = llm_stage(stage, task, inputs, catalog, False)
        write_json(path, value)
        checkpoint(run_dir, checkpoint_key, "done", file=filename)
        return value

    valuation = part("stage_5_mapping_valuation.json", "stage_5_mapping_valuation", "阶段五B1：PE输入", (
        "只输出 valuation_inputs。字段名精确为 growth_quality(structural|cyclical|mature),market_position(leader|mid|follower),"
        "comparable_pe_lower,comparable_pe_median,comparable_pe_upper,company_growth_rate,comp_growth_rate,qualitative_adjustments,"
        "stage2_recommended_pe,selected_pe,pe_selection_method(three_step|override),pe_selection_reason,pe_scope_basis,"
        "excluded_anchor_names,source_ids。"
        "增长率用41表示41%，不能用0.41；调整数组可为空，单项范围-0.10~0.10。"
        "核心PE只能使用阶段二pe_usability=direct_usable的同口径锚，或使用完成独立事项扣除后的convertible锚；"
        "cross_check_only不得进入PE上下沿。pe_scope_basis说明利润与估值范围如何一致，excluded_anchor_names列出被排除机构。"
        "若明确采用机构或阶段二建议附近PE，使用override并令pe_override=selected_pe；不得让传统低增速可比隐式压低AIDC估值。"
    ), {"investment_thesis": research.get("investment_thesis"), "comparables": research.get("comparables"),
        "consensus": research.get("consensus"), "stage_2_consensus": stage_two})
    valuation_inputs = valuation.get("valuation_inputs") if isinstance(valuation.get("valuation_inputs"), dict) else valuation
    normalize_pe_scope_fallback(valuation_inputs, stage_two)
    write_json(run_dir / "stage_5_mapping_valuation.json", {"valuation_inputs": valuation_inputs})
    pillars = part("stage_5_mapping_pillars.json", "stage_5_mapping_pillars", "阶段五B2：支柱利润映射", (
        "只输出 pillar_mappings，逐一覆盖输入 research_id。每项={research_id,classification,profit_timing,accounting_treatment,calculation_mapping}。"
        "三个枚举只能使用标准英文值。calculation_mapping target只能pillar|exclude。"
        "A1/B/C字段精确为 consensus_np_2026e,consensus_np_2027e,consensus_np_lower_2026e,consensus_np_upper_2026e,"
        "consensus_np_lower_2027e,consensus_np_upper_2027e,deducted_np，单位亿元。"
        "公司级机构共识只能在支柱间分配一次：所有非排除支柱2026E净利之和须接近机构均值，禁止每个支柱重复填写公司总净利，禁止使用毛利润。"
    ), {"business_pillars": research.get("business_pillars"), "consensus": research.get("consensus"),
        "stage_2_consensus": stage_two})
    adjustments = part("stage_5_mapping_adjustments_v3.json", "stage_5_mapping_adjustments", "阶段五B3：分歧与期权映射", (
        "输出 contract_version=2、divergence_mappings 和 option_mappings，逐一覆盖 research_id，每项保留三个标准英文枚举和 calculation_mapping。"
        "每条 divergence_mapping 另给 driver_type=profit|multiple|margin|other。"
        "未计入估值的 narrative_options 必须 calculation_mapping={target:'exclude'}；included_in_valuation=true的probabilistic_event不得exclude，"
        "必须映射为type_b_pipeline，engine_values使用其2026E基准情景的project_profit,pe,probability。"
        "所有分歧必须target=exclude；其影响已经进入机构基准模型的三情景利润、倍数或概率，不得再用Layer2市值增减重复计价。"
        "期权若计入只允许 type_b_pipeline|layer3_item，并使用各自标准字段；无充分利润/PE证据则exclude。"
    ), {"valuation_inputs": valuation.get("valuation_inputs") or valuation, "business_pillars": research.get("business_pillars"),
        "pillar_mappings": pillars.get("pillar_mappings"),
        "market_divergences": research.get("market_divergences"), "narrative_options": research.get("narrative_options"),
        "stage_2_consensus": stage_two, "stage_3_expectations": stage_three})
    if valuation_inputs.get("pe_selection_method") == "override" and valuation_inputs.get("pe_override") is None:
        valuation_inputs["pe_override"] = valuation_inputs.get("selected_pe")
    pillar_rows = pillars.get("pillar_mappings", [])
    consensus_2026 = [_number(item.get("np_2026e"), "consensus.np_2026e") for item in research.get("consensus", []) or []]
    consensus_2027 = [_number(item.get("np_2027e"), "consensus.np_2027e") for item in research.get("consensus", []) or []]
    means = (sum(consensus_2026) / len(consensus_2026), sum(consensus_2027) / len(consensus_2027))
    for item in pillar_rows:
        calc = item.get("calculation_mapping", {})
        if calc.get("target") == "pillar":
            calc.setdefault("pillar_id", item.get("research_id"))
            if not isinstance(calc.get("engine_values"), dict) and isinstance(calc.get("fields"), dict):
                calc["engine_values"] = calc.pop("fields")
            if not isinstance(calc.get("engine_values"), dict):
                direct_fields = {
                    field: calc.pop(field) for field in ROUTE_ENGINE_FIELDS["A1"]
                    if calc.get(field) is not None
                }
                if len(direct_fields) == len(ROUTE_ENGINE_FIELDS["A1"]):
                    direct_fields["deducted_np"] = calc.pop("deducted_np", 0)
                    calc["engine_values"] = direct_fields
            if isinstance(calc.get("engine_values"), dict) and not calc.get("route"):
                calc["route"] = "A1"
        if calc.get("target") == "pillar" and not isinstance(calc.get("engine_values"), dict):
            weights = []
            for key in ("2026E", "2027E"):
                match = re.search(r"\*\s*([0-9.]+)", str(calc.get(key, "")))
                weights.append(float(match.group(1)) if match else None)
            if all(weight is not None for weight in weights):
                calc.update({"pillar_id": item.get("research_id"), "route": "A1", "engine_values": {
                    "consensus_np_2026e": round(means[0] * weights[0], 4),
                    "consensus_np_2027e": round(means[1] * weights[1], 4),
                    "consensus_np_lower_2026e": round(min(consensus_2026) * weights[0], 4),
                    "consensus_np_upper_2026e": round(max(consensus_2026) * weights[0], 4),
                    "consensus_np_lower_2027e": round(min(consensus_2027) * weights[1], 4),
                    "consensus_np_upper_2027e": round(max(consensus_2027) * weights[1], 4),
                    "deducted_np": 0,
                }})
        if item.get("classification") not in CLASSIFICATIONS:
            item["classification"] = "core_operating"
        if item.get("profit_timing") not in PROFIT_TIMINGS:
            item["profit_timing"] = "realized"
        if item.get("accounting_treatment") not in ACCOUNTING_TREATMENTS:
            item["accounting_treatment"] = "recurring"
    divergence_rows = adjustments.get("divergence_mappings", [])
    option_rows = adjustments.get("option_mappings", [])
    for rows, defaults in ((divergence_rows, ("core_operating", "realized", "recurring")),
                           (option_rows, ("narrative_option", "long_term", "non_recurring"))):
        for item in rows:
            calc = item.get("calculation_mapping", {})
            if item.get("pillar_id") and not calc.get("pillar_id"):
                calc["pillar_id"] = item["pillar_id"]
            if isinstance(item.get("engine_values"), dict) and not isinstance(calc.get("engine_values"), dict):
                calc["engine_values"] = item["engine_values"]
            for field, value in zip(("classification", "profit_timing", "accounting_treatment"), defaults):
                if item.get(field) not in (CLASSIFICATIONS if field == "classification" else PROFIT_TIMINGS if field == "profit_timing" else ACCOUNTING_TREATMENTS):
                    item[field] = value
    return {"valuation_inputs": valuation_inputs, "pillar_mappings": pillar_rows,
            "divergence_mappings": divergence_rows, "option_mappings": option_rows}


def run_staged_research(run_dir, briefing, evidence, code, name, evidence_dir, fixed_paths, refresh, max_age_hours, dry_run, allow_dynamic_fetch=True, resume=False, rerun_stage3=False, rerun_stage5=False):
    """Execute the original five-stage research process as separate LLM nodes."""
    stage1_evidence = select_topic_evidence(evidence, "business", "annual", "主营")
    if resume and (run_dir / "stage_1_business.json").exists():
        stage_one = read_json(run_dir / "stage_1_business.json")
        stage1_readings = read_json(run_dir / "stage_1_readings.json")
    else:
        stage1_readings = read_evidence_batches("阶段一", "识别业务、资产平台、并购、REITs、产能和海外项目。", {"briefing": briefing}, stage1_evidence)
        write_json(run_dir / "stage_1_readings.json", stage1_readings)
        stage_one = llm_stage("阶段一：业务地图拆解", (
            "完整识别业务线、资产平台、并购/重组、REITs/资产证券化、产能和海外项目；"
            "按最小经济实质拆分：同一资产平台若同时包含现有经常性收益、未来扩募/并购/投产和历史已实现收益，"
            "必须拆成不同pillar_id，禁止在一个业务候选项里混写多种利润性质；"
            "只输出business_pillars和search_plan。business_pillars每项字段精确为pillar_id,name,business_essence,"
            "economic_nature(recurring_operation|future_event|historical_realized|narrative_candidate),"
            "profit_bridge={2025a,2026e,2027e,unit:'亿元'},route(A|A1|B|C|D|E|F),split_reason,source_ids。"
            "profit_bridge只整理公司或研报明确披露的数值，不自行推演未来利润；原始元值必须除以1亿。历史事项前瞻为0，未来事项2025a为0，无法量化项填0并保留业务说明。"
            "search_plan每项精确为source_pillar_id,topic,query,reason；所有非稳态项目必须有包含公司名和代码的专题query。"
        ), {"briefing": briefing, "code": code, "name": name, "source_readings": stage1_readings}, evidence_catalog(stage1_evidence), False)
    try:
        validate_stage_one_contract(stage_one, stage1_evidence)
    except PipelineError:
        stage_one = repair_stage_one_contract(stage_one, stage1_readings, briefing, code, name, stage1_evidence)
    write_json(run_dir / "stage_1_business.json", stage_one)
    checkpoint(run_dir, "stage_1_business", file="stage_1_business.json")
    if resume and (run_dir / "search_plan.json").exists():
        plan = dynamic_search_plan({"search_plan": read_json(run_dir / "search_plan.json")}, code, name)
    else:
        plan = dynamic_search_plan(stage_one, code, name)
    write_json(run_dir / "search_plan.json", plan)
    search_runs = fetch_dynamic_evidence(plan, code, evidence_dir, refresh, max_age_hours, dry_run, allow_dynamic_fetch)
    if dry_run:
        return None, search_runs
    briefing_path = run_dir / "briefing.json"
    if any(item["status"] == "missing_offline" for item in search_runs):
        raise PipelineError("阶段三专题缓存缺失；移除 --no-fetch-evidence 后重新运行以获取完整证据")
    dynamic_paths = [ROOT / item["path"] for item in search_runs if item.get("status") in ("done", "cached", "cached_offline")]
    if rerun_stage3 or rerun_stage5 or not (resume and (run_dir / "stage_3_expectations.json").exists()):
        evidence, rejected = build_evidence(briefing_path, briefing, code, name, evidence_dir, fixed_paths + dynamic_paths)
        if resume:
            preserve_cached_reading_sources(run_dir, evidence)
        write_json(run_dir / "evidence.json", {"evidence": evidence, "rejected": rejected})
    stage2_evidence = select_topic_evidence(evidence, "consensus", "研报", "comparables", "可比")
    if resume and (run_dir / "stage_2_consensus.json").exists():
        stage_two = read_json(run_dir / "stage_2_consensus.json")
        stage2_readings = read_json(run_dir / "stage_2_readings.json")
        if (run_dir / "stage_2_baseline.json").exists():
            existing_baseline = read_json(run_dir / "stage_2_baseline.json")
            if int(existing_baseline.get("version") or 0) < 4 and stage_two.get("baseline_pillars"):
                baseline = build_stage_two_baseline(stage_two, evidence, stage_one)
                write_json(run_dir / "stage_2_baseline.json", baseline)
                checkpoint(run_dir, "stage_2_baseline", "done", file="stage_2_baseline.json", rebuilt_for_contract=4)
            else:
                checkpoint(run_dir, "stage_2_baseline", "done", file="stage_2_baseline.json", reused=True)
        elif stage_two.get("baseline_pillars"):
            baseline = build_stage_two_baseline(stage_two, evidence, stage_one)
            write_json(run_dir / "stage_2_baseline.json", baseline)
            checkpoint(run_dir, "stage_2_baseline", "done", file="stage_2_baseline.json")
        else:
            checkpoint(run_dir, "stage_2_baseline", "legacy_missing", file="stage_2_baseline.json")
    else:
        readings_path = run_dir / "stage_2_readings.json"
        if resume and readings_path.exists():
            stage2_readings = read_json(readings_path)
            checkpoint(run_dir, "阶段二", "done", file=readings_path.name, reused=True)
        else:
            stage2_readings = read_evidence_batches("阶段二", "逐机构提取双年盈利预测、目标估值结论、利润驱动和关键假设；区分当前隐含PE与目标PE。", {"stage_1_business": stage_one}, stage2_evidence)
            write_json(readings_path, stage2_readings)
        institutions_path = run_dir / "stage_2_institutions.json"
        if resume and institutions_path.exists():
            institutions = read_json(institutions_path)
        else:
            institutions = llm_stage("阶段二A：机构预测与研报逻辑清洗", (
                "输出contract_version=5、institution_forecasts、profit_analysis、excluded_forecasts、consensus_divergence和business_line_divergence；不要输出可比公司。"
                "逐篇提取全部候选机构的2026E和2027E营收、净利、PE、目标价、利润驱动和关键假设；3家只是有效共识底线，不是上限。"
                "institution_forecasts每家含institution,source_ids,revenue_2026e,revenue_2027e,net_profit_2026e,net_profit_2027e,"
                "reported_np,recurring_np_2026e,recurring_np_2027e,non_recurring_items,profit_scope(recurring|includes_non_recurring|unknown),profit_scope_reason,"
                "pe_2026e,pe_2027e,pe_semantics_2026e,pe_semantics_2027e,target_pe_2026e,target_pe_2027e,target_price,valuation_scope,included_project_ids,scope_reason。"
                "pe_semantics只能为current_implied|target_explicit|target_implied|unavailable。研报写‘当前股价对应PE’必须标current_implied，绝不能复制到target_pe。"
                "只有明确目标PE，或由同份研报目标价与预测利润可靠换算的PE，才能写target_pe并标target_explicit或target_implied。没有目标估值结论的研报只贡献利润。"
                "profit_analysis逐家对应institution_forecasts，含institution,report_date,np_2026e,np_2027e,profit_scope,summary,profit_drivers,key_assumptions,included_business_items,excluded_or_unclear_items,risks,source_ids。"
                "summary回答该机构为什么得到这组利润；所有驱动和假设必须来自研报原文，未披露就明确写未披露，不得自行推演。"
                "只有研报明确把REIT、处置、公允价值等非经常项目计入预测时才能标includes_non_recurring；仅仅没有写明排除时必须标unknown。"
                "另列过期、双年预测不完整、口径污染和明显异常样本的逐项排除原因。"
            ), {"briefing": briefing, "stage_1_business": stage_one, "source_readings": stage2_readings}, evidence_catalog(stage2_evidence), False, max_tokens=24000)
            write_json(institutions_path, institutions)
            checkpoint(run_dir, "stage_2_institutions", "done", file=institutions_path.name)
        model_path = run_dir / "stage_2_model_inputs.json"
        if resume and model_path.exists():
            model_inputs = read_json(model_path)
        else:
            model_inputs = llm_stage("阶段二B：公司级一致性预期估值", (
                "输出contract_version=5、baseline_pillars、business_item_coverage和assumption_ledger，不得重复逐机构表。"
                "baseline_pillars必须且只能有一个pillar_type=operating、profit_basis=company_consensus的‘公司整体一致性预期’估值行；禁止按业务支柱分配利润或估值，禁止加入阶段三独立事项。"
                "业务地图只通过business_item_coverage说明多数机构已纳入、部分纳入或未明确，不改变公司整体利润。"
                "每柱含pillar_id,pillar_type(operating|independent_event),profit_basis(company_consensus|direct_segment_forecast|future_event|residual_asset_value|historical_realized),name,profit_metric,valuation_method,multiple_name,data_confidence,estimation_method,source_ids。"
                "以及years.2026e/2027e的pessimistic/base/optimistic；每个情景含profit,multiple,probability,target_price,institutions,reason,profit_reason,multiple_reason,probability_reason,source_ids。"
                "每个情景必须保留同一家机构或同一组可比机构的利润与目标估值完整组合。multiple只允许来自target_explicit或target_implied；current_implied严禁进入。"
                "按清洗后机构组合估值的下沿、中位、上沿形成悲观、基准、乐观，且估值不得倒挂。合格目标估值组合不足2家时输出status=insufficient_valuation_consensus，不得用当前PE、可比公司或固定比例回退。"
                "profit和baseline_value使用亿元数值；经营支柱probability固定1。"
                "business_item_coverage必须逐一覆盖阶段一business_pillars的pillar_id，每项含source_pillar_id,"
                "valuation_role(valued_operating|valued_event|merged_component|narrative_observation|historical_excluded),"
                "valuation_pillar_id,quantification_status(quantitative|qualitative|missing_input),"
                "overlap_status(incremental|inside_target|not_applicable),reason,source_ids。"
                "持续经营业务组成可merged_component到公司整体估值行；潜在事项可narrative_observation；历史已实现事项标historical_excluded。不得输出valued_event或独立估值支柱。"
                "assumption_ledger每项含assumption_id,pillar_id,year,metric,baseline_value,unit,condition,information_cutoff,source_ids,"
                "included_in_baseline,pricing_channel(profit|multiple|standalone|none),pricing_reason,quantification_status(quantitative|qualitative|missing_input)。"
                "assumption_ledger只记录估值表取值审计；逐机构业务假设已在profit_analysis中，不在这里重复。"
            ), {"stage_1_business": stage_one, "institution_analysis": institutions}, evidence_catalog(stage2_evidence), False)
            write_json(model_path, model_inputs)
            checkpoint(run_dir, "stage_2_model_inputs", "done", file=model_path.name)
        stage_two = {**institutions, **model_inputs}
        write_json(run_dir / "stage_2_consensus.json", stage_two)
        baseline = build_stage_two_baseline(stage_two, evidence, stage_one)
        write_json(run_dir / "stage_2_baseline.json", baseline)
        checkpoint(run_dir, "stage_2_consensus", file="stage_2_consensus.json")
        checkpoint(run_dir, "stage_2_baseline", file="stage_2_baseline.json")
    dynamic_cache_paths = {str(path.relative_to(ROOT)) for path in dynamic_paths}
    stage3_candidates = [item for item in evidence if item.get("cache_path") in dynamic_cache_paths and not item.get("duplicate_of")]
    stage3_evidence = stage3_candidates
    if resume and not rerun_stage3 and (run_dir / "stage_3_expectations.json").exists():
        stage_three = read_json(run_dir / "stage_3_expectations.json")
        stage3_readings = read_json(run_dir / "stage_3_readings.json")
    else:
        stage3_readings = read_evidence_batches("阶段三", "抽取项目进度、资产平台、并购、产能、海外和管理层事实及其估值含义。", {"stage_1_business": stage_one, "stage_2_consensus": stage_two}, stage3_evidence)
        write_json(run_dir / "stage_3_readings.json", stage3_readings)
        stage_three = llm_stage("阶段三：预期差与项目估值", (
        "逐专题阅读公告、资产证券化、并购、产能、海外、订单、管理层和产业链证据。"
        "把现有持续收益与未来扩募/并购/投产拆成不同的最小经济项目，不得混写。输出projects数组，每项含"
        "project_id,name,source_pillar_id,business_essence,valuation_role(baseline_component|probabilistic_event|narrative_observation|historical_excluded),"
        "quantification_status(quantitative|qualitative|missing_input),included_in_valuation,incremental_to_baseline,source_ids,invalidation_conditions。"
        "baseline_component只解释已进入阶段二的利润，不得重复加值；historical_excluded不进入前瞻估值。"
        "probabilistic_event必须相对阶段二基准是独立增量并强制计入，另给years.2026e/2027e的悲观/基准/乐观，"
        "每格含profit,multiple,probability,profit_reason,multiple_reason,probability_reason,source_ids；悲观允许profit或概率为0，"
        "基准和乐观必须有可估值收益/资产价值。缺少利润/价值、倍数或独立口径任一项时只能narrative_observation，概率不能代替缺失输入。"
        "对每个项目输出 classification(core_operating|asset_pipeline|narrative_option|non_recurring),"
        "valuation_route(A|A1|B|C|D|E|F), profit_timing(realized|contracted|pipeline|long_term),"
        "accounting_treatment(recurring|non_recurring|consolidated)，以及利润、倍数、概率、证据和不成立条件。"
        ), {"briefing": briefing, "stage_1_business": stage_one, "stage_2_consensus": stage_two, "search_plan": plan, "source_readings": stage3_readings}, evidence_catalog(stage3_evidence), False)
        write_json(run_dir / "stage_3_expectations.json", stage_three)
        checkpoint(run_dir, "stage_3_expectations", file="stage_3_expectations.json")
    validate_stage_three_contract(stage_three, evidence)
    # Stage four reuses project-level readings and only reasons about verification.
    # It must not spend another pass rereading the same source documents.
    stage4_readings = {"reused_from": "stage_3_readings.json", "source_count": len(stage3_evidence)}
    if resume and not rerun_stage3 and (run_dir / "stage_4_catalysts.json").exists():
        stage_four = read_json(run_dir / "stage_4_catalysts.json")
    else:
        write_json(run_dir / "stage_4_readings.json", stage4_readings)
        stage_four = llm_stage("阶段四：后续验证与风险", (
        "为重要机构盈利假设、关键分歧和重大业务项目建立时间、事件、验证成功/失败含义和风险。"
        "只输出verification_nodes、risks；不要另建催化剂日历，不得遗漏重大项目。"
        ), {"stage_1_business": stage_one, "stage_2_consensus": stage_two, "stage_3_expectations": stage_three,
            "stage_3_readings": stage3_readings, "reuse_note": stage4_readings}, evidence_catalog(stage3_evidence), False)
        write_json(run_dir / "stage_4_catalysts.json", stage_four)
        checkpoint(run_dir, "stage_4_catalysts", file="stage_4_catalysts.json")
    if resume and not rerun_stage3 and not rerun_stage5 and (run_dir / "research_card.json").exists():
        final = read_json(run_dir / "research_card.json")
    else:
        research_path = run_dir / "stage_5_research.json"
        if resume and not rerun_stage3 and research_path.exists():
            stage_five_research = read_json(research_path)
        else:
            stage_five_research = build_stage_five_research(
                run_dir, briefing, stage_one, stage_two, stage_three, stage_four, evidence, resume and not rerun_stage3
            )
            normalize_stage_five_research(stage_five_research)
            repair_stage_five_thesis(stage_five_research, evidence)
            validate_stage_five_research(stage_five_research, evidence)
            write_json(research_path, stage_five_research)
            checkpoint(run_dir, "stage_5_research", "done", file=research_path.name)
        normalize_stage_five_research(stage_five_research)
        validate_stage_five_research(stage_five_research, evidence)
        mapping_path = run_dir / "stage_5_mapping.json"
        if resume and mapping_path.exists():
            stage_five_mapping = read_json(mapping_path)
        else:
            stage_five_mapping = build_stage_five_mapping(
                run_dir, stage_five_research, stage_two, stage_three, evidence, resume
            )
        normalize_stage_five_mapping(stage_five_mapping, stage_five_research, stage_two)
        try:
            validate_stage_five_mapping(stage_five_mapping, stage_five_research)
        except PipelineError as exc:
            stage_five_mapping = build_stage_five_mapping(
                run_dir, stage_five_research, stage_two, stage_three, evidence, resume
            )
            normalize_stage_five_mapping(stage_five_mapping, stage_five_research, stage_two)
            try:
                validate_stage_five_mapping(stage_five_mapping, stage_five_research)
            except PipelineError as repair_exc:
                stage_five_mapping = repair_stage_five_mapping(stage_five_mapping, stage_five_research, evidence, str(repair_exc))
                normalize_stage_five_mapping(stage_five_mapping, stage_five_research, stage_two)
                validate_stage_five_mapping(stage_five_mapping, stage_five_research)
        if not mapping_path.exists() or read_json(mapping_path) != stage_five_mapping:
            write_json(mapping_path, stage_five_mapping)
            checkpoint(run_dir, "stage_5_mapping", "done", file=mapping_path.name)
        final = assemble_stage_five(stage_five_research, stage_five_mapping)
    return final, search_runs


def all_source_ids(card):
    ids = []
    for key in ("facts", "assumptions", "consensus", "comparables", "risks", "catalysts", "calc_param_sources",
                "market_divergences", "narrative_options", "verification_nodes"):
        for item in card.get(key, []) or []:
            ids.extend(item.get("source_ids", []) if isinstance(item, dict) else [])
    return ids


def require_sources(items, label, valid_ids, required=True):
    if not items and required:
        raise PipelineError(f"研究卡缺少 {label}")
    for index, item in enumerate(items or []):
        if not isinstance(item, dict) or not item.get("source_ids"):
            raise PipelineError(f"{label}[{index}] 缺少 evidence source_id")
        if not set(item["source_ids"]).issubset(valid_ids):
            raise PipelineError(f"{label}[{index}] 引用了无效 evidence source_id")


def consensus_status(card):
    cutoff = datetime.now().date() - timedelta(days=183)
    valid = []
    for item in card.get("consensus", []) or []:
        if not isinstance(item, dict):
            continue
        try:
            report_date = datetime.strptime(str(item.get("report_date")), "%Y-%m-%d").date()
        except ValueError:
            continue
        if item.get("institution") and report_date >= cutoff and item.get("np_2026e") is not None and item.get("np_2027e") is not None:
            valid.append(item)
    return ("done" if len(valid) >= 3 else "insufficient_consensus"), len(valid)


def _median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    return ordered[middle] if len(ordered) % 2 else (ordered[middle - 1] + ordered[middle]) / 2


def _forecast_billion(value, label):
    """Accept stage-two extraction in either raw yuan or canonical 亿元."""
    number = _number(value, label)
    if abs(number) > 10000:
        number /= 1e8
    return _canonical_billion(number, label)


def derive_operating_profit_control(stage_two):
    """Build a deterministic recurring-profit control from comparable forecasts."""
    controls = {}
    for year in ("2026e", "2027e"):
        candidates = []
        for index, item in enumerate(stage_two.get("institution_forecasts") or []):
            if not isinstance(item, dict) or item.get("profit_scope") not in {"recurring", "unknown"}:
                continue
            recurring_key = f"recurring_np_{year}"
            value = item.get(recurring_key)
            if value is None and year == "2026e":
                value = item.get("recurring_np")
            if value is None:
                value = item.get(f"net_profit_{year}")
            if value is None:
                continue
            profit = _forecast_billion(value, f"institution_forecasts[{index}].{year}")
            if profit <= 0:
                continue
            candidates.append({
                "institution": str(item.get("institution") or f"institution_{index + 1}"),
                "profit": profit, "profit_scope": item.get("profit_scope"),
                "source_ids": item.get("source_ids") or [],
            })
        if len(candidates) < 3:
            raise PipelineError(f"阶段二{year.upper()}可比持续经营利润预测不足3家，不能拆分或估值")
        center = _median([item["profit"] for item in candidates])
        deviations = [abs(item["profit"] - center) for item in candidates]
        mad = _median(deviations)
        threshold = max(abs(center) * 0.25, mad * 3, 1.0)
        included = [item for item in candidates if abs(item["profit"] - center) <= threshold]
        excluded = [item for item in candidates if item not in included]
        if len(included) < 3:
            raise PipelineError(f"阶段二{year.upper()}异常值清洗后持续经营利润预测不足3家")
        values = [item["profit"] for item in included]
        controls[year] = {
            "pessimistic": round(min(values), 4),
            "base": round(_median(values), 4),
            "optimistic": round(max(values), 4),
            "included_forecasts": included,
            "excluded_outliers": excluded,
            "method": "recurring_or_unknown_scope; MAD/25% outlier gate; min/median/max scenarios",
            "scope_confidence": "high" if sum(item["profit_scope"] == "recurring" for item in included) >= 3 else "medium",
        }
    return controls


def validate_business_item_coverage(stage_one, coverage, pillars, valid_ids):
    """Require every stage-one economic item to have one explicit valuation destination."""
    if stage_one is None:
        return []
    raw_items = stage_one.get("business_pillars") if isinstance(stage_one, dict) else None
    if not isinstance(raw_items, list) or not raw_items:
        raise PipelineError("阶段一缺少可供估值准入审计的 business_pillars")
    if not isinstance(coverage, list):
        raise PipelineError("阶段二缺少 business_item_coverage")
    expected = {str(item.get("pillar_id") or "").strip() for item in raw_items if isinstance(item, dict)}
    if "" in expected or len(expected) != len(raw_items):
        raise PipelineError("阶段一 business_pillars 的 pillar_id 缺失或重复")
    by_source = {}
    pillar_by_id = {str(item.get("pillar_id")): item for item in pillars}
    normalized = []
    for index, raw in enumerate(coverage):
        label = f"business_item_coverage[{index}]"
        if not isinstance(raw, dict):
            raise PipelineError(f"{label} 必须为对象")
        source_id = str(raw.get("source_pillar_id") or "").strip()
        if not source_id or source_id in by_source:
            raise PipelineError(f"{label}.source_pillar_id 缺失或重复")
        role = str(raw.get("valuation_role") or "")
        quantification = str(raw.get("quantification_status") or "")
        overlap = str(raw.get("overlap_status") or "")
        proposed_target_id = str(raw.get("valuation_pillar_id") or "").strip()
        proposed_target = pillar_by_id.get(proposed_target_id)
        if proposed_target and proposed_target.get("profit_basis") == "historical_realized":
            role = "historical_excluded"
            overlap = "not_applicable"
            raw["valuation_role"] = role
            raw["overlap_status"] = overlap
            raw["valuation_pillar_id"] = None
            raw["reason"] = (
                f"{raw.get('reason') or ''}；历史已实现事项由脚本归为historical_excluded，不作为前瞻估值支柱"
            )
        if role not in BUSINESS_ITEM_ROLES:
            raise PipelineError(f"{label}.valuation_role 无效")
        if quantification not in QUANTIFICATION_STATUSES:
            raise PipelineError(f"{label}.quantification_status 无效")
        if overlap not in OVERLAP_STATUSES:
            raise PipelineError(f"{label}.overlap_status 无效")
        sources = raw.get("source_ids") or []
        if not sources or not set(sources).issubset(valid_ids):
            raise PipelineError(f"{label} 缺少有效 source_ids")
        reason = str(raw.get("reason") or "").strip()
        if not reason:
            raise PipelineError(f"{label}.reason 缺失")
        target_id = str(raw.get("valuation_pillar_id") or "").strip()
        if role in {"valued_operating", "valued_event", "merged_component"}:
            target = pillar_by_id.get(target_id)
            if target is None:
                raise PipelineError(f"{label} 目标估值支柱不存在")
            expected_type = "independent_event" if role == "valued_event" else "operating"
            if target.get("pillar_type") != expected_type:
                raise PipelineError(f"{label} 目标支柱类型与 {role} 不匹配")
        elif target_id:
            raise PipelineError(f"{label} 非估值项不得指定 valuation_pillar_id")
        if role in {"valued_operating", "valued_event"} and quantification != "quantitative":
            raise PipelineError(f"{label} 估值支柱必须可量化")
        if role == "valued_event" and overlap != "incremental":
            raise PipelineError(f"{label} 概率事项必须证明相对基准为增量")
        if role == "merged_component" and overlap != "inside_target":
            raise PipelineError(f"{label} 合并业务必须声明已包含在目标支柱")
        row = {
            "source_pillar_id": source_id, "valuation_role": role,
            "valuation_pillar_id": target_id or None, "quantification_status": quantification,
            "overlap_status": overlap, "reason": reason, "source_ids": sources,
        }
        by_source[source_id] = row
        normalized.append(row)
    actual = set(by_source)
    if actual != expected:
        raise PipelineError(
            f"阶段二业务地图覆盖不完整；缺少={sorted(expected - actual)}，多余={sorted(actual - expected)}"
        )
    return normalized


def validate_stage_one_contract(stage_one, evidence):
    """Keep the economic map granular, unit-safe and usable by stage-two coverage."""
    items = stage_one.get("business_pillars") if isinstance(stage_one, dict) else None
    if not isinstance(items, list) or not items:
        raise PipelineError("阶段一缺少 business_pillars")
    valid_ids = {item.get("source_id") for item in evidence if item.get("source_id")}
    seen = set()
    for index, item in enumerate(items):
        label = f"stage_1_business.business_pillars[{index}]"
        if not isinstance(item, dict):
            raise PipelineError(f"{label} 必须为对象")
        pillar_id = str(item.get("pillar_id") or "").strip()
        if not pillar_id or pillar_id in seen:
            raise PipelineError(f"{label}.pillar_id 缺失或重复")
        seen.add(pillar_id)
        if not item.get("name") or not item.get("business_essence") or not item.get("split_reason"):
            raise PipelineError(f"{label} 缺少 name/business_essence/split_reason")
        nature = str(item.get("economic_nature") or "")
        if nature not in ECONOMIC_NATURES:
            raise PipelineError(f"{label}.economic_nature 无效")
        if item.get("route") not in VALUATION_ROUTES:
            raise PipelineError(f"{label}.route 无效")
        sources = item.get("source_ids") or []
        if not sources or not set(sources).issubset(valid_ids):
            raise PipelineError(f"{label} 缺少有效 source_ids")
        bridge = item.get("profit_bridge")
        if not isinstance(bridge, dict) or bridge.get("unit") != "亿元":
            raise PipelineError(f"{label}.profit_bridge 必须使用亿元")
        values = {
            year: _canonical_billion(bridge.get(year), f"{label}.profit_bridge.{year}")
            for year in ("2025a", "2026e", "2027e")
        }
        if nature == "historical_realized" and (values["2026e"] != 0 or values["2027e"] != 0):
            raise PipelineError(f"{label} 历史事项前瞻利润必须为0")
        if nature == "future_event" and values["2025a"] != 0:
            raise PipelineError(f"{label} 未来事项不得混入历史已实现利润")
        if nature == "narrative_candidate" and any(values.values()):
            raise PipelineError(f"{label} 叙事候选尚不可量化，利润桥必须为0")
    plan = stage_one.get("search_plan")
    if not isinstance(plan, list) or not plan:
        raise PipelineError("阶段一缺少 search_plan")
    planned = {str(item.get("source_pillar_id") or "") for item in plan if isinstance(item, dict)}
    required = {item["pillar_id"] for item in items if item["economic_nature"] != "recurring_operation"}
    if not required.issubset(planned):
        raise PipelineError(f"阶段一专题检索计划未覆盖非稳态项目: {sorted(required - planned)}")
    return stage_one


def repair_stage_one_contract(stage_one, readings, briefing, code, name, evidence):
    """Repair stage-one shape once without introducing evidence outside its reading set."""
    repaired = llm_stage("阶段一：经济业务地图契约修复", (
        "原阶段一输出未通过结构门禁。只依据输入证据重建business_pillars和search_plan，不新增事实。"
        "business_pillars每项字段必须精确为pillar_id,name,business_essence,economic_nature,profit_bridge,route,split_reason,source_ids。"
        "economic_nature只能recurring_operation|future_event|historical_realized|narrative_candidate。"
        "同一平台里的现有经常性收益、未来扩募/并购/投产、历史已实现收益必须拆成不同pillar_id，不得混写。"
        "profit_bridge必须为{2025a,2026e,2027e,unit:'亿元'}，原始元值除以1亿；historical_realized前瞻为0，"
        "future_event的2025a为0，无法量化的narrative_candidate三年均为0。"
        "search_plan每项含source_pillar_id,topic,query,reason；所有非recurring_operation项目至少一条专题检索，query含公司名和代码。"
    ), {"invalid_stage_one": stage_one, "briefing": briefing, "source_readings": readings,
        "code": code, "name": name}, evidence_catalog(evidence), False)
    validate_stage_one_contract(repaired, evidence)
    return repaired


def validate_stage_three_contract(stage_three, evidence):
    """Separate priced probability events from narrative observations before stage five."""
    projects = stage_three.get("projects") if isinstance(stage_three, dict) else None
    if not isinstance(projects, list):
        raise PipelineError("阶段三缺少 projects 数组")
    valid_ids = {item.get("source_id") for item in evidence if item.get("source_id")}
    seen = set()
    scenarios = ("pessimistic", "base", "optimistic")
    for index, project in enumerate(projects):
        label = f"stage_3_expectations.projects[{index}]"
        if not isinstance(project, dict):
            raise PipelineError(f"{label} 必须为对象")
        project_id = str(project.get("project_id") or "").strip()
        if not project_id or project_id in seen:
            raise PipelineError(f"{label}.project_id 缺失或重复")
        seen.add(project_id)
        role = str(project.get("valuation_role") or "")
        if role not in STAGE_THREE_ROLES:
            raise PipelineError(f"{label}.valuation_role 无效")
        quantification = str(project.get("quantification_status") or "")
        if quantification not in QUANTIFICATION_STATUSES:
            raise PipelineError(f"{label}.quantification_status 无效")
        included = project.get("included_in_valuation")
        incremental = project.get("incremental_to_baseline")
        if not isinstance(included, bool) or not isinstance(incremental, bool):
            raise PipelineError(f"{label} 缺少布尔型 included_in_valuation/incremental_to_baseline")
        sources = project.get("source_ids") or project.get("evidence") or []
        if not sources or not set(sources).issubset(valid_ids):
            raise PipelineError(f"{label} 缺少有效 source_ids")
        project["source_ids"] = sources
        if not project.get("name") or not project.get("business_essence") or not project.get("invalidation_conditions"):
            raise PipelineError(f"{label} 缺少名称、业务实质或失效条件")
        if role == "probabilistic_event":
            if not included or not incremental or quantification != "quantitative":
                raise PipelineError(f"{label} 概率事项必须可量化、相对基准增量且计入估值")
            years = project.get("years")
            if not isinstance(years, dict):
                raise PipelineError(f"{label}.years 缺失")
            for year in ("2026e", "2027e"):
                year_row = years.get(year)
                if not isinstance(year_row, dict):
                    raise PipelineError(f"{label}.years.{year} 缺失")
                for scenario in scenarios:
                    cell = year_row.get(scenario)
                    if not isinstance(cell, dict):
                        raise PipelineError(f"{label}.years.{year}.{scenario} 缺失")
                    profit = _canonical_billion(cell.get("profit"), f"{label}.{year}.{scenario}.profit")
                    multiple = _number(cell.get("multiple"), f"{label}.{year}.{scenario}.multiple")
                    probability = _number(cell.get("probability"), f"{label}.{year}.{scenario}.probability")
                    if multiple <= 0 or not 0 <= probability <= 1:
                        raise PipelineError(f"{label}.{year}.{scenario} 倍数须大于0且概率在0到1")
                    if scenario in {"base", "optimistic"} and profit == 0:
                        raise PipelineError(f"{label}.{year}.{scenario} 概率事项缺少可估值收益/资产价值")
                    cell_sources = cell.get("source_ids") or sources
                    if not set(cell_sources).issubset(valid_ids):
                        raise PipelineError(f"{label}.{year}.{scenario} source_ids 无效")
                    if not all(cell.get(field) for field in ("profit_reason", "multiple_reason", "probability_reason")):
                        raise PipelineError(f"{label}.{year}.{scenario} 缺少利润、倍数或概率依据")
                    cell["source_ids"] = cell_sources
        elif role == "narrative_observation":
            if included:
                raise PipelineError(f"{label} 叙事观察项不得进入估值")
        elif role == "baseline_component":
            if not included or incremental:
                raise PipelineError(f"{label} 基准组成必须标记已计价且不得作为增量估值")
        elif included or incremental:
            raise PipelineError(f"{label} 历史排除项不得进入估值或标记为增量")
    return stage_three


def validate_stage_three_research_coverage(stage_three, research):
    """Keep stage-three valuation decisions unchanged when stage five rewrites the narrative."""
    expected = {
        str(item.get("project_id")): item
        for item in stage_three.get("projects", []) or []
        if item.get("valuation_role") in {"probabilistic_event", "narrative_observation"}
    }
    actual = {str(item.get("research_id")): item for item in research.get("narrative_options", []) or []}
    if set(actual) != set(expected):
        raise PipelineError(
            f"阶段五未完整继承阶段三事项；缺少={sorted(set(expected) - set(actual))}，多余={sorted(set(actual) - set(expected))}"
        )
    for project_id, source in expected.items():
        item = actual[project_id]
        role = source["valuation_role"]
        if item.get("valuation_role") != role:
            raise PipelineError(f"阶段五事项 {project_id} valuation_role 被改写")
        should_include = role == "probabilistic_event"
        if item.get("included_in_valuation") is not should_include:
            raise PipelineError(f"阶段五事项 {project_id} 计价状态与阶段三门禁不一致")
        if should_include:
            item["valuation_scenarios"] = source["years"]
            item["incremental_to_baseline"] = True
            item["quantification_status"] = "quantitative"


def validate_stage_five_baseline_pillar_coverage(research, baseline):
    """Stage five may explain components but cannot recreate rejected valuation pillars."""
    expected = {
        str(item.get("pillar_id"))
        for item in baseline.get("pillars", []) or []
        if item.get("profit_basis") != "historical_realized"
    }
    actual = {str(item.get("research_id")) for item in research.get("business_pillars", []) or []}
    if actual != expected:
        raise PipelineError(
            f"阶段五估值支柱必须一对一继承阶段二；缺少={sorted(expected - actual)}，多余={sorted(actual - expected)}"
        )


def build_stage_two_baseline(stage_two, evidence, stage_one=None):
    """Validate stage-two scenario inputs and calculate the baseline valuation table."""
    contract_version = int(stage_two.get("contract_version") or 4)
    valid_ids = {item.get("source_id") for item in evidence if item.get("source_id")}
    raw_pillars = stage_two.get("baseline_pillars")
    assumptions = stage_two.get("assumption_ledger")
    if not isinstance(raw_pillars, list) or not raw_pillars:
        raise PipelineError("阶段二缺少 baseline_pillars")
    if not isinstance(assumptions, list):
        raise PipelineError("阶段二 assumption_ledger 必须为数组")
    institution_anchors = []
    target_pe_by_year = {"2026e": {}, "2027e": {}}
    target_profit_by_year = {"2026e": {}, "2027e": {}}
    for index, raw in enumerate(stage_two.get("institution_forecasts") or []):
        if not isinstance(raw, dict):
            raise PipelineError(f"institution_forecasts[{index}] 必须为对象")
        if contract_version >= 5:
            target_values = [raw.get("target_pe_2026e"), raw.get("target_pe_2027e"), raw.get("target_price")]
            if all(value is None for value in target_values):
                continue
            semantics = {str(raw.get("pe_semantics_2026e") or "unavailable"), str(raw.get("pe_semantics_2027e") or "unavailable")}
            if not semantics.issubset({"current_implied", "target_explicit", "target_implied", "unavailable"}):
                raise PipelineError(f"institution_forecasts[{index}] PE语义无效")
        elif raw.get("pe_2026e") is None and raw.get("target_price") is None:
            continue
        label = f"institution_forecasts[{index}]"
        scope = str(raw.get("profit_scope") or "")
        usability = ("direct_usable" if contract_version >= 5 else str(raw.get("pe_usability") or ""))
        if not usability and raw.get("target_price") is not None:
            usability = "cross_check_only"
            raw["pe_usability"] = usability
            raw["scope_reason"] = (
                f"{raw.get('scope_reason') or '未提供可直接使用的PE'}；"
                "脚本将缺少明确PE可用性但含目标价的记录保守归为cross_check_only"
            )
        if scope not in {"recurring", "includes_non_recurring", "unknown"}:
            raise PipelineError(f"{label}.profit_scope 无效")
        if usability not in {"direct_usable", "convertible", "cross_check_only"}:
            raise PipelineError(f"{label}.pe_usability 无效")
        valuation_scope = str(raw.get("valuation_scope") or "unknown")
        if not raw.get("scope_reason"):
            raise PipelineError(f"{label} 缺少 scope_reason")
        if contract_version < 5 and usability in {"direct_usable", "convertible"} and valuation_scope == "unknown":
            raise PipelineError(f"{label} 可用估值锚缺少 valuation_scope")
        if contract_version < 5 and usability == "direct_usable" and scope == "unknown":
            raise PipelineError(f"{label} 利润口径未知，不能 direct_usable")
        anchor_sources = raw.get("source_ids") or []
        if not anchor_sources or not set(anchor_sources).issubset(valid_ids):
            raise PipelineError(f"{label} 缺少有效 source_ids")
        institution_anchors.append({
            "institution": raw.get("institution"), "profit_scope": scope,
            "valuation_scope": valuation_scope,
            "included_project_ids": raw.get("included_project_ids") or [],
            "pe_usability": usability, "scope_reason": str(raw["scope_reason"]),
            "pe_2026e": raw.get("target_pe_2026e") if contract_version >= 5 else raw.get("pe_2026e"),
            "pe_2027e": raw.get("target_pe_2027e") if contract_version >= 5 else raw.get("pe_2027e"),
            "pe_semantics_2026e": raw.get("pe_semantics_2026e"),
            "pe_semantics_2027e": raw.get("pe_semantics_2027e"),
            "target_price": raw.get("target_price"),
            "source_ids": anchor_sources,
        })
        if contract_version >= 5 and scope != "includes_non_recurring":
            institution = str(raw.get("institution") or "").strip()
            for year in ("2026e", "2027e"):
                semantics = str(raw.get(f"pe_semantics_{year}") or "unavailable")
                target_pe = raw.get(f"target_pe_{year}")
                if institution and semantics in {"target_explicit", "target_implied"} and target_pe is not None:
                    target_pe_by_year[year][institution] = _number(target_pe, f"institution_forecasts[{index}].target_pe_{year}")
                    profit_value = raw.get(f"recurring_np_{year}")
                    if profit_value is None:
                        profit_value = raw.get(f"net_profit_{year}")
                    if profit_value is not None:
                        target_profit_by_year[year][institution] = _forecast_billion(
                            profit_value, f"institution_forecasts[{index}].net_profit_{year}"
                        )
    if contract_version >= 5:
        for year, values in target_pe_by_year.items():
            if len(values) < 2:
                raise PipelineError(f"阶段二{year.upper()}合格机构目标估值组合不足2家")
    profit_control = derive_operating_profit_control(stage_two)
    pillars, pillar_ids = [], set()
    for index, raw in enumerate(raw_pillars):
        label = f"baseline_pillars[{index}]"
        if not isinstance(raw, dict):
            raise PipelineError(f"{label} 必须为对象")
        pillar_id = str(raw.get("pillar_id") or "").strip()
        if not pillar_id or pillar_id in pillar_ids:
            raise PipelineError(f"{label}.pillar_id 缺失或重复")
        pillar_ids.add(pillar_id)
        sources = raw.get("source_ids") or []
        if not sources or not set(sources).issubset(valid_ids):
            raise PipelineError(f"{label} 缺少有效 source_ids")
        missing = [field for field in ("name", "profit_metric", "valuation_method", "multiple_name", "pillar_type") if not raw.get(field)]
        if missing:
            raise PipelineError(f"{label} 缺少: " + ", ".join(missing))
        pillar_type = str(raw["pillar_type"])
        if pillar_type not in {"operating", "independent_event"}:
            raise PipelineError(f"{label}.pillar_type 无效")
        profit_basis = str(raw.get("profit_basis") or "")
        if pillar_type == "operating" and profit_basis not in {"company_consensus", "direct_segment_forecast"}:
            raise PipelineError(f"{label}.profit_basis 必须为 company_consensus|direct_segment_forecast")
        if pillar_type == "independent_event":
            profit_basis = {"actual_historical": "historical_realized", "independent_event": "future_event"}.get(
                profit_basis, profit_basis
            )
            if profit_basis not in {"future_event", "residual_asset_value", "historical_realized"}:
                raise PipelineError(f"{label}.profit_basis 独立事项必须为 future_event|residual_asset_value|historical_realized")
        years = {}
        for year in ("2026e", "2027e"):
            raw_year = (raw.get("years") or {}).get(year)
            if not isinstance(raw_year, dict):
                raise PipelineError(f"{label}.years.{year} 缺失")
            year_row = {}
            for scenario in ("pessimistic", "base", "optimistic"):
                raw_scenario = raw_year.get(scenario)
                if not isinstance(raw_scenario, dict):
                    raise PipelineError(f"{label}.years.{year}.{scenario} 缺失")
                profit = _canonical_billion(raw_scenario.get("profit"), f"{label}.{year}.{scenario}.profit")
                multiple = _number(raw_scenario.get("multiple"), f"{label}.{year}.{scenario}.multiple")
                probability = _number(raw_scenario.get("probability"), f"{label}.{year}.{scenario}.probability")
                if multiple < 0 or not 0 <= probability <= 1:
                    raise PipelineError(f"{label}.{year}.{scenario} 倍数不得为负且概率必须在0到1")
                if pillar_type == "operating" and abs(probability - 1) > 1e-9:
                    raise PipelineError(f"{label}.{year}.{scenario} 经营支柱概率必须为1")
                scenario_sources = raw_scenario.get("source_ids") or sources
                if not scenario_sources or not set(scenario_sources).issubset(valid_ids):
                    raise PipelineError(f"{label}.{year}.{scenario} 缺少有效 source_id")
                if not raw_scenario.get("profit_reason") or not raw_scenario.get("multiple_reason") or not raw_scenario.get("probability_reason"):
                    raise PipelineError(f"{label}.{year}.{scenario} 缺少利润、倍数或概率依据")
                if contract_version >= 5:
                    scenario_institutions = raw_scenario.get("institutions") or []
                    if isinstance(scenario_institutions, str):
                        scenario_institutions = [scenario_institutions]
                    matched_target_pes = [target_pe_by_year[year].get(str(name)) for name in scenario_institutions]
                    matched_profits = [target_profit_by_year[year].get(str(name)) for name in scenario_institutions]
                    if (not scenario_institutions or any(value is None for value in matched_target_pes)
                            or any(value is None for value in matched_profits)):
                        raise PipelineError(f"{label}.{year}.{scenario} 未对应合格机构目标PE组合")
                    expected_multiple = _median(matched_target_pes)
                    if abs(multiple - expected_multiple) > max(0.1, expected_multiple * 0.01):
                        raise PipelineError(
                            f"{label}.{year}.{scenario} PE {multiple:.2f}x 与所列机构目标PE中位数 {expected_multiple:.2f}x 不一致"
                        )
                    expected_profit = _median(matched_profits)
                    if abs(profit - expected_profit) > max(0.01, expected_profit * 0.001):
                        raise PipelineError(
                            f"{label}.{year}.{scenario} 利润 {profit:.2f}亿 与所列机构利润中位数 {expected_profit:.2f}亿不一致"
                        )
                year_row[scenario] = {
                    "profit": round(profit, 4), "multiple": round(multiple, 4),
                    "probability": round(probability, 4),
                    "valuation": round(profit * multiple * probability, 2),
                    "target_price": raw_scenario.get("target_price"),
                    "institutions": raw_scenario.get("institutions") or [],
                    "reason": str(raw_scenario.get("reason") or ""),
                    "profit_reason": str(raw_scenario.get("profit_reason") or ""),
                    "multiple_reason": str(raw_scenario.get("multiple_reason") or ""),
                    "probability_reason": str(raw_scenario.get("probability_reason") or ""),
                    "source_ids": scenario_sources,
                }
                if pillar_type == "independent_event" and profit_basis == "historical_realized":
                    year_row[scenario].update({
                        "profit": 0.0, "valuation": 0.0,
                        "profit_reason": f"{year_row[scenario]['profit_reason']}；历史已实现损益不进入前瞻估值，脚本归零",
                    })
            years[year] = year_row
        confidence = str(raw.get("data_confidence") or "medium")
        if confidence not in {"high", "medium", "low"}:
            raise PipelineError(f"{label}.data_confidence 必须为 high/medium/low")
        pillars.append({
            "pillar_id": pillar_id, "pillar_type": pillar_type, "pricing_stage": "institution_baseline",
            "profit_basis": profit_basis,
            "name": raw["name"], "profit_metric": raw["profit_metric"],
            "valuation_method": raw["valuation_method"], "multiple_name": raw["multiple_name"],
            "data_confidence": confidence,
            "estimation_method": raw.get("estimation_method", ""), "source_ids": sources, "years": years,
        })
    business_item_coverage = validate_business_item_coverage(
        stage_one, stage_two.get("business_item_coverage"), pillars, valid_ids
    )
    operating = [pillar for pillar in pillars if pillar["pillar_type"] == "operating"]
    if not operating:
        raise PipelineError("阶段二统一估值模型缺少经营支柱")
    if contract_version >= 5 and (len(pillars) != 1 or len(operating) != 1 or operating[0]["profit_basis"] != "company_consensus"):
        raise PipelineError("V5阶段二只能生成一个公司整体一致性预期估值行")
    if len(operating) > 1 and any(pillar["profit_basis"] != "direct_segment_forecast" for pillar in operating):
        raise PipelineError("缺少可靠分部利润时不得强拆多个经营估值支柱；请合并为核心持续经营业务")
    reconciliation = {}
    for year in ("2026e", "2027e"):
        reconciliation[year] = {}
        for scenario in ("pessimistic", "base", "optimistic"):
            target = float(profit_control[year][scenario])
            before = sum(float(pillar["years"][year][scenario]["profit"]) for pillar in operating)
            difference_pct = abs(before - target) / target if target else 0
            if len(operating) > 1 and difference_pct > 0.05:
                raise PipelineError(
                    f"阶段二{year.upper()} {scenario} 分部利润合计{before:.2f}亿偏离公司级控制数{target:.2f}亿超过5%"
                )
            if contract_version >= 5:
                lower = float(profit_control[year]["pessimistic"])
                upper = float(profit_control[year]["optimistic"])
                if before < lower - 1e-6 or before > upper + 1e-6:
                    raise PipelineError(
                        f"阶段二{year.upper()} {scenario}利润{before:.2f}亿不在清洗后机构区间{lower:.2f}-{upper:.2f}亿"
                    )
                allocations = [before]
                method = "institution_valuation_package_preserved"
            elif len(operating) == 1:
                allocations = [target]
                method = "single_operating_pillar_overridden_by_company_consensus"
            else:
                if before <= 0:
                    raise PipelineError(f"阶段二{year.upper()} {scenario} 分部利润合计必须大于0")
                allocations = [target * float(pillar["years"][year][scenario]["profit"]) / before for pillar in operating]
                method = "direct_segment_forecasts_scaled_to_company_consensus"
            for pillar, allocated in zip(operating, allocations):
                cell = pillar["years"][year][scenario]
                cell["profit"] = round(allocated, 4)
                cell["valuation"] = round(allocated * float(cell["multiple"]) * float(cell["probability"]), 2)
                cell["profit_reason"] = f"{cell['profit_reason']}；脚本按公司级持续经营利润控制数完成总量对账"
            after = sum(float(pillar["years"][year][scenario]["profit"]) for pillar in operating)
            reconciliation[year][scenario] = {
                "control_profit": round(target, 4), "pillar_profit_before": round(before, 4),
                "pillar_profit_after": round(after, 4), "difference_after": round(after - target, 4),
                "method": method,
            }
    totals = {
        year: {
            scenario: round(sum(float(pillar["years"][year][scenario]["valuation"]) for pillar in pillars), 2)
            for scenario in ("pessimistic", "base", "optimistic")
        } for year in ("2026e", "2027e")
    }
    if contract_version >= 5:
        for year, values in totals.items():
            if not values["pessimistic"] <= values["base"] <= values["optimistic"]:
                raise PipelineError(f"阶段二{year.upper()}一致性估值倒挂，拒绝发布")
    pillar_profit_basis = {pillar["pillar_id"]: pillar["profit_basis"] for pillar in pillars}
    normalized_assumptions, assumption_ids = [], set()
    for index, raw in enumerate(assumptions):
        label = f"assumption_ledger[{index}]"
        if not isinstance(raw, dict):
            raise PipelineError(f"{label} 必须为对象")
        assumption_id = str(raw.get("assumption_id") or "").strip()
        pillar_id = str(raw.get("pillar_id") or "").strip()
        if not assumption_id or assumption_id in assumption_ids or pillar_id not in pillar_ids:
            raise PipelineError(f"{label} assumption_id 缺失/重复或 pillar_id 无效")
        assumption_ids.add(assumption_id)
        sources = raw.get("source_ids") or []
        if not sources or not set(sources).issubset(valid_ids):
            raise PipelineError(f"{label} 缺少有效 source_ids")
        missing = [field for field in ("metric", "year", "unit", "condition", "information_cutoff") if raw.get(field) in (None, "")]
        if missing:
            raise PipelineError(f"{label} 缺少: " + ", ".join(missing))
        included = raw.get("included_in_baseline")
        if not isinstance(included, bool):
            raise PipelineError(f"{label}.included_in_baseline 必须为布尔值")
        pricing_channel = str(raw.get("pricing_channel") or "")
        if pricing_channel not in {"profit", "multiple", "standalone", "none"}:
            raise PipelineError(f"{label}.pricing_channel 无效")
        if included is True and pricing_channel == "none":
            target_pillar = next((item for item in pillars if item.get("pillar_id") == pillar_id), None)
            if target_pillar and target_pillar.get("pillar_type") == "operating":
                pricing_channel = "profit"
                raw["pricing_reason"] = (
                    f"{raw.get('pricing_reason') or ''}；已纳入经营支柱但通道缺失，脚本归一为profit"
                )
            elif target_pillar and target_pillar.get("pillar_type") == "independent_event":
                pricing_channel = "standalone"
                raw["pricing_reason"] = (
                    f"{raw.get('pricing_reason') or ''}；已纳入独立事项但通道缺失，脚本归一为standalone"
                )
            else:
                included = False
                raw["pricing_reason"] = f"{raw.get('pricing_reason') or ''}；无有效定价入口，脚本校正为未纳入"
        if included is False and pricing_channel != "none":
            included = True
            raw["pricing_reason"] = f"{raw.get('pricing_reason') or ''}；脚本按非空定价入口校正为已纳入"
        if pillar_profit_basis.get(pillar_id) == "historical_realized":
            included = False
            pricing_channel = "none"
            raw["pricing_reason"] = f"{raw.get('pricing_reason') or ''}；历史已实现损益不进入前瞻模型"
        pricing_reason = str(raw.get("pricing_reason") or "").strip()
        if not pricing_reason:
            raise PipelineError(f"{label}.pricing_reason 缺失")
        quantification_status = str(raw.get("quantification_status") or "")
        if quantification_status not in {"quantitative", "qualitative", "missing_input"}:
            raise PipelineError(f"{label}.quantification_status 无效")
        normalized_assumptions.append({
            "assumption_id": assumption_id, "pillar_id": pillar_id, "year": str(raw["year"]),
            "metric": str(raw["metric"]), "baseline_value": raw.get("baseline_value"),
            "unit": str(raw["unit"]), "condition": str(raw["condition"]),
            "information_cutoff": str(raw["information_cutoff"]), "source_ids": sources,
            "included_in_baseline": included, "pricing_channel": pricing_channel,
            "included_in_model": included, "pricing_stage": "institution_baseline" if included else "not_priced",
            "pricing_reason": pricing_reason, "quantification_status": quantification_status,
        })
    consensus_valuation = {
        "sample_count": len({item.get("institution") for item in institution_anchors if item.get("institution")}),
        "years": operating[0]["years"] if contract_version >= 5 else {},
    }
    return {
        "version": contract_version, "status": stage_two.get("status") or "ready", "source": "stage_2_native",
        "pillars": pillars, "assumption_ledger": normalized_assumptions,
        "consensus_valuation": consensus_valuation,
        "business_item_coverage": business_item_coverage,
        "institution_anchors": institution_anchors,
        "operating_profit_control": profit_control,
        "operating_profit_reconciliation": reconciliation,
        "totals": totals,
    }


def enrich_consensus_from_stage_two(card, run_dir):
    """Preserve dual-year institution forecasts when stage five omits copied fields."""
    path = run_dir / "stage_2_consensus.json"
    if not path.exists():
        return
    forecasts = read_json(path).get("institution_forecasts", [])
    evidence_path = run_dir / "evidence.json"
    evidence_rows = read_json(evidence_path).get("evidence", []) if evidence_path.exists() else []
    evidence_dates = {str(item.get("source_id")): item.get("published_at") for item in evidence_rows if item.get("source_id")}
    by_institution = {str(item.get("institution")): item for item in forecasts
                      if isinstance(item, dict) and item.get("institution")}
    consensus = card.setdefault("consensus", [])
    existing = {str(item.get("institution")): item for item in consensus if isinstance(item, dict) and item.get("institution")}
    for institution, source in by_institution.items():
        item = existing.get(institution)
        if item is None:
            item = {"institution": institution, "source_ids": list(source.get("source_ids") or [])}
            consensus.append(item)
            existing[institution] = item
        dates = [evidence_dates.get(str(source_id)) for source_id in source.get("source_ids") or []]
        dates = [str(value)[:10] for value in dates if value]
        if not item.get("report_date") and dates:
            item["report_date"] = max(dates)
        field_map = {
            "np_2026e": "net_profit_2026e", "np_2027e": "net_profit_2027e",
            "revenue_2026e": "revenue_2026e", "revenue_2027e": "revenue_2027e",
            "pe_2026e": "pe_2026e", "pe_2027e": "pe_2027e", "target_price": "target_price",
            "profit_scope": "profit_scope", "valuation_scope": "valuation_scope",
            "pe_usability": "pe_usability", "scope_reason": "scope_reason",
            "profit_scope_reason": "profit_scope_reason",
            "pe_semantics_2026e": "pe_semantics_2026e", "pe_semantics_2027e": "pe_semantics_2027e",
            "target_pe_2026e": "target_pe_2026e", "target_pe_2027e": "target_pe_2027e",
            "included_project_ids": "included_project_ids",
        }
        for target_field, source_field in field_map.items():
            if item.get(target_field) is None and source.get(source_field) is not None:
                item[target_field] = source[source_field]
        for source_id in source.get("source_ids") or []:
            if source_id not in item.setdefault("source_ids", []):
                item["source_ids"].append(source_id)
    for item in consensus:
        if not isinstance(item, dict):
            continue
        source = by_institution.get(str(item.get("institution")))
        if not source:
            continue
        for field in ("revenue_2026e", "revenue_2027e", "net_profit_2026e", "net_profit_2027e", "pe_2026e", "pe_2027e", "target_price",
                      "profit_scope", "profit_scope_reason", "valuation_scope", "pe_usability", "scope_reason",
                      "pe_semantics_2026e", "pe_semantics_2027e", "target_pe_2026e", "target_pe_2027e", "included_project_ids"):
            if item.get(field) is None and source.get(field) is not None:
                item[field] = source[field]


CLASSIFICATIONS = {"core_operating", "asset_pipeline", "narrative_option", "non_recurring"}
PROFIT_TIMINGS = {"realized", "contracted", "pipeline", "long_term"}
ACCOUNTING_TREATMENTS = {"recurring", "non_recurring", "consolidated"}
MAPPING_TARGETS = {"pillar", "type_b_pipeline", "layer3_item", "layer2", "exclude"}
BUSINESS_ITEM_ROLES = {"valued_operating", "valued_event", "merged_component", "narrative_observation", "historical_excluded"}
STAGE_THREE_ROLES = {"baseline_component", "probabilistic_event", "narrative_observation", "historical_excluded"}
QUANTIFICATION_STATUSES = {"quantitative", "qualitative", "missing_input"}
OVERLAP_STATUSES = {"incremental", "inside_target", "not_applicable"}
ECONOMIC_NATURES = {"recurring_operation", "future_event", "historical_realized", "narrative_candidate"}
VALUATION_ROUTES = {"A", "A1", "B", "C", "D", "E", "F"}
ROUTE_ENGINE_FIELDS = {
    "A": ("ebitda_2026e", "ebitda_2027e", "comparable_ev_ebitda", "net_debt"),
    "A1": ("consensus_np_2026e", "consensus_np_2027e", "consensus_np_lower_2026e", "consensus_np_upper_2026e",
           "consensus_np_lower_2027e", "consensus_np_upper_2027e"),
    "B": ("consensus_np_2026e", "consensus_np_2027e", "consensus_np_lower_2026e", "consensus_np_upper_2026e",
          "consensus_np_lower_2027e", "consensus_np_upper_2027e"),
    "C": ("consensus_np_2026e", "consensus_np_2027e", "consensus_np_lower_2026e", "consensus_np_upper_2026e",
          "consensus_np_lower_2027e", "consensus_np_upper_2027e"),
    "D": ("net_assets", "roe", "comparable_pb_median", "comparable_pb_lower", "comparable_pb_upper", "comparable_roe"),
    "E": ("revenue_2026e", "revenue_2027e", "gross_margin", "comparable_ps_median", "comparable_ps_lower",
          "comparable_ps_upper", "comparable_gross_margin"),
    "F": ("net_assets", "roe", "comparable_pb_median", "comparable_pb_lower", "comparable_pb_upper", "comparable_roe"),
}


def _number(value, label):
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise PipelineError(f"{label} 必须为数值") from exc


def _canonical_billion(value, label):
    number = _number(value, label)
    if abs(number) > 1000:
        raise PipelineError(f"{label} 超出亿元合理范围，疑似仍为元单位: {number}")
    return number


def _validate_statement_collection(card, key, valid_ids):
    items = card.get(key, []) or []
    if not isinstance(items, list):
        raise PipelineError(f"{key} 必须为对象数组")
    for index, item in enumerate(items):
        if not isinstance(item, dict) or not str(item.get("statement", "")).strip():
            raise PipelineError(f"{key}[{index}] 必须为含 statement/source_ids 的对象")
    require_sources(items, key, valid_ids, required=False)


def _validate_project_contract(item, label):
    if item.get("classification") not in CLASSIFICATIONS:
        raise PipelineError(f"{label}.classification 无效")
    if item.get("profit_timing") not in PROFIT_TIMINGS:
        raise PipelineError(f"{label}.profit_timing 无效")
    if item.get("accounting_treatment") not in ACCOUNTING_TREATMENTS:
        raise PipelineError(f"{label}.accounting_treatment 无效")
    mapping = item.get("calculation_mapping")
    if not isinstance(mapping, dict) or mapping.get("target") not in MAPPING_TARGETS:
        raise PipelineError(f"{label}.calculation_mapping.target 无效")
    if not item.get("source_ids"):
        raise PipelineError(f"{label} 缺少 source_ids")
    return mapping


def _validate_engine_units(values, label):
    profit_fields = {"ebitda_2026e", "ebitda_2027e", "net_debt", "consensus_np_2026e", "consensus_np_2027e",
                     "consensus_np_lower_2026e", "consensus_np_upper_2026e", "consensus_np_lower_2027e",
                     "consensus_np_upper_2027e", "deducted_np", "project_profit", "pessimistic", "base", "optimistic"}
    balance_fields = {"net_assets", "revenue_2026e", "revenue_2027e"}
    for field in profit_fields & set(values):
        _canonical_billion(values[field], f"{label}.{field}")
    for field in balance_fields & set(values):
        number = _number(values[field], f"{label}.{field}")
        if abs(number) > 10000:
            raise PipelineError(f"{label}.{field} 超出亿元合理范围，疑似仍为元单位: {number}")


def build_generic_calc_params(card, briefing, code, name, evidence):
    """Map explicit research enums to the engine contract without business-name heuristics."""
    valid_ids = {item["source_id"] for item in evidence}
    snapshot = briefing.get("valuation_snapshot", {})
    raw_shares = _number(snapshot.get("total_shares"), "briefing.total_shares")
    total_shares = raw_shares / 1e8
    quote = briefing.get("market_quote")
    if not isinstance(quote, dict):
        raise PipelineError("briefing.market_quote 缺失；禁止从报告期市值反推当前价")
    current_price = _number(quote.get("current_price"), "briefing.market_quote.current_price")
    price_date = str(quote.get("price_date") or "")
    price_source = str(quote.get("price_source") or "")
    if current_price <= 0 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", price_date) or not price_source:
        raise PipelineError("briefing.market_quote 价格、日期或来源无效")
    inputs = card.get("valuation_inputs")
    if not isinstance(inputs, dict):
        raise PipelineError("研究卡缺少 valuation_inputs")
    input_fields = ("growth_quality", "market_position", "comparable_pe_median", "comparable_pe_lower",
                    "comparable_pe_upper", "company_growth_rate", "comp_growth_rate")
    missing = [field for field in input_fields if inputs.get(field) is None]
    if missing:
        raise PipelineError("valuation_inputs 缺少: " + ", ".join(missing))
    input_sources = inputs.get("source_ids", [])
    if not input_sources or not set(input_sources).issubset(valid_ids):
        raise PipelineError("valuation_inputs 缺少有效 source_ids")
    if inputs.get("growth_quality") not in {"structural", "cyclical", "mature"}:
        raise PipelineError("valuation_inputs.growth_quality 无效")
    if inputs.get("market_position") not in {"leader", "mid", "follower"}:
        raise PipelineError("valuation_inputs.market_position 无效")
    for field in ("comparable_pe_median", "comparable_pe_lower", "comparable_pe_upper", "company_growth_rate", "comp_growth_rate"):
        _number(inputs[field], f"valuation_inputs.{field}")
    company_growth = _number(inputs["company_growth_rate"], "valuation_inputs.company_growth_rate")
    comp_growth = _number(inputs["comp_growth_rate"], "valuation_inputs.comp_growth_rate")
    if not (1 <= company_growth <= 300):
        raise PipelineError("valuation_inputs.company_growth_rate 必须使用百分比数值（例如29.99表示29.99%）")
    if not (1 <= comp_growth <= 300):
        raise PipelineError("valuation_inputs.comp_growth_rate 必须使用百分比数值且大于等于1")
    pe_lower = _number(inputs["comparable_pe_lower"], "valuation_inputs.comparable_pe_lower")
    pe_median = _number(inputs["comparable_pe_median"], "valuation_inputs.comparable_pe_median")
    pe_upper = _number(inputs["comparable_pe_upper"], "valuation_inputs.comparable_pe_upper")
    if not (0 < pe_lower <= pe_median <= pe_upper):
        raise PipelineError("valuation_inputs PE 区间必须满足 0 < lower <= median <= upper")
    adjustments = inputs.get("qualitative_adjustments", [])
    if not isinstance(adjustments, list) or any(not isinstance(item, dict) or not item.get("item") or not isinstance(item.get("adjustment"), (int, float)) for item in adjustments):
        raise PipelineError("valuation_inputs.qualitative_adjustments 必须为 {item,adjustment} 数组")
    if any(not -0.10 <= float(item["adjustment"]) <= 0.10 for item in adjustments):
        raise PipelineError("valuation_inputs.qualitative_adjustments 单项必须在 -0.10 到 0.10")
    params = {field: inputs[field] for field in input_fields}
    params.update({"meta": {"code": code, "name": name, "total_shares": round(total_shares, 6),
                            "current_price": round(current_price, 4), "price_date": price_date,
                            "price_source": price_source},
                   "qualitative_adjustments": inputs.get("qualitative_adjustments", []), "pillars": [],
                   "institution_consensus_2026e": [_number(item["np_2026e"], "consensus.np_2026e") for item in card.get("consensus", [])],
                   "institution_consensus_2027e": [_number(item["np_2027e"], "consensus.np_2027e") for item in card.get("consensus", [])]})
    if inputs.get("pe_selection_method") == "override":
        params["pe_override"] = _number(inputs.get("pe_override"), "valuation_inputs.pe_override")
    pillars_by_id, calc_sources = {}, []
    for field in input_fields:
        calc_sources.append({"path": field, "source_ids": input_sources})
    for index, item in enumerate(card.get("business_pillars", []) or []):
        mapping = _validate_project_contract(item, f"business_pillars[{index}]")
        if mapping["target"] == "exclude":
            continue
        if mapping["target"] != "pillar":
            raise PipelineError(f"business_pillars[{index}] 只能映射到 pillar 或 exclude")
        pillar_id = str(mapping.get("pillar_id", "")).strip()
        route = mapping.get("route")
        values = mapping.get("engine_values")
        if not pillar_id or route not in VALUATION_ROUTES or not isinstance(values, dict):
            raise PipelineError(f"business_pillars[{index}] 缺少 pillar_id/route/engine_values")
        _validate_engine_units(values, f"business_pillars[{index}].engine_values")
        missing_route_fields = [field for field in ROUTE_ENGINE_FIELDS[route] if values.get(field) is None]
        if missing_route_fields:
            raise PipelineError(f"business_pillars[{index}] 路线 {route} 缺少参数: " + ", ".join(missing_route_fields))
        if pillar_id in pillars_by_id:
            raise PipelineError(f"business_pillars[{index}] pillar_id 重复: {pillar_id}")
        pillar = {"name": item.get("name"), "route": route, **values}
        pillar.setdefault("layer2", {"pessimistic": 0, "base": 0, "optimistic": 0, "description": ""})
        pillar.setdefault("layer3_items", []); pillar.setdefault("type_b_pipeline", [])
        pillars_by_id[pillar_id] = pillar; params["pillars"].append(pillar)
        calc_sources.append({"path": f"pillars[{len(params['pillars']) - 1}].engine_values", "source_ids": item["source_ids"]})
    if not params["pillars"]:
        raise PipelineError("通用映射未生成任何估值支柱")
    for collection, label in ((card.get("market_divergences", []) or [], "market_divergences"),
                              (card.get("narrative_options", []) or [], "narrative_options")):
        for index, item in enumerate(collection):
            mapping = _validate_project_contract(item, f"{label}[{index}]")
            target = mapping["target"]
            if target == "exclude":
                continue
            if label == "narrative_options" and item.get("included_in_valuation") is False:
                raise PipelineError(f"{label}[{index}] 标记为未计入估值，只能映射到 exclude")
            pillar_id = str(mapping.get("pillar_id", ""))
            if pillar_id not in pillars_by_id:
                raise PipelineError(f"{label}[{index}] 引用了不存在的 pillar_id")
            pillar = pillars_by_id[pillar_id]; values = mapping.get("engine_values")
            if not isinstance(values, dict):
                raise PipelineError(f"{label}[{index}] 缺少 engine_values")
            _validate_engine_units(values, f"{label}[{index}].engine_values")
            if target == "layer2":
                current = pillar["layer2"]
                for scenario in ("pessimistic", "base", "optimistic"):
                    current[scenario] = _number(current.get(scenario, 0), f"{label}[{index}].layer2.{scenario}") + _number(values.get(scenario, 0), f"{label}[{index}].engine_values.{scenario}")
                descriptions = [text for text in (current.get("description"), values.get("description")) if text]
                current["description"] = "；".join(descriptions)
            elif target == "type_b_pipeline":
                pillar["type_b_pipeline"].append({"name": item.get("name"), **values})
            elif target == "layer3_item":
                pillar["layer3_items"].append({"name": item.get("name"), **values})
            else:
                raise PipelineError(f"{label}[{index}] 不能映射到 {target}")
    card["calc_param_sources"] = calc_sources
    card["calc_params"] = params
    return params


def validate_card_v4(card, briefing, evidence, code, name):
    """Validate the generic research contract, then deterministically map it."""
    if card.get("status") == "complete":
        card["status"] = "ready"
    if card.get("status") != "ready":
        raise PipelineError(f"研究证据不足: {card.get('status', 'missing_status')}")
    valid_ids = {item["source_id"] for item in evidence}
    evidence_dates = {item["source_id"]: item.get("published_at") for item in evidence}
    thesis = card.get("investment_thesis")
    if not isinstance(thesis, dict) or not all(thesis.get(field) for field in ("market_trade", "company_stage", "core_judgement", "source_ids")):
        raise PipelineError("研究卡缺少结构化 investment_thesis")
    require_sources([thesis], "investment_thesis", valid_ids)
    for index, item in enumerate(card.get("consensus", []) or []):
        if not isinstance(item, dict):
            raise PipelineError(f"consensus[{index}] 必须为对象")
        if item.get("np_2026e") is None: item["np_2026e"] = item.get("net_profit_2026e")
        if item.get("np_2027e") is None: item["np_2027e"] = item.get("net_profit_2027e")
        if not item.get("report_date"):
            dates = [evidence_dates.get(source_id) for source_id in item.get("source_ids", [])]
            dates = [value for value in dates if value]
            if dates: item["report_date"] = max(dates)
    require_sources(card.get("consensus"), "consensus", valid_ids)
    state, count = consensus_status(card)
    if state != "done":
        raise PipelineError(f"一致预期不足：需要至少3家双年预测，当前 {count} 家")
    contract_version = int(card.get("contract_version") or 0)
    if contract_version < 5:
        require_sources(card.get("comparables"), "comparables", valid_ids)
    raw_shares = _number((briefing.get("valuation_snapshot") or {}).get("total_shares"), "briefing.total_shares")
    total_shares = raw_shares / 1e8
    consensus_by_institution = {
        str(item.get("institution")): item for item in card.get("consensus", []) or []
        if isinstance(item, dict) and item.get("institution")
    }
    valid_anchors = 0
    for index, item in enumerate(card.get("comparables", []) or []):
        anchor_type = item.get("anchor_type")
        if anchor_type not in {"company", "institution_target", "industry_aggregate"}:
            raise PipelineError(f"comparables[{index}].anchor_type 无效")
        if anchor_type == "institution_target" and item.get("pe") is None:
            matched = next((row for institution, row in consensus_by_institution.items()
                            if institution and institution in str(item.get("name") or "")), None)
            if matched and matched.get("target_price") is not None and matched.get("np_2026e") is not None:
                target_price = _number(matched["target_price"], f"comparables[{index}].target_price")
                target_profit = _number(matched["np_2026e"], f"comparables[{index}].np_2026e")
                if target_profit > 0:
                    item["pe"] = round(target_price * total_shares / target_profit, 2)
                    item["pe_horizon"] = item.get("pe_horizon") or "2026E implied from target price"
                    item["relevance"] = f"{item.get('relevance') or ''}；目标价按2026E利润反推隐含PE，仅作交叉验证"
        for field in ("name", "relevance"):
            if item.get(field) is None or item.get(field) == "":
                raise PipelineError(f"comparables[{index}] 缺少 {field}")
        if item.get("pe") is None and anchor_type == "institution_target":
            item["pe_horizon"] = item.get("pe_horizon") or "target price only; PE unavailable"
            continue
        if item.get("pe") is None or not item.get("pe_horizon"):
            raise PipelineError(f"comparables[{index}] 缺少 pe/pe_horizon")
        _number(item["pe"], f"comparables[{index}].pe")
        if anchor_type == "company":
            for field in ("growth_rate", "gross_margin", "growth_horizon"):
                if item.get(field) is None or item.get(field) == "":
                    raise PipelineError(f"comparables[{index}] 公司可比缺少 {field}")
                _number(item[field], f"comparables[{index}].{field}")
        if anchor_type != "industry_aggregate":
            valid_anchors += 1
    if contract_version < 5 and valid_anchors < 2:
        raise PipelineError("有效估值锚不足：至少需要2个公司或机构目标估值锚")
    pillars = card.get("business_pillars", []) or []
    if not pillars:
        raise PipelineError("研究卡缺少 business_pillars")
    for index, item in enumerate(pillars):
        if not isinstance(item, dict):
            raise PipelineError(f"business_pillars[{index}] 必须为对象")
        if not all(item.get(field) for field in ("name", "business_essence", "split_rationale", "profit_bridge", "source_ids")):
            raise PipelineError(f"business_pillars[{index}] 缺少研究字段")
        bridge = item.get("profit_bridge")
        if not isinstance(bridge, dict) or bridge.get("base_2025a") is None:
            raise PipelineError(f"business_pillars[{index}].profit_bridge 必须为含 base_2025a 的对象")
        if bridge.get("profit_metric") not in {"net_profit", "gross_profit", "operating_profit"}:
            raise PipelineError(f"business_pillars[{index}].profit_bridge.profit_metric 无效")
        _canonical_billion(bridge["base_2025a"], f"business_pillars[{index}].profit_bridge.base_2025a")
        for year in ("items_2026e", "items_2027e"):
            require_sources(bridge.get(year), f"business_pillars[{index}].profit_bridge.{year}", valid_ids)
            for bridge_index, bridge_item in enumerate(bridge.get(year, [])):
                if not isinstance(bridge_item, dict) or not bridge_item.get("item"):
                    raise PipelineError(f"business_pillars[{index}].profit_bridge.{year}[{bridge_index}] 结构无效")
                _canonical_billion(bridge_item.get("profit_impact"), f"business_pillars[{index}].profit_bridge.{year}[{bridge_index}].profit_impact")
        _validate_project_contract(item, f"business_pillars[{index}]")
        require_sources([item], f"business_pillars[{index}]", valid_ids)
    for key in ("market_divergences", "narrative_options"):
        for index, item in enumerate(card.get(key, []) or []):
            if not isinstance(item, dict):
                raise PipelineError(f"{key}[{index}] 必须为对象")
            _validate_project_contract(item, f"{key}[{index}]")
            require_sources([item], f"{key}[{index}]", valid_ids)
            required_fields = (("name", "pillar", "bull_case", "bear_case", "root_cause", "our_judgement", "pricing_status")
                               if key == "market_divergences" else
                               ("name", "pillar", "business_essence", "pricing_status", "potential_profit", "pe", "probability", "included_in_valuation", "verification_nodes"))
            missing = [field for field in required_fields if item.get(field) is None or item.get(field) == ""]
            if missing:
                raise PipelineError(f"{key}[{index}] 缺少研究字段: " + ", ".join(missing))
            if key == "narrative_options":
                probability = _number(item["probability"], f"narrative_options[{index}].probability")
                if not 0 <= probability <= 1:
                    raise PipelineError(f"narrative_options[{index}].probability 必须在0到1")
                _canonical_billion(item["potential_profit"], f"narrative_options[{index}].potential_profit")
    nodes = card.get("verification_nodes", []) or []
    require_sources(nodes, "verification_nodes", valid_ids)
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            raise PipelineError(f"verification_nodes[{index}] 必须为对象")
        if not all(item.get(field) for field in ("node_id", "timeframe", "event", "pillar", "success_meaning", "failure_meaning")):
            raise PipelineError(f"verification_nodes[{index}] 缺少验证逻辑")
    for key in ("facts", "assumptions", "risks", "catalysts"):
        _validate_statement_collection(card, key, valid_ids)
    inputs = card.get("valuation_inputs", {})
    for field in ("stage2_recommended_pe", "selected_pe", "pe_selection_method", "pe_selection_reason",
                  "pe_scope_basis", "excluded_anchor_names"):
        if inputs.get(field) is None or inputs.get(field) == "":
            raise PipelineError(f"valuation_inputs 缺少 PE 语义字段: {field}")
    if inputs.get("pe_selection_method") not in {"three_step", "override"}:
        raise PipelineError("valuation_inputs.pe_selection_method 无效")
    stage2_pe = _number(inputs["stage2_recommended_pe"], "valuation_inputs.stage2_recommended_pe")
    selected_pe = _number(inputs["selected_pe"], "valuation_inputs.selected_pe")
    if stage2_pe <= 0 or selected_pe <= 0:
        raise PipelineError("阶段二建议 PE 和最终采用 PE 必须大于0")
    if inputs.get("pe_selection_method") == "override" and abs(_number(inputs.get("pe_override"), "valuation_inputs.pe_override") - selected_pe) > 0.01:
        raise PipelineError("valuation_inputs.pe_override 必须等于 selected_pe")
    return build_generic_calc_params(card, briefing, code, name, evidence)


def validate_result_semantics(card, result):
    """Reject silent PE drift between stage-two research, mapping, and engine output."""
    inputs = card["valuation_inputs"]
    final_pe = _number(result.get("pe_2026e", {}).get("final_pe"), "calc_results.pe_2026e.final_pe")
    selected_pe = _number(inputs["selected_pe"], "valuation_inputs.selected_pe")
    if abs(final_pe - selected_pe) / selected_pe > 0.05:
        raise PipelineError(f"引擎最终 PE {final_pe:.2f}x 与阶段五采用 PE {selected_pe:.2f}x 偏离超过5%")
    stage2_pe = _number(inputs["stage2_recommended_pe"], "valuation_inputs.stage2_recommended_pe")
    if abs(final_pe - stage2_pe) / stage2_pe > 0.20 and len(str(inputs.get("pe_selection_reason", "")).strip()) < 12:
        raise PipelineError("最终 PE 相对阶段二建议值偏离超过20%，但缺少充分的显式偏离理由")


def repair_research_card(card, evidence):
    """Repair only the stage-five contract; research facts and citations are immutable."""
    source_card = card.get("research_card") if isinstance(card, dict) and isinstance(card.get("research_card"), dict) else card
    repaired = llm_stage("阶段五：研究卡结构修复", (
        "只修复输入研究卡的结构和计算契约，返回完整研究卡。禁止新增、删除或改写研究事实与 source_ids。"
        "必须保留逐机构 consensus、comparables、业务支柱、分歧、叙事和验证节点。"
        "补齐每一个 business_pillars、每一个 market_divergences、每一个 narrative_options 对象的三个顶层字段，缺一不可："
        "classification 只能是 core_operating|asset_pipeline|narrative_option|non_recurring；"
        "profit_timing 只能是 realized|contracted|pipeline|long_term；accounting_treatment 只能是 recurring|non_recurring|consolidated。"
        "valuation_inputs 的 PE 和增长率必须为数值，comp_growth_rate 不得为空；qualitative_adjustments 必须为 {item,adjustment} 数组。"
        "profit_bridge 的 profit_impact 必须为亿元数值；若证据只支持方向无法拆出数值，使用0并在 item 中注明未量化，不得猜测。"
        "calculation_mapping 必须保留显式 target/pillar_id/route/engine_values；layer2 的 pessimistic/base/optimistic 单位必须是相对 Layer1 的市值增量（亿元），"
        "不能填写利润、利润率或 PE 本身；type_b_pipeline 和 layer3_item 沿用其规定字段。"
        "支柱路线字段必须匹配：A用双年EBITDA/EV-EBITDA/净负债，A1/B/C用双年净利上下沿，D/F用净资产/ROE/PB，E用双年营收/毛利率/PS。"
        "若输入证据没有路线所需量化参数，必须将该项目 calculation_mapping.target 改为 exclude，不得拿另一条路线的字段冒充。"
        "所有非 exclude 的 layer2/type_b_pipeline/layer3_item 只能引用 target=pillar 的现存 pillar_id；若对应支柱已排除，该项目也必须 target=exclude。"
        "所有 Layer2 市值增量必须按当前 valuation_inputs 的 PE 区间和当前支柱利润重新计算，description 不得残留修复前的旧 PE 或旧口径。"
        "verification_nodes 必须保持扁平对象数组。最终只输出合法 JSON。"
    ), {"research_card": source_card}, evidence_catalog(evidence), False)
    return repaired.get("research_card") if isinstance(repaired.get("research_card"), dict) else repaired


def audit_research_card(card, run_dir, evidence):
    """Audit project coverage and valuation semantics against completed stage outputs."""
    source_card = card.get("research_card") if isinstance(card, dict) and isinstance(card.get("research_card"), dict) else card
    audited = llm_stage("阶段五：研究完整性与估值校准审计", (
        "只返回 audit_patch 对象，且只含 investment_thesis、valuation_inputs、comparables、business_pillars、market_divergences、"
        "narrative_options、verification_nodes 七个完整替换字段；不要重复 consensus/facts/assumptions/risks/catalysts。"
        "逐项对照 search_plan 和 stage_3_readings：每个具有独立利润、资产、并购、"
        "产能、海外、资产证券化或验证逻辑的重大项目，都必须进入业务支柱、市场分歧、叙事期权或显式排除项，并建立验证节点；"
        "不得因已有概括性项目而漏掉独立交易。只可使用输入中的事实与 source_ids。"
        "company_growth_rate 和 comp_growth_rate 均使用百分比数值（29.99表示29.99%），必须大于等于1；PE满足下沿<=中位<=上沿。"
        "不得用缺少底层明细的单一行业平均数同时充当上下沿；可比数据不足时，使用逐机构报告给出的2026E PE或目标估值作为独立估值锚，"
        "并在 comparables.name 标明机构估值锚。核心判断、PE区间、利润口径和 engine_values 必须一致。"
        "每个 business_pillars、market_divergences、narrative_options 均须含三个标准枚举；profit_bridge 数值和 Layer2 市值增量单位正确；"
        "叙事项目暂无可靠量化可 target=exclude，但必须保留研究结论与验证节点。"
    ), {"research_card": source_card, "search_plan": read_json(run_dir / "search_plan.json"),
        "stage_2_consensus": read_json(run_dir / "stage_2_consensus.json"),
        "stage_3_expectations": read_json(run_dir / "stage_3_expectations.json"),
        "stage_3_readings": read_json(run_dir / "stage_3_readings.json"),
        "stage_4_catalysts": read_json(run_dir / "stage_4_catalysts.json")}, evidence_catalog(evidence), False)
    patch = audited.get("audit_patch") if isinstance(audited.get("audit_patch"), dict) else audited
    required = ("investment_thesis", "valuation_inputs", "comparables", "business_pillars", "market_divergences",
                "narrative_options", "verification_nodes")
    missing = [key for key in required if key not in patch]
    if missing:
        raise PipelineError("研究审计补丁缺少字段: " + ", ".join(missing))
    result = dict(source_card)
    for key in required:
        result[key] = patch[key]
    result["status"] = "ready"
    return result


SCENARIOS = ("pessimistic", "base", "optimistic")


def valuation_model_matrix(model, year):
    """Convert the unified pillar model to the legacy matrix envelope."""
    rows = []
    layer_totals = {
        "layer1": {scenario: 0.0 for scenario in SCENARIOS},
        "layer2": {scenario: 0.0 for scenario in SCENARIOS},
        "layer3": {scenario: 0.0 for scenario in SCENARIOS},
    }
    for pillar in model.get("pillars", []):
        values = {
            scenario: round(float((pillar.get("years", {}).get(year, {}).get(scenario, {}) or {}).get("valuation") or 0), 2)
            for scenario in SCENARIOS
        }
        layer_key = "layer1" if pillar.get("pillar_type") == "operating" else "layer2"
        for scenario in SCENARIOS:
            layer_totals[layer_key][scenario] += values[scenario]
        rows.append({
            "pillar_name": pillar.get("name"), "pillar_id": pillar.get("pillar_id"),
            "pillar_type": pillar.get("pillar_type"), "pricing_stage": pillar.get("pricing_stage"),
            "scenario_inputs": pillar.get("years", {}).get(year, {}),
            "layer1": values if layer_key == "layer1" else {scenario: 0 for scenario in SCENARIOS},
            "layer2": values if layer_key == "layer2" else {scenario: 0 for scenario in SCENARIOS},
            "layer3": {scenario: 0 for scenario in SCENARIOS}, "pillar_total": values,
        })
    labels = {"layer1": "持续经营业务支柱", "layer2": "独立事项支柱", "layer3": "未进入估值模型"}
    layers = {
        key: {**{scenario: round(value, 2) for scenario, value in totals.items()}, "pricing_status": labels[key]}
        for key, totals in layer_totals.items()
    }
    totals = {
        scenario: round(sum(layer_totals[key][scenario] for key in layer_totals), 2)
        for scenario in SCENARIOS
    }
    return {"matrix_rows": rows, "layers_summary": layers, "total": totals}


def unified_model_markdown(model):
    if int(model.get("version") or 0) >= 5:
        lines = ["### 公司整体一致性预期估值表", "",
                 "| 年份 | 情景 | 对应机构 | 净利润(亿) | 目标PE | 目标价(元) | 估值(亿) |",
                 "|---|---|---|---:|---:|---:|---:|"]
        scenario_names = {"pessimistic": "悲观", "base": "基准", "optimistic": "乐观"}
        pillar = (model.get("pillars") or [{}])[0]
        for year in ("2026e", "2027e"):
            for scenario in SCENARIOS:
                item = pillar.get("years", {}).get(year, {}).get(scenario, {}) or {}
                institutions = item.get("institutions") or []
                if isinstance(institutions, str): institutions = [institutions]
                lines.append(f"| {year.upper()} | {scenario_names[scenario]} | {'、'.join(institutions) or '—'} | "
                             f"{item.get('profit', '—')} | {item.get('multiple', '—')}x | {item.get('target_price', '—')} | {item.get('valuation', '—')} |")
        return "\n".join(lines)
    lines = [
        "### 双年、分支柱估值表", "",
        (f"> 经营利润总量控制：2026E基准 {model.get('operating_profit_control', {}).get('2026e', {}).get('base', '—')} 亿；"
         f"2027E基准 {model.get('operating_profit_control', {}).get('2027e', {}).get('base', '—')} 亿。"), "",
        "| 年份 | 业务支柱 | 类型 | 情景 | 利润/收益(亿) | 倍数 | 概率 | 估值(亿) |",
        "|---|---|---|---|---:|---:|---:|---:|",
    ]
    scenario_names = {"pessimistic": "悲观", "base": "基准", "optimistic": "乐观"}
    type_names = {"operating": "持续经营", "independent_event": "独立事项"}
    for year in ("2026e", "2027e"):
        for pillar in model.get("pillars", []):
            for scenario in SCENARIOS:
                item = pillar.get("years", {}).get(year, {}).get(scenario, {}) or {}
                lines.append(
                    f"| {year.upper()} | {pillar.get('name', '—')} | {type_names.get(pillar.get('pillar_type'), '独立事项')} | "
                    f"{scenario_names[scenario]} | {item.get('profit', '—')} | {item.get('multiple', '—')}x | "
                    f"{float(item.get('probability') or 0):.0%} | {item.get('valuation', '—')} |"
                )
        total = model.get("totals", {}).get(year, {})
        lines.append(
            f"| **{year.upper()}** | **合计** |  |  |  |  |  | "
            f"**悲观 {total.get('pessimistic', 0)} / 基准 {total.get('base', 0)} / 乐观 {total.get('optimistic', 0)}** |"
        )
    return "\n".join(lines)


def apply_unified_model_result(result, model, params):
    """Make the unified model authoritative for report, ranking and dashboard."""
    legacy = {
        "matrix_2026e": result.get("matrix_2026e"), "matrix_2027e": result.get("matrix_2027e"),
        "pillars_2026e": result.get("pillars_2026e"), "pillars_2027e": result.get("pillars_2027e"),
    }
    matrix_2026e = valuation_model_matrix(model, "2026e")
    matrix_2027e = valuation_model_matrix(model, "2027e")
    meta = params.get("meta", {})
    shares = float(meta.get("total_shares") or 0)
    current_price = float(meta.get("current_price") or 0)
    per_share = {
        scenario: round(matrix_2026e["total"][scenario] / shares, 2) if shares > 0 else None
        for scenario in SCENARIOS
    }
    rc = reverse_check(
        current_price, per_share["pessimistic"], per_share["base"], per_share["optimistic"],
        str(result.get("pe_2026e", {}).get("growth_quality") or params.get("growth_quality") or "structural"),
        str(params.get("market_position") or "follower"),
    )
    ranking = generate_ranking_row(
        str(meta.get("code") or ""), str(meta.get("name") or ""), current_price,
        per_share["pessimistic"], per_share["base"], per_share["optimistic"],
        result.get("pe_2026e", {}).get("final_pe"), "分业务支柱统一估值",
    )
    fragments = generate_report_fragments({
        "total_2026e": matrix_2026e["total"], "total_2027e": matrix_2027e["total"],
        "matrix_2026e": matrix_2026e, "pe_2026e": result.get("pe_2026e", {}), "reverse_check": rc,
    }, shares, current_price)
    fragments["matrix_summary"] = unified_model_markdown(model)
    result.update({
        "valuation_model": model, "legacy_engine_audit": legacy,
        "matrix_2026e": matrix_2026e, "matrix_2027e": matrix_2027e,
        "reverse_check": rc, "ranking_row": ranking, "report_fragments": fragments,
    })
    return result


def render_report(run_dir, briefing, evidence, card, result):
    meta = result["_meta"]
    total = result["matrix_2026e"]["total"]
    thesis = card["investment_thesis"]
    if int(card.get("contract_version") or 0) >= 5:
        lines = [f"# {meta['name']}（{meta['code']}）估值报告", "",
                 f"> 模型三可审计运行包：`{run_dir.relative_to(ROOT)}`", "",
                 "## 一、结论摘要", "", f"- 市场主题：{thesis['market_trade']}",
                 f"- 公司阶段：{thesis['company_stage']}", f"- 核心判断：{thesis['core_judgement']}", "",
                 result["report_fragments"]["overview_table"], "", "## 二、一致性预期估值表", "",
                 result["report_fragments"]["matrix_summary"], "", "## 三、业务支柱拆分", "",
                 "| 业务支柱 | 业务实质 | 拆分逻辑 |", "|---|---|---|"]
        for pillar in card.get("business_pillars", []):
            lines.append(f"| {pillar.get('name', '—')} | {pillar.get('business_essence', '—')} | {pillar.get('split_rationale', '—')} |")
        lines += ["", "## 四、机构利润预测与关键假设", ""]
        for item in card.get("profit_analysis", []):
            assumptions = item.get("key_assumptions") or []
            if isinstance(assumptions, str): assumptions = [assumptions]
            lines += [f"### {item.get('institution', '—')}", "",
                      f"- 2026E / 2027E净利润：{item.get('np_2026e', '—')} / {item.get('np_2027e', '—')} 亿元",
                      f"- 利润口径：{item.get('profit_scope', '—')}", f"- 研报摘要：{item.get('summary', '—')}",
                      f"- 关键假设：{'；'.join(str(value) for value in assumptions) or '未结构化披露'}", ""]
        lines += ["## 五、机构一致预期明细", "", "| 机构 | 报告日期 | 2026E净利 | 2027E净利 | 利润口径 | 目标价 |",
                  "|---|---|---:|---:|---|---:|"]
        for item in card.get("consensus", []):
            lines.append(f"| {item.get('institution', '—')} | {item.get('report_date', '—')} | {item.get('np_2026e', '—')} | {item.get('np_2027e', '—')} | {item.get('profit_scope', '—')} | {item.get('target_price', '—')} |")
        lines += ["", "## 六、关键分歧", ""]
        for item in card.get("market_divergences", []):
            lines += [f"### {item.get('name', '—')}", "", f"- 较高预测：{item.get('bull_case', '—')}",
                      f"- 较低预测：{item.get('bear_case', '—')}", f"- 根因：{item.get('root_cause', '原因未披露')}",
                      f"- 共识解读：{item.get('our_judgement', '—')}", ""]
        lines += ["## 七、后续验证节点", "", "| 时间 | 事件 | 验证成功意味着 | 验证失败意味着 |", "|---|---|---|---|"]
        for item in card.get("verification_nodes", []):
            lines.append(f"| {item.get('timeframe', '—')} | {item.get('event', '—')} | {item.get('success_meaning', '—')} | {item.get('failure_meaning', '—')} |")
        lines += ["", "## 八、主要风险", ""]
        for item in card.get("risks", []):
            lines.append(f"- {item.get('statement', '')}（来源：{', '.join(item.get('source_ids', []))}）")
        lines += ["", "## 证据目录", "", "| ID | 类型 | 缓存文件 |", "|---|---|---|"]
        for item in evidence:
            lines.append(f"| {item['source_id']} | {item['source_type']} | `{item['cache_path']}` |")
        return "\n".join(lines + [""])
    lines = [f"# {meta['name']}（{meta['code']}）估值报告", "",
             f"> 模型三可审计运行包：`{run_dir.relative_to(ROOT)}`", "",
             "## 一、结论与市场交易主题", "",
             f"- **市场正在交易什么**：{thesis['market_trade']}（来源：{', '.join(thesis['source_ids'])}）",
             f"- **公司所处阶段**：{thesis['company_stage']}",
             f"- **核心判断**：{thesis['core_judgement']}", "", result["report_fragments"]["overview_table"], "",
             "## 二、业务与利润支柱地图", "",
             "| 利润支柱 | 业务实质 | 拆分理由 | 估值路线 |",
             "|---|---|---|---|"]
    for pillar in card["business_pillars"]:
        mapping = pillar.get("calculation_mapping", {})
        route = "exclude" if mapping.get("target") == "exclude" else mapping.get("route", "exclude")
        lines.append(f"| {pillar['name']} | {pillar['business_essence']} | {pillar['split_rationale']} | {route} |")
    lines += ["", "## 三、机构基准与口径审计", "",
              "### 机构一致预期（2026E / 2027E）", "",
              "| 机构 | 报告日期 | 2026E净利（亿） | 2027E净利（亿） | 证据 |", "|---|---|---:|---:|---|"]
    for item in card["consensus"]:
        lines.append(f"| {item['institution']} | {item['report_date']} | {item['np_2026e']} | {item['np_2027e']} | {', '.join(item['source_ids'])} |")
    lines += ["", "### 可比公司锚定", "", "| 锚类型 | 公司/机构 | PE | 增长率 | 毛利率 | 证据 |", "|---|---|---:|---:|---:|---|"]
    for item in card["comparables"]:
        growth = "—" if item.get("growth_rate") is None else f"{item['growth_rate']}%"
        margin = "—" if item.get("gross_margin") is None else f"{item['gross_margin']}%"
        pe_display = "—" if item.get("pe") is None else f"{item['pe']}x"
        lines.append(f"| {item['anchor_type']} | {item['name']} | {pe_display} | {growth} | {margin} | {', '.join(item['source_ids'])} |")
    for index, pillar in enumerate(card["business_pillars"], 1):
        bridge = pillar["profit_bridge"]
        metric_label = {"net_profit": "净利润", "gross_profit": "毛利", "operating_profit": "营业利润"}.get(bridge.get("profit_metric"), "利润")
        lines += ["", f"### 支柱 {index}：{pillar['name']}", "", pillar["business_essence"], "",
                  f"2025A {metric_label}基准：{bridge['base_2025a']} 亿。", "", f"**2026E {metric_label}桥**", ""]
        for item in bridge["items_2026e"]:
            lines.append(f"- {item['item']}：{item['profit_impact']:+.2f} 亿（来源：{', '.join(item['source_ids'])}）")
        lines += ["", f"**2027E {metric_label}桥**", ""]
        for item in bridge["items_2027e"]:
            lines.append(f"- {item['item']}：{item['profit_impact']:+.2f} 亿（来源：{', '.join(item['source_ids'])}）")
        lines += ["", f"估值驱动：{'；'.join(pillar.get('valuation_drivers', []))}"]
        calc_pillar = next((item for item in card.get("calc_params", {}).get("pillars", []) if item.get("name") == pillar["name"]), {})
        if calc_pillar.get("type_b_pipeline"):
            lines += ["", "独立事项已按项目单列于第六节统一估值模型，不在本节重复展示估值。"]
    lines += ["", "## 四、证据与基准模型的重叠检查", ""]
    if card.get("market_divergences"):
        for item in card["market_divergences"]:
            lines += [f"### {item['name']}（{item['pillar']}）", "", f"- 乐观叙事：{item['bull_case']}",
                      f"- 悲观叙事：{item['bear_case']}", f"- 分歧根因：{item['root_cause']}",
                      f"- 我方判断：{item['our_judgement']}", f"- 定价状态：{item['pricing_status']}；估值映射：{json.dumps(item['calculation_mapping'], ensure_ascii=False)}",
                      f"- 证据：{', '.join(item['source_ids'])}", ""]
    else:
        lines += ["未发现足以改变统一估值模型的市场分歧。", ""]
    lines += ["## 五、独立事项研究状态", ""]
    for item in card["narrative_options"]:
        included = "计入估值" if item.get("included_in_valuation") else "仅观察，未计入估值"
        if item.get("quantification_status") == "quantitative":
            valuation_inputs = f"{item['potential_profit']} 亿 / {item['pe']}x / {item['probability']:.0%}"
        else:
            valuation_inputs = "未量化 / 未量化 / 不适用"
        lines += [f"### {item['name']}（{included}）", "", f"- 业务实质：{item['business_essence']}",
                  f"- 定价状态：{item['pricing_status']}",
                  f"- 潜在利润 / PE / 概率：{valuation_inputs}",
                  f"- 估值映射：{json.dumps(item.get('calculation_mapping', {}), ensure_ascii=False)}", f"- 证据：{', '.join(item['source_ids'])}", ""]
    lines += ["## 六、机构基准估值模型", "", result["report_fragments"]["matrix_summary"], "",
              f"2026E 合计：悲观 {total['pessimistic']:.2f} 亿 / 基准 {total['base']:.2f} 亿 / 乐观 {total['optimistic']:.2f} 亿。", "",
              "### 公司级 PE 交叉检查（不直接套用于各支柱）", "", result["pe_2026e"].get("details", "无 PE 计算详情"), "",
              "## 七、验证节点与催化剂日历", "", "| 时间 | 事件 | 支柱 | 验证成功意味着 | 验证失败意味着 | 证据 |", "|---|---|---|---|---|---|"]
    for item in card["verification_nodes"]:
        lines.append(f"| {item['timeframe']} | {item['event']} | {item['pillar']} | {item['success_meaning']} | {item['failure_meaning']} | {', '.join(item['source_ids'])} |")
    lines += ["", "## 八、风险与更新记录", ""]
    for item in card.get("risks", []):
        lines.append(f"- 风险：{item.get('statement', '')}（来源：{', '.join(item.get('source_ids', []))}）")
    lines += ["", "### 反向检查", "", result["report_fragments"]["reverse_check"], "",
              "### 已验证事实", ""]
    for fact in card.get("facts", []):
        lines.append(f"- {fact.get('statement', '')}（来源：{', '.join(fact.get('source_ids', []))}）")
    lines += ["", "## 证据目录", "", "| ID | 类型 | 缓存文件 |", "|---|---|---|"]
    for item in evidence:
        lines.append(f"| {item['source_id']} | {item['source_type']} | `{item['cache_path']}` |")
    lines += [""]
    return "\n".join(lines)


def update_index(code, name, status, run_at, consensus_stage):
    path = Path(VALUATION_INDEX_PATH)
    fields = ["股票代码", "股票名称", "研究状态", "阶段零状态", "阶段一状态", "阶段二状态", "阶段三状态", "阶段四状态", "阶段五状态", "上次完成阶段", "数据截至报告期", "首次覆盖日期", "最后更新日期"]
    rows = []
    if path.exists():
        with path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
            fields = list(rows[0].keys()) if rows else fields
    encoded = f'="{code}"'
    row = next((item for item in rows if item.get("股票代码", "").replace('="', '').replace('"', '') == code), None)
    if row is None:
        row = {field: "" for field in fields}; rows.append(row)
        row["首次覆盖日期"] = run_at[:10]
    row.update({"股票代码": encoded, "股票名称": name, "研究状态": status,
                "阶段零状态": "done", "阶段一状态": "done", "阶段二状态": consensus_stage,
                "阶段三状态": "done", "阶段四状态": "done", "阶段五状态": "done",
                "上次完成阶段": "5", "最后更新日期": run_at[:10]})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields); writer.writeheader(); writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description="模型三 v3：按旧指令卡逐阶段执行的可审计估值流水线")
    parser.add_argument("--code", required=True, help="六位股票代码")
    parser.add_argument("--name", help="股票名称；briefing 未含名称时使用")
    parser.add_argument("--evidence-dir", default=str(RESEARCH_DIR), help="已缓存的检索 JSON 目录")
    parser.add_argument("--refresh-evidence", action="store_true", help="忽略检索缓存，重新执行全部固定查询")
    parser.add_argument("--no-fetch-evidence", action="store_true", help="不联网，只复用已有检索缓存")
    parser.add_argument("--search-cache-hours", type=int, default=24, help="成功检索缓存有效期（默认 24 小时）")
    parser.add_argument("--fetch-only", action="store_true", help="只执行检索、证据筛选与归档，不调用 LLM 或计算引擎")
    parser.add_argument("--research-card", help="跳过 LLM，使用已审核 research_card.json")
    parser.add_argument("--repair-research-card", help="仅调用阶段五结构修复节点，输入已归档的原始研究卡")
    parser.add_argument("--audit-research-card", help="对已归档研究卡执行项目覆盖与估值语义审计")
    parser.add_argument("--resume-run", help="从指定运行包续跑；已完成阶段直接复用，不重复调用 LLM")
    parser.add_argument("--rerun-stage3", action="store_true", help="仅与 --resume-run 联用：复用阶段一二，按当前通用契约重跑阶段三至五")
    parser.add_argument("--rerun-stage5", action="store_true", help="仅与 --resume-run 联用：保留前四阶段，只重跑研究卡映射")
    parser.add_argument("--dry-run", action="store_true", help="只生成运行包和证据清单，不调用 LLM/引擎")
    parser.add_argument("--no-publish", action="store_true", help="完成研究和计算但不覆盖报告、索引、排名或 Dashboard")
    args = parser.parse_args()
    if sum(bool(value) for value in (args.research_card, args.repair_research_card, args.audit_research_card)) > 1:
        raise PipelineError("--research-card、--repair-research-card、--audit-research-card 不能同时使用")
    if (args.rerun_stage3 or args.rerun_stage5) and not args.resume_run:
        raise PipelineError("--rerun-stage3/--rerun-stage5 必须与 --resume-run 联用")
    code = args.code.strip()
    if not re.fullmatch(r"\d{6}", code):
        raise PipelineError("--code 必须为六位代码")
    if args.search_cache_hours < 1:
        raise PipelineError("--search-cache-hours 必须不少于 1")
    load_dotenv()
    started = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    run_dir = Path(args.resume_run).resolve() if args.resume_run else RUNS_DIR / f"{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    if args.resume_run and not run_dir.is_dir():
        raise PipelineError(f"--resume-run 不是有效运行包目录: {run_dir}")
    run_dir.mkdir(parents=True, exist_ok=True)
    os.environ["VALUATION_PROGRESS_RUN_DIR"] = str(run_dir)
    os.environ.setdefault("VALUATION_LLM_TRACE_DIR", str(run_dir / "llm_traces"))
    manifest = read_json(run_dir / "manifest.json") if args.resume_run and (run_dir / "manifest.json").exists() else {"version": "valuation-v3", "code": code, "started_at": started, "status": "running", "stages": {}}
    try:
        if args.resume_run and (run_dir / "briefing.json").exists():
            briefing_path, briefing = run_dir / "briefing.json", read_json(run_dir / "briefing.json")
        else:
            briefing_path, briefing = latest_briefing(code)
        name = str(args.name or briefing.get("_meta", {}).get("name") or briefing.get("meta", {}).get("name") or "")
        if not name or name == code:
            name = name_from_index(code)
        if not name:
            raise PipelineError("briefing 缺少股票名称")
        manifest.update({"version": "valuation-v3", "code": code, "name": name, "status": "running"})
        manifest.setdefault("started_at", started)
        manifest.pop("error", None)
        if args.rerun_stage3:
            for stage_key in ("阶段三", "stage_3_expectations", "stage_4_catalysts", "stage_5_research_core", "stage_5_research_options",
                              "stage_5_research_validation", "stage_5_research", "stage_5_mapping", "stage_5_assembly",
                              "parameter_validation", "calculation", "render", "dashboard"):
                manifest.setdefault("stages", {})[stage_key] = {"status": "pending", "reason": "--rerun-stage3"}
        elif args.rerun_stage5 or args.repair_research_card or args.audit_research_card:
            for stage_key in ("stage_5_research_core", "stage_5_research_options", "stage_5_research_validation",
                              "stage_5_research", "stage_5_mapping_valuation", "stage_5_mapping_pillars", "stage_5_mapping_adjustments",
                              "stage_5_mapping", "stage_5_assembly", "parameter_validation", "calculation", "render", "dashboard"):
                manifest.setdefault("stages", {})[stage_key] = {"status": "pending", "reason": "--rerun-stage5"}
        write_json(run_dir / "manifest.json", manifest)
        subprocess.run([sys.executable, "scripts/dashboard_valuation.py", "--progress"], cwd=ROOT, capture_output=True)
        quote = current_market_quote(code, name)
        briefing["market_quote"] = quote
        run_briefing_path = run_dir / "briefing.json"
        write_json(run_briefing_path, briefing)
        manifest["stages"]["market_quote"] = {"status": "done", "updated_at": datetime.now().isoformat(timespec="seconds"), **quote}
        evidence_dir = Path(args.evidence_dir)
        fixed_paths = [search_cache_path(evidence_dir, code, topic) for topic, _ in SEARCH_TOPICS]
        search_runs = [] if (args.no_fetch_evidence or args.resume_run) else fetch_evidence(
            code, name, evidence_dir, args.refresh_evidence, args.search_cache_hours, args.dry_run
        )
        manifest["stages"]["search"] = {
            "status": "skipped" if args.no_fetch_evidence else ("planned" if args.dry_run else "done"),
            "items": search_runs,
        }
        if args.resume_run and (run_dir / "evidence.json").exists():
            snapshot = read_json(run_dir / "evidence.json"); evidence, rejected = snapshot["evidence"], snapshot.get("rejected", [])
        else:
            evidence, rejected = build_evidence(run_briefing_path, briefing, code, name, evidence_dir, fixed_paths)
        write_json(run_dir / "evidence.json", {"evidence": evidence, "rejected": rejected})
        manifest["stages"]["evidence"] = {"status": "done", "count": len(evidence), "rejected": rejected}
        if args.dry_run or args.fetch_only:
            manifest["status"] = "dry_run" if args.dry_run else "evidence_ready"
            write_json(run_dir / "manifest.json", manifest); print(run_dir); return
        dynamic_search_runs = []
        if args.audit_research_card:
            card = audit_research_card(read_json(Path(args.audit_research_card)), run_dir, evidence)
            manifest["stages"]["research"] = {"status": "done", "source": "stage5_research_audit"}
            checkpoint(run_dir, "stage_5_audit", "done", file="research_card_raw.json")
        elif args.repair_research_card:
            card = repair_research_card(read_json(Path(args.repair_research_card)), evidence)
            manifest["stages"]["research"] = {"status": "done", "source": "stage5_contract_repair"}
        elif args.research_card:
            card = read_json(Path(args.research_card))
            manifest["stages"]["research"] = {"status": "done", "source": "manual_card"}
        else:
            card, dynamic_search_runs = run_staged_research(
                run_dir, briefing, evidence, code, name, evidence_dir, fixed_paths,
                args.refresh_evidence, args.search_cache_hours, args.dry_run,
                allow_dynamic_fetch=not args.no_fetch_evidence, resume=bool(args.resume_run),
                rerun_stage3=args.rerun_stage3, rerun_stage5=args.rerun_stage5,
            )
            if card is None:
                manifest["stages"]["dynamic_search"] = {"status": "planned", "items": dynamic_search_runs}
                manifest["status"] = "dry_run"; write_json(run_dir / "manifest.json", manifest); print(run_dir); return
            evidence = read_json(run_dir / "evidence.json")["evidence"]
            manifest["stages"].update({
                "stage_1_business": {"status": "done", "file": "stage_1_business.json"},
                "dynamic_search": {"status": "done", "items": dynamic_search_runs, "file": "search_plan.json"},
                "stage_2_consensus": {"status": "done", "file": "stage_2_consensus.json"},
                "stage_2_baseline": {"status": "done" if (run_dir / "stage_2_baseline.json").exists() else "legacy_missing",
                                     "file": "stage_2_baseline.json"},
                "stage_3_expectations": {"status": "done", "file": "stage_3_expectations.json"},
                "stage_4_catalysts": {"status": "done", "file": "stage_4_catalysts.json"},
                "research": {"status": "done", "source": "staged_llm"},
            })
        write_json(run_dir / "research_card_raw.json", card)
        enrich_consensus_from_stage_two(card, run_dir)
        write_json(run_dir / "research_card.json", card)
        checkpoint(run_dir, "stage_5_assembly", "done", file="research_card.json")
        params = parse_params(validate_card_v4(card, briefing, evidence, code, name))
        write_json(run_dir / "calc_params.json", params)
        checkpoint(run_dir, "parameter_validation", "done", file="calc_params.json")
        CALC_PARAMS_DIR.mkdir(parents=True, exist_ok=True)
        write_json(CALC_PARAMS_DIR / f"{code}_{datetime.now().strftime('%y%m%d')}.json", params)
        consensus_state, consensus_count = consensus_status(card)
        manifest["stages"]["consensus"] = {"status": consensus_state, "valid_institutions": consensus_count}
        result = run_valuation(params)
        validate_result_semantics(card, result)
        stage2_path = run_dir / "stage_2_baseline.json"
        if not stage2_path.exists():
            raise PipelineError("统一估值模型缺少 stage_2_baseline.json")
        valuation_model = merge_priced_independent_items(read_json(stage2_path), card, params, {
            "2026e": result.get("pe_2026e", {}), "2027e": result.get("pe_2027e", {}),
        })
        write_json(run_dir / "valuation_model.json", valuation_model)
        result = apply_unified_model_result(result, valuation_model, params)
        write_json(run_dir / "calc_results.json", result)
        checkpoint(run_dir, "calculation", "done", file="calc_results.json")
        CALC_RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        write_json(CALC_RESULTS_DIR / f"{code}_{datetime.now().strftime('%y%m%d')}.json", result)
        if args.no_publish:
            manifest["stages"].update({"calculation": {"status": "done"}, "render": {"status": "skipped", "reason": "--no-publish"}})
        else:
            update_ranking_csv(result["ranking_row"])
            report = render_report(run_dir, briefing, evidence, card, result)
            report_path = Path(VALUATION_REPORTS_DIR) / f"{code}_{name}.md"; report_path.parent.mkdir(parents=True, exist_ok=True)
            report_path.write_text(report, encoding="utf-8")
            update_index(code, name, "v3_done", started, consensus_state)
            manifest["stages"].update({"calculation": {"status": "done"}, "render": {"status": "done", "report": str(report_path.relative_to(ROOT))}})
        disk_manifest = read_json(run_dir / "manifest.json")
        manifest = merge_manifest_stages(disk_manifest, manifest)
        manifest["status"] = "done"; manifest.pop("error", None); write_json(run_dir / "manifest.json", manifest)
        if args.no_publish:
            manifest["stages"]["dashboard"] = {"status": "skipped", "reason": "--no-publish"}
        else:
            dashboard = subprocess.run(
                [sys.executable, "scripts/dashboard_valuation.py", "--date", datetime.fromisoformat(started).strftime("%y%m%d")],
                cwd=ROOT, text=True, capture_output=True,
            )
            manifest["stages"]["dashboard"] = {
                "status": "done" if dashboard.returncode == 0 else "failed",
                "output": dashboard.stdout.strip(), "reason": dashboard.stderr.strip(),
            }
        write_json(run_dir / "manifest.json", manifest)
        if not args.no_publish and manifest["stages"]["dashboard"]["status"] != "done":
            raise PipelineError("Dashboard 发布失败: " + (manifest["stages"]["dashboard"].get("reason") or "unknown"))
        subprocess.run([sys.executable, "scripts/dashboard_valuation.py", "--progress"], cwd=ROOT, capture_output=True)
        print("✅ 模型三 v3 完成（未发布）" if args.no_publish else f"✅ 模型三 v3 完成: {report_path}")
    except Exception as exc:
        if (run_dir / "manifest.json").exists():
            disk_manifest = read_json(run_dir / "manifest.json")
            manifest = merge_manifest_stages(disk_manifest, manifest)
        manifest["status"] = "failed"; manifest["error"] = str(exc); write_json(run_dir / "manifest.json", manifest)
        subprocess.run([sys.executable, "scripts/dashboard_valuation.py", "--progress"], cwd=ROOT, capture_output=True)
        print(f"❌ 模型三 v3 失败（已保留运行包）: {exc}", file=sys.stderr); sys.exit(2)


if __name__ == "__main__":
    main()
