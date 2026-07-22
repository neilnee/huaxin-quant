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
from scripts.calc_valuation import parse_params, run_valuation, update_ranking_csv
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
    ("comparables", "{name} {code} 可比公司 估值 PE 增长 毛利率"),
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
    manifest.setdefault("stages", {})[stage] = {"status": status, **extra}
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
        if current is None or rank.get(value.get("status"), -1) >= rank.get(current.get("status"), -1):
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


def llm_stage(stage, task, inputs, evidence, include_full_evidence=True):
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
        "max_tokens": int(os.environ.get("VALUATION_LLM_MAX_TOKENS", "16000")),
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


def run_staged_research(run_dir, briefing, evidence, code, name, evidence_dir, fixed_paths, refresh, max_age_hours, dry_run, allow_dynamic_fetch=True, resume=False, rerun_stage3=False, rerun_stage5=False):
    """Execute the original five-stage research process as separate LLM nodes."""
    stage1_evidence = select_topic_evidence(evidence, "business", "annual", "主营")
    if resume and (run_dir / "stage_1_business.json").exists():
        stage_one = read_json(run_dir / "stage_1_business.json")
        stage1_readings = read_json(run_dir / "stage_1_readings.json")
    else:
        stage1_readings = read_evidence_batches("阶段一", "识别业务、资产平台、并购、REITs、产能和海外项目。", {"briefing": briefing}, stage1_evidence)
        write_json(run_dir / "stage_1_readings.json", stage1_readings)
        stage_one = llm_stage("阶段一：业务拆解与利润测算", (
            "完整识别业务线、资产平台、并购/重组、REITs/资产证券化、产能和海外项目；"
            "逐支柱给出2025A→2026E→2027E利润桥、A~F路线和拆分/合并原因。"
            "必须输出 search_plan[]，将每个重大项目转为包含公司名和代码的专题检索 query。"
        ), {"briefing": briefing, "code": code, "name": name, "source_readings": stage1_readings}, evidence_catalog(stage1_evidence), False)
        write_json(run_dir / "stage_1_business.json", stage_one)
        checkpoint(run_dir, "stage_1_business", file="stage_1_business.json")
    plan = read_json(run_dir / "search_plan.json") if resume and (run_dir / "search_plan.json").exists() else dynamic_search_plan(stage_one, code, name)
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
    else:
        stage2_readings = read_evidence_batches("阶段二", "提取机构双年盈利预测、目标价、估值方法和可比公司指标。", {"stage_1_business": stage_one}, stage2_evidence)
        write_json(run_dir / "stage_2_readings.json", stage2_readings)
        stage_two = llm_stage("阶段二：机构共识、可比与分歧", (
        "逐篇提取不少于三家未过期机构的2026E和2027E营收、净利、PE、目标价和关键假设；"
        "建立可比公司表；完整解释共识分歧、业务线分歧和Layer 2取值，不能只给均值。"
        ), {"briefing": briefing, "stage_1_business": stage_one, "source_readings": stage2_readings}, evidence_catalog(stage2_evidence), False)
        write_json(run_dir / "stage_2_consensus.json", stage_two)
        checkpoint(run_dir, "stage_2_consensus", file="stage_2_consensus.json")
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
        "逐专题阅读公告、REITs/资产证券化、并购、产能、海外、订单、管理层和产业链证据。"
        "每个项目必须明确属于主营利润、Layer 2分歧、Layer 3未定价期权或排除；"
        "对每个项目输出 classification(core_operating|asset_pipeline|narrative_option|non_recurring),"
        "valuation_route(A|A1|B|C|D|E|F), profit_timing(realized|contracted|pipeline|long_term),"
        "accounting_treatment(recurring|non_recurring|consolidated)，以及利润、倍数、概率、证据和不成立条件。"
        ), {"briefing": briefing, "stage_1_business": stage_one, "stage_2_consensus": stage_two, "search_plan": plan, "source_readings": stage3_readings}, evidence_catalog(stage3_evidence), False)
        write_json(run_dir / "stage_3_expectations.json", stage_three)
        checkpoint(run_dir, "stage_3_expectations", file="stage_3_expectations.json")
    # Stage four reuses project-level readings and only reasons about verification.
    # It must not spend another pass rereading the same source documents.
    stage4_readings = {"reused_from": "stage_3_readings.json", "source_count": len(stage3_evidence)}
    if resume and not rerun_stage3 and (run_dir / "stage_4_catalysts.json").exists():
        stage_four = read_json(run_dir / "stage_4_catalysts.json")
    else:
        write_json(run_dir / "stage_4_readings.json", stage4_readings)
        stage_four = llm_stage("阶段四：催化剂与验证", (
        "为每条主营假设、Layer 2分歧和Layer 3项目建立时间、事件、验证成功/失败含义、估值影响和风险。"
        "输出催化剂日历、verification_nodes、risks，不得遗漏重大项目。"
        ), {"stage_1_business": stage_one, "stage_2_consensus": stage_two, "stage_3_expectations": stage_three,
            "stage_3_readings": stage3_readings, "reuse_note": stage4_readings}, evidence_catalog(stage3_evidence), False)
        write_json(run_dir / "stage_4_catalysts.json", stage_four)
        checkpoint(run_dir, "stage_4_catalysts", file="stage_4_catalysts.json")
    if resume and not rerun_stage3 and not rerun_stage5 and (run_dir / "research_card.json").exists():
        final = read_json(run_dir / "research_card.json")
    else:
        final = llm_stage("阶段五：研究结论与参数映射", (
            "不新增事实，只合并四阶段结论。严格输出 status='ready'；investment_thesis 为含 market_trade,company_stage,core_judgement,source_ids 的对象；"
            "输出 valuation_inputs={growth_quality(structural|cyclical|mature),market_position(leader|mid|follower),"
            "comparable_pe_lower,comparable_pe_median,comparable_pe_upper,company_growth_rate,comp_growth_rate,qualitative_adjustments,source_ids}。"
            "business_pillars[] 每项必须含 name,business_essence,split_rationale,profit_bridge,source_ids,classification,profit_timing,accounting_treatment,"
            "calculation_mapping={target:pillar|exclude,pillar_id,route:A|A1|B|C|D|E|F,engine_values}；engine_values 使用估值引擎支柱字段。"
            "market_divergences[] 每项必须含 name,pillar,bull_case,bear_case,root_cause,our_judgement,pricing_status,source_ids、上述三个枚举及 calculation_mapping；"
            "narrative_options[] 每项必须含 name,pillar,business_essence,pricing_status,potential_profit,pe,probability,included_in_valuation,verification_nodes,source_ids、上述三个枚举及 calculation_mapping；"
            "其 target 只能为 layer2|type_b_pipeline|layer3_item|exclude，非 exclude 时须给 pillar_id 与 engine_values。"
            "layer2 engine_values={pessimistic,base,optimistic,description}；type_b_pipeline={project_profit,pe,probability}；"
            "layer3_item={pessimistic,base,optimistic,probability}。另输出 consensus,comparables,verification_nodes,facts,assumptions,risks,catalysts。"
            "consensus 必须是逐机构数组且至少3项，每项={institution,report_date:YYYY-MM-DD,np_2026e,np_2027e,revenue_2026e,revenue_2027e,pe_2026e,pe_2027e,target_price,source_ids}，"
            "禁止输出均值汇总对象；comparables 必须是至少2项的数组，每项={name,pe,growth_rate,gross_margin,source_ids}。"
            "profit_bridge 必须是对象={base_2025a,items_2026e:[{item,profit_impact,source_ids}],items_2027e:[{item,profit_impact,source_ids}]}。"
            "qualitative_adjustments 必须是数组，每项={item,adjustment} 且 adjustment 为-0.15到0.15的小数；不得输出说明字符串。"
            "verification_nodes 必须为扁平数组，每项={node_id,timeframe,event,pillar,success_meaning,failure_meaning,source_ids}；"
            "各路线 engine_values 必填字段：A={ebitda_2026e,ebitda_2027e,comparable_ev_ebitda,net_debt}；"
            "A1/B/C={consensus_np_2026e,consensus_np_2027e,consensus_np_lower_2026e,consensus_np_upper_2026e,consensus_np_lower_2027e,consensus_np_upper_2027e,deducted_np}；"
            "D/F={net_assets,roe,comparable_pb_median,comparable_pb_lower,comparable_pb_upper,comparable_roe}；"
            "E={revenue_2026e,revenue_2027e,gross_margin,comparable_ps_median,comparable_ps_lower,comparable_ps_upper,comparable_gross_margin}。"
            "禁止输出 calc_params，禁止根据公司名、行业词或项目名称自行决定映射。"
        ), {"briefing": briefing, "stage_1_business": stage_one, "stage_2_consensus": stage_two,
            "stage_3_expectations": stage_three, "stage_4_catalysts": stage_four}, evidence_catalog(evidence), False)
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


def enrich_consensus_from_stage_two(card, run_dir):
    """Preserve dual-year institution forecasts when stage five omits copied fields."""
    path = run_dir / "stage_2_consensus.json"
    if not path.exists():
        return
    forecasts = read_json(path).get("institution_forecasts", [])
    by_institution = {str(item.get("institution")): item for item in forecasts
                      if isinstance(item, dict) and item.get("institution")}
    for item in card.get("consensus", []) or []:
        if not isinstance(item, dict):
            continue
        source = by_institution.get(str(item.get("institution")))
        if not source:
            continue
        for field in ("revenue_2026e", "revenue_2027e", "net_profit_2026e", "net_profit_2027e", "pe_2026e", "pe_2027e", "target_price"):
            if item.get(field) is None and source.get(field) is not None:
                item[field] = source[field]


CLASSIFICATIONS = {"core_operating", "asset_pipeline", "narrative_option", "non_recurring"}
PROFIT_TIMINGS = {"realized", "contracted", "pipeline", "long_term"}
ACCOUNTING_TREATMENTS = {"recurring", "non_recurring", "consolidated"}
MAPPING_TARGETS = {"pillar", "type_b_pipeline", "layer3_item", "layer2", "exclude"}
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
    params = {field: inputs[field] for field in input_fields}
    params.update({"meta": {"code": code, "name": name, "total_shares": round(total_shares, 6),
                            "current_price": round(current_price, 4), "price_date": price_date,
                            "price_source": price_source},
                   "qualitative_adjustments": inputs.get("qualitative_adjustments", []), "pillars": []})
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
            pillar_id = str(mapping.get("pillar_id", ""))
            if pillar_id not in pillars_by_id:
                raise PipelineError(f"{label}[{index}] 引用了不存在的 pillar_id")
            pillar = pillars_by_id[pillar_id]; values = mapping.get("engine_values")
            if not isinstance(values, dict):
                raise PipelineError(f"{label}[{index}] 缺少 engine_values")
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
    require_sources(card.get("comparables"), "comparables", valid_ids)
    if len(card.get("comparables", [])) < 2:
        raise PipelineError("可比公司不足：至少需要2家")
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
        for year in ("items_2026e", "items_2027e"):
            require_sources(bridge.get(year), f"business_pillars[{index}].profit_bridge.{year}", valid_ids)
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
    nodes = card.get("verification_nodes", []) or []
    require_sources(nodes, "verification_nodes", valid_ids)
    for index, item in enumerate(nodes):
        if not isinstance(item, dict):
            raise PipelineError(f"verification_nodes[{index}] 必须为对象")
        if not all(item.get(field) for field in ("node_id", "timeframe", "event", "pillar", "success_meaning", "failure_meaning")):
            raise PipelineError(f"verification_nodes[{index}] 缺少验证逻辑")
    return build_generic_calc_params(card, briefing, code, name, evidence)


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


def render_report(run_dir, briefing, evidence, card, result):
    meta = result["_meta"]
    total = result["matrix_2026e"]["total"]
    thesis = card["investment_thesis"]
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
        route = pillar.get("calculation_mapping", {}).get("route", "exclude")
        lines.append(f"| {pillar['name']} | {pillar['business_essence']} | {pillar['split_rationale']} | {route} |")
    lines += ["", "## 三、Layer 1 — 一致预期主营", "",
              "### 机构一致预期（2026E / 2027E）", "",
              "| 机构 | 报告日期 | 2026E净利（亿） | 2027E净利（亿） | 证据 |", "|---|---|---:|---:|---|"]
    for item in card["consensus"]:
        lines.append(f"| {item['institution']} | {item['report_date']} | {item['np_2026e']} | {item['np_2027e']} | {', '.join(item['source_ids'])} |")
    lines += ["", "### 可比公司锚定", "", "| 公司 | PE | 增长率 | 毛利率 | 证据 |", "|---|---:|---:|---:|---|"]
    for item in card["comparables"]:
        lines.append(f"| {item['name']} | {item['pe']}x | {item['growth_rate']}% | {item['gross_margin']}% | {', '.join(item['source_ids'])} |")
    for index, pillar in enumerate(card["business_pillars"], 1):
        bridge = pillar["profit_bridge"]
        lines += ["", f"### 支柱 {index}：{pillar['name']}", "", pillar["business_essence"], "",
                  f"2025A 利润基准：{bridge['base_2025a']} 亿。", "", "**2026E 利润桥**", ""]
        for item in bridge["items_2026e"]:
            lines.append(f"- {item['item']}：{item['profit_impact']:+.2f} 亿（来源：{', '.join(item['source_ids'])}）")
        lines += ["", "**2027E 利润桥**", ""]
        for item in bridge["items_2027e"]:
            lines.append(f"- {item['item']}：{item['profit_impact']:+.2f} 亿（来源：{', '.join(item['source_ids'])}）")
        lines += ["", f"估值驱动：{'；'.join(pillar.get('valuation_drivers', []))}"]
        calc_pillar = next((item for item in card.get("calc_params", {}).get("pillars", []) if item.get("name") == pillar["name"]), {})
        if calc_pillar.get("type_b_pipeline"):
            lines += ["", "**Type B 管道（Layer 2）**", "", "| 项目 | 潜在利润（亿） | PE | 概率 |", "|---|---:|---:|---:|"]
            for item in calc_pillar["type_b_pipeline"]:
                lines.append(f"| {item.get('name', '—')} | {item.get('project_profit', '—')} | {item.get('pe', '—')}x | {item.get('probability', 0):.0%} |")
        layer2 = calc_pillar.get("layer2")
        if layer2:
            lines += ["", f"Layer 2 估值调整：悲观 {layer2.get('pessimistic', 0)} 亿 / 基准 {layer2.get('base', 0)} 亿 / 乐观 {layer2.get('optimistic', 0)} 亿。{layer2.get('description', '')}"]
    lines += ["", "## 四、Layer 2 — 市场分歧", ""]
    if card.get("market_divergences"):
        for item in card["market_divergences"]:
            lines += [f"### {item['name']}（{item['pillar']}）", "", f"- 乐观叙事：{item['bull_case']}",
                      f"- 悲观叙事：{item['bear_case']}", f"- 分歧根因：{item['root_cause']}",
                      f"- 我方判断：{item['our_judgement']}", f"- 定价状态：{item['pricing_status']}；估值映射：{item['calc_mapping']}",
                      f"- 证据：{', '.join(item['source_ids'])}", ""]
    else:
        lines += ["未发现足以单列估值调整的市场分歧；Layer 2 不计入额外价值。", ""]
    lines += ["## 五、Layer 3 — 叙事期权与预期差", ""]
    for item in card["narrative_options"]:
        included = "计入估值" if item.get("included_in_valuation") else "仅观察，未计入估值"
        lines += [f"### {item['name']}（{included}）", "", f"- 业务实质：{item['business_essence']}",
                  f"- 定价状态：{item['pricing_status']}",
                  f"- 潜在利润 / PE / 概率：{item['potential_profit']} 亿 / {item['pe']}x / {item['probability']:.0%}",
                  f"- 估值映射：{item.get('calc_mapping', '—')}", f"- 证据：{', '.join(item['source_ids'])}", ""]
    lines += ["## 六、双年三情景估值汇总", "", result["report_fragments"]["matrix_summary"], "",
              f"2026E 合计：悲观 {total['pessimistic']:.2f} 亿 / 基准 {total['base']:.2f} 亿 / 乐观 {total['optimistic']:.2f} 亿。", "",
              "### PE 与情景条件", "", result["pe_2026e"].get("details", "无 PE 计算详情"), "",
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
            for stage_key in ("阶段三", "stage_3_expectations", "stage_4_catalysts", "stage_5_mapping",
                              "parameter_validation", "calculation", "render", "dashboard"):
                manifest.setdefault("stages", {})[stage_key] = {"status": "pending", "reason": "--rerun-stage3"}
        elif args.rerun_stage5 or args.repair_research_card or args.audit_research_card:
            for stage_key in ("stage_5_mapping", "parameter_validation", "calculation", "render", "dashboard"):
                manifest.setdefault("stages", {})[stage_key] = {"status": "pending", "reason": "--rerun-stage5"}
        write_json(run_dir / "manifest.json", manifest)
        subprocess.run([sys.executable, "scripts/dashboard_valuation.py", "--progress"], cwd=ROOT, capture_output=True)
        quote = current_market_quote(code, name)
        briefing["market_quote"] = quote
        run_briefing_path = run_dir / "briefing.json"
        write_json(run_briefing_path, briefing)
        manifest["stages"]["market_quote"] = {"status": "done", **quote}
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
                "stage_3_expectations": {"status": "done", "file": "stage_3_expectations.json"},
                "stage_4_catalysts": {"status": "done", "file": "stage_4_catalysts.json"},
                "research": {"status": "done", "source": "staged_llm"},
            })
        write_json(run_dir / "research_card_raw.json", card)
        enrich_consensus_from_stage_two(card, run_dir)
        write_json(run_dir / "research_card.json", card)
        checkpoint(run_dir, "stage_5_mapping", "done", file="research_card.json")
        params = parse_params(validate_card_v4(card, briefing, evidence, code, name))
        write_json(run_dir / "calc_params.json", params)
        checkpoint(run_dir, "parameter_validation", "done", file="calc_params.json")
        CALC_PARAMS_DIR.mkdir(parents=True, exist_ok=True)
        write_json(CALC_PARAMS_DIR / f"{code}_{datetime.now().strftime('%y%m%d')}.json", params)
        consensus_state, consensus_count = consensus_status(card)
        manifest["stages"]["consensus"] = {"status": consensus_state, "valid_institutions": consensus_count}
        result = run_valuation(params)
        checkpoint(run_dir, "calculation", "done", file="calc_results.json")
        write_json(run_dir / "calc_results.json", result)
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
