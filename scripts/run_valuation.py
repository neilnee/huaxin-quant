#!/usr/bin/env python3
"""Run Model 3 end-to-end with preflight, recovery, locking and verification."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
FINANCIAL_DIR = PROJECT_ROOT / "cache" / "financial"
BRIEFING_DIR = PROJECT_ROOT / "cache" / "briefing"
RUNS_DIR = PROJECT_ROOT / "cache" / "valuation_runs"
LOCK_DIR = PROJECT_ROOT / ".tmp" / "valuation_locks"
CONTROLLER_DIR = PROJECT_ROOT / ".tmp" / "valuation_controller"
VALUATE_SCRIPT = SCRIPTS_DIR / "valuate.py"
PIPELINE_SCRIPT = SCRIPTS_DIR / "valuation_pipeline.py"
DEFAULT_FINANCIAL_MAX_AGE_DAYS = 90
DEFAULT_FINANCIAL_RETRIES = 2
RUNNING_STALE_MINUTES = 20


class ControllerError(RuntimeError):
    pass


@dataclass(frozen=True)
class RecoveryDecision:
    mode: str
    reason: str
    card_path: Path | None = None


def load_dotenv() -> None:
    path = PROJECT_ROOT / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ControllerError(f"无法读取 JSON {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ControllerError(f"JSON 顶层必须为对象: {path}")
    return value


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(PROJECT_ROOT))
    except ValueError:
        return str(path)


def validate_code(value: str) -> str:
    code = str(value or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        raise ControllerError("--code 必须为六位股票代码")
    return code


def financial_files(code: str) -> list[Path]:
    if not FINANCIAL_DIR.exists():
        return []
    return sorted(
        (path for path in FINANCIAL_DIR.glob(f"*{code}*_raw.json") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )


def inspect_financial_cache(code: str, max_age_days: int) -> dict:
    files = financial_files(code)
    if not files:
        return {"status": "missing", "file_count": 0, "stale": True}
    newest = files[0]
    age_days = max(0.0, (datetime.now().timestamp() - newest.stat().st_mtime) / 86400)
    return {
        "status": "stale" if age_days > max_age_days else "ready",
        "file_count": len(files),
        "newest_file": str(newest.relative_to(PROJECT_ROOT)),
        "newest_age_days": round(age_days, 1),
        "stale": age_days > max_age_days,
    }


def check_financial_cache(code: str, max_age_days: int, allow_stale: bool) -> dict:
    state = inspect_financial_cache(code, max_age_days)
    if state["status"] == "missing":
        raise ControllerError(f"{code} 缺少 cache/financial/*_raw.json")
    if state["stale"] and not allow_stale:
        raise ControllerError(
            f"{code} 最新财务缓存已 {state['newest_age_days']:.1f} 天未更新，超过 {max_age_days} 天；"
            "请允许总控自动刷新，或确认仍有效后使用 --allow-stale-financial"
        )
    return {**state, "stale_allowed": bool(state["stale"] and allow_stale)}


def latest_annual_year(now: datetime | None = None) -> int:
    current = now or datetime.now()
    return current.year - 1 if current.month >= 5 else current.year - 2


def build_financial_queries(code: str, name: str | None = None, annual_year: int | None = None) -> list[dict]:
    year = annual_year or latest_annual_year()
    identity = f"{name.strip()} {code}" if name and name.strip() else code
    return [
        {
            "topic": "core_financials",
            "query": (
                f"{identity} 核心财务 {year}年报 营业收入 营收同比增速 归母净利润 归母净利润同比增速 "
                "扣非净利润 扣非净利润同比增速 经营现金流 ROE 资产负债率 总资产 固定资产 "
                "在建工程 研发费用 销售毛利率 销售净利率"
            ),
        },
        {
            "topic": "profit_history",
            "query": (
                f"{identity} 历史趋势 {year - 2}年报 {year - 1}年报 {year}年报 "
                "销售毛利率 销售净利率 营业收入 归母净利润"
            ),
        },
        {
            "topic": "valuation_base",
            "query": (
                f"{identity} 估值基础 {year}年报 总股本 股东权益 有息负债 折旧 存货 商誉 "
                "总市值 市盈率PE(TTM) 市净率PB"
            ),
        },
    ]


def safe_mx_filename(value: str, max_len: int = 80) -> str:
    value = re.sub(r'[<>:"/\\|?*\[\]]', "_", value)
    return value.strip().replace(" ", "_")[:max_len] or "query"


def mx_raw_path(query: str) -> Path:
    return FINANCIAL_DIR / f"mx_data_{safe_mx_filename(query)}_raw.json"


def validate_mx_raw(path: Path, code: str) -> dict:
    raw = read_json(path)
    if raw.get("status") != 0:
        raise ControllerError(f"mx-data 返回失败状态: {raw.get('status')} {raw.get('message', '')}")
    search_result = raw.get("data", {}).get("data", {}).get("searchDataResultDTO", {})
    tables = search_result.get("dataTableDTOList", [])
    if not isinstance(tables, list) or not tables:
        raise ControllerError(f"mx-data raw 缺少 dataTableDTOList: {path}")
    matched = []
    for table in tables:
        if not isinstance(table, dict):
            continue
        tag = table.get("entityTagDTO") or {}
        table_code = str(tag.get("secuCode") or table.get("code") or "").split(".", 1)[0]
        raw_table = table.get("rawTable")
        if table_code == code and isinstance(raw_table, dict) and any(key != "headName" for key in raw_table):
            matched.append(table)
    if not matched:
        raise ControllerError(f"mx-data raw 未包含 {code} 的可解析 rawTable: {path}")
    return {
        "path": display_path(path),
        "table_count": len(matched),
        "question_id": search_result.get("questionId"),
    }


def resolve_mx_data_script(value: str | None = None) -> Path:
    configured = value or os.environ.get("HUAXIN_MX_DATA_SCRIPT")
    path = Path(configured).expanduser() if configured else Path.home() / ".claude" / "skills" / "mx-data" / "mx_data.py"
    if not path.is_file():
        raise ControllerError(f"mx-data 脚本不存在: {path}")
    return path


def plan_financial_data(args: argparse.Namespace, code: str, stage0_required: bool) -> dict:
    if not stage0_required:
        return {"action": "skip", "reason": "复用 briefing，不重新执行阶段零", "queries": []}
    cache = inspect_financial_cache(code, args.financial_max_age_days)
    force = bool(args.refresh or args.refresh_financial)
    needs_fetch = force or cache["status"] == "missing" or (cache["stale"] and not args.allow_stale_financial)
    queries = build_financial_queries(code, args.name)
    if needs_fetch and args.skip_financial_fetch:
        checked = check_financial_cache(code, args.financial_max_age_days, args.allow_stale_financial)
        return {"action": "reuse", "reason": "--skip-financial-fetch", "cache": checked, "queries": []}
    if needs_fetch:
        script = resolve_mx_data_script(args.mx_data_script)
        if not os.environ.get("MX_APIKEY", "").strip():
            raise ControllerError("缺少 MX_APIKEY，无法自动刷新财务缓存")
        reason = "强制刷新" if force else ("缓存缺失" if cache["status"] == "missing" else "缓存过期")
        return {"action": "fetch", "reason": reason, "cache_before": cache, "script": str(script), "queries": queries}
    checked = check_financial_cache(code, args.financial_max_age_days, args.allow_stale_financial)
    return {"action": "reuse", "reason": "缓存有效", "cache": checked, "queries": []}


def expected_briefing_path(code: str) -> Path:
    return BRIEFING_DIR / f"{code}_{datetime.now().strftime('%y%m%d')}.json"


def validate_briefing(path: Path, code: str) -> dict:
    if not path.exists():
        raise ControllerError(f"阶段零未生成当日 briefing: {path}")
    briefing = read_json(path)
    meta = briefing.get("_meta", {})
    if str(meta.get("code", "")) != code:
        raise ControllerError(f"briefing 股票代码不匹配: 期望 {code}，实际 {meta.get('code')}")
    snapshot = briefing.get("valuation_snapshot", {})
    try:
        shares = float(snapshot.get("total_shares"))
    except (TypeError, ValueError) as exc:
        raise ControllerError("briefing.valuation_snapshot.total_shares 缺失或无效") from exc
    route = briefing.get("decision_tree", {}).get("route")
    if shares <= 0 or route not in {"A", "A1", "B", "C", "D", "E", "F"}:
        raise ControllerError("briefing 缺少有效总股本或决策树路线")
    return {
        "path": str(path.relative_to(PROJECT_ROOT)),
        "name": meta.get("name"),
        "data_date": meta.get("data_date"),
        "route": route,
        "total_shares": shares,
    }


def valuation_run_paths(code: str) -> list[Path]:
    if not RUNS_DIR.exists():
        return []
    pattern = re.compile(rf"^{re.escape(code)}_\d{{8}}_\d{{6}}$")
    return sorted(
        (path for path in RUNS_DIR.iterdir() if path.is_dir() and pattern.fullmatch(path.name)),
        key=lambda path: path.name,
        reverse=True,
    )


def select_resume_run(code: str, explicit: str | None = None) -> Path:
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir() or not (path / "manifest.json").exists():
            raise ControllerError(f"不是有效的模型三运行包: {path}")
        manifest = read_json(path / "manifest.json")
        if str(manifest.get("code")) != code:
            raise ControllerError(f"运行包代码 {manifest.get('code')} 与 --code {code} 不一致")
        return path
    paths = valuation_run_paths(code)
    if not paths:
        raise ControllerError(f"{code} 没有可续跑的历史运行包")
    path = paths[0]
    manifest_path = path / "manifest.json"
    manifest = read_json(manifest_path)
    status = manifest.get("status")
    if status == "done":
        raise ControllerError(f"{code} 最新运行包已经完成，无需续跑: {path}")
    if status == "running":
        age_minutes = (datetime.now().timestamp() - manifest_path.stat().st_mtime) / 60
        if age_minutes < RUNNING_STALE_MINUTES:
            raise ControllerError(
                f"{code} 最新运行包仍可能在执行（manifest {age_minutes:.1f} 分钟前更新）；"
                "请先使用 --status 检查，确认中断后再显式传 --resume-run"
            )
    return path


def choose_recovery(run_dir: Path, requested: str = "auto") -> RecoveryDecision:
    if requested in {"stage3", "stage5"}:
        return RecoveryDecision(requested, f"用户显式要求从 {requested} 重跑")
    manifest = read_json(run_dir / "manifest.json")
    error = str(manifest.get("error") or "")
    completed_research = all(
        (run_dir / filename).exists()
        for filename in (
            "stage_1_business.json",
            "stage_2_consensus.json",
            "stage_3_expectations.json",
            "stage_4_catalysts.json",
        )
    )
    raw_card = run_dir / "research_card_raw.json"
    card = run_dir / "research_card.json"
    stage5_research = run_dir / "stage_5_research.json"
    stage5_mapping = run_dir / "stage_5_mapping.json"
    contract_error = any(
        token in error
        for token in (
            "缺少",
            "无效",
            "必须",
            "pillar_id",
            "PE 区间",
            "百分比",
            "参数校验",
            "calculation_mapping",
        )
    )
    if requested == "repair":
        if not raw_card.exists():
            raise ControllerError("--recovery repair 需要 research_card_raw.json")
        return RecoveryDecision("repair", "用户显式要求修复研究卡计算契约", raw_card)
    if completed_research and (not stage5_research.exists() or not stage5_mapping.exists()):
        return RecoveryDecision("stage5", "阶段五A/五B子阶段尚未完整形成")
    if completed_research and contract_error:
        return RecoveryDecision("stage5", "阶段五研究或参数映射未通过新契约门禁")
    if completed_research and not card.exists():
        return RecoveryDecision("stage5", "前四阶段完整但研究卡尚未形成")
    return RecoveryDecision("resume", "复用已完成节点并继续缺失阶段")


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


class StockLock:
    def __init__(self, code: str):
        self.path = LOCK_DIR / f"{code}.lock"
        self.acquired = False

    def acquire(self) -> None:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"pid": os.getpid(), "started_at": datetime.now().isoformat(timespec="seconds")})
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            try:
                owner = read_json(self.path)
                owner_pid = int(owner.get("pid", 0))
            except (ControllerError, TypeError, ValueError):
                owner_pid = 0
            if owner_pid and pid_is_alive(owner_pid):
                raise ControllerError(f"同一股票已有总控任务运行，PID={owner_pid}: {self.path}")
            self.path.unlink(missing_ok=True)
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        self.acquired = True

    def release(self) -> None:
        if self.acquired:
            self.path.unlink(missing_ok=True)
            self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.release()


def run_command(command: list[str], label: str, log_path: Path) -> int:
    print(f"\n{'=' * 68}\n{label}\n$ {shlex.join(command)}\n{'=' * 68}")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {label}\n$ {shlex.join(command)}\n")
        process = subprocess.Popen(
            command,
            cwd=PROJECT_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=1,
        )
        assert process.stdout is not None
        try:
            for line in process.stdout:
                print(line, end="")
                log.write(line)
            return process.wait()
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise


def fetch_financial_data(plan: dict, code: str, retries: int, log_path: Path) -> list[dict]:
    if plan.get("action") != "fetch":
        return []
    FINANCIAL_DIR.mkdir(parents=True, exist_ok=True)
    results = []
    script = str(plan["script"])
    for item in plan["queries"]:
        query = item["query"]
        raw_path = mx_raw_path(query)
        last_error = "unknown"
        for attempt in range(1, retries + 1):
            started_ns = time.time_ns()
            command = [sys.executable, script, query, str(FINANCIAL_DIR)]
            rc = run_command(command, f"财务数据 · {item['topic']}（尝试 {attempt}/{retries}）", log_path)
            if rc == 0:
                try:
                    if not raw_path.exists() or raw_path.stat().st_mtime_ns < started_ns:
                        raise ControllerError(f"mx-data 未生成本次查询的 raw JSON: {raw_path}")
                    validated = validate_mx_raw(raw_path, code)
                except ControllerError as exc:
                    last_error = str(exc)
                else:
                    results.append({**item, "attempt": attempt, **validated})
                    break
            else:
                last_error = f"mx-data 子进程退出码 {rc}"
            if attempt < retries:
                time.sleep(min(2 ** attempt, 8))
        else:
            raise ControllerError(f"财务查询 {item['topic']} 失败（已重试 {retries} 次）: {last_error}")
    return results


def build_pipeline_command(args: argparse.Namespace, code: str, run_dir: Path | None, decision: RecoveryDecision | None) -> list[str]:
    command = [sys.executable, "scripts/valuation_pipeline.py", "--code", code]
    if args.name:
        command.extend(["--name", args.name])
    if run_dir:
        command.extend(["--resume-run", str(run_dir)])
    if decision:
        if decision.mode == "stage3":
            command.append("--rerun-stage3")
        elif decision.mode == "stage5":
            command.append("--rerun-stage5")
        elif decision.mode == "repair":
            command.extend(["--repair-research-card", str(decision.card_path)])
    if args.refresh:
        command.append("--refresh-evidence")
    if args.no_fetch_evidence:
        command.append("--no-fetch-evidence")
    if args.no_publish:
        command.append("--no-publish")
    command.extend(["--search-cache-hours", str(args.search_cache_hours)])
    return command


def verify_terminal_run(run_dir: Path, no_publish: bool) -> dict:
    manifest = read_json(run_dir / "manifest.json")
    if manifest.get("status") != "done":
        raise ControllerError(f"主流水线未完成: {manifest.get('error') or manifest.get('status')}")
    required = ("briefing.json", "evidence.json", "stage_5_research.json", "stage_5_mapping.json",
                "research_card.json", "calc_params.json", "calc_results.json")
    missing = [name for name in required if not (run_dir / name).exists()]
    if missing:
        raise ControllerError("完成运行包缺少文件: " + ", ".join(missing))
    stages = manifest.get("stages", {})
    if not no_publish:
        render_status = stages.get("render", {}).get("status")
        dashboard_status = stages.get("dashboard", {}).get("status")
        if render_status != "done" or dashboard_status != "done":
            raise ControllerError(
                f"研究计算已完成但发布未完成: render={render_status}, dashboard={dashboard_status}"
            )
    return {
        "manifest_status": manifest.get("status"),
        "consensus": stages.get("consensus", {}),
        "render": stages.get("render", {}),
        "dashboard": stages.get("dashboard", {}),
    }


def print_status(code: str) -> int:
    paths = valuation_run_paths(code)[:5]
    if not paths:
        print(f"{code} 尚无模型三运行包")
        return 0
    print(f"{code} 最近 {len(paths)} 次模型三运行：")
    for path in paths:
        try:
            manifest = read_json(path / "manifest.json")
        except ControllerError as exc:
            print(f"  {path.name}: invalid_manifest ({exc})")
            continue
        stages = manifest.get("stages", {})
        finished = [key for key, value in stages.items() if isinstance(value, dict) and value.get("status") == "done"]
        error = str(manifest.get("error") or "")
        print(
            f"  {path.name}: {manifest.get('status', 'unknown')} | "
            f"完成节点={len(finished)} | {error[:100]}"
        )
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="模型三端到端总控：阶段零、五阶段研究、恢复、校验与发布")
    parser.add_argument("--code", required=True, help="六位股票代码")
    parser.add_argument("--name", help="股票名称；本地索引和 briefing 无名称时使用")
    parser.add_argument("--refresh", action="store_true", help="强制刷新财务、briefing、固定与动态检索证据")
    parser.add_argument("--refresh-financial", action="store_true", help="强制调用 mx-data 刷新财务后继续完整流程")
    parser.add_argument("--skip-financial-fetch", action="store_true", help="禁止调用 mx-data，只允许使用现有财务缓存")
    parser.add_argument("--mx-data-script", help="mx_data.py 路径；默认读取 HUAXIN_MX_DATA_SCRIPT")
    parser.add_argument("--resume", action="store_true", help="自动选择该股票最新未完成运行包续跑")
    parser.add_argument("--resume-run", help="显式指定要续跑的运行包目录")
    parser.add_argument("--recovery", choices=("auto", "resume", "stage3", "stage5", "repair"), default="auto", help="续跑策略，默认根据运行包自动判断")
    parser.add_argument("--no-fetch-evidence", action="store_true", help="续跑时禁止联网，只使用运行包和成功缓存")
    parser.add_argument("--no-publish", action="store_true", help="完成研究和计算但不发布报告、索引和 Dashboard")
    parser.add_argument("--skip-stage0", action="store_true", help="新运行复用当日 briefing，不重新执行阶段零")
    parser.add_argument("--allow-stale-financial", action="store_true", help="确认旧财务缓存仍有效并允许继续")
    parser.add_argument("--financial-max-age-days", type=int, default=DEFAULT_FINANCIAL_MAX_AGE_DAYS, help="财务缓存最大时效，默认 90 天")
    parser.add_argument("--financial-retries", type=int, default=DEFAULT_FINANCIAL_RETRIES, help="单项财务查询重试次数，默认 2 次")
    parser.add_argument("--search-cache-hours", type=int, default=24, help="检索缓存有效期，默认 24 小时")
    parser.add_argument("--status", action="store_true", help="只显示该股票最近运行状态")
    parser.add_argument("--dry-run", action="store_true", help="只执行只读预检并打印计划命令")
    return parser.parse_args()


def validate_args(args: argparse.Namespace) -> None:
    if args.resume and args.resume_run:
        raise ControllerError("--resume 与 --resume-run 不能同时使用")
    if args.refresh and (args.resume or args.resume_run):
        raise ControllerError("--refresh 只用于新运行；续跑必须保持原运行包证据边界")
    if args.refresh_financial and (args.resume or args.resume_run or args.skip_stage0):
        raise ControllerError("--refresh-financial 只能用于执行阶段零的新运行")
    if args.skip_financial_fetch and (args.refresh or args.refresh_financial):
        raise ControllerError("--skip-financial-fetch 不能与 --refresh/--refresh-financial 联用")
    if args.recovery != "auto" and not (args.resume or args.resume_run):
        raise ControllerError("--recovery 只能与 --resume/--resume-run 联用")
    if args.no_fetch_evidence and not (args.resume or args.resume_run):
        raise ControllerError("--no-fetch-evidence 仅用于续跑")
    if args.financial_max_age_days < 1 or args.financial_retries < 1 or args.search_cache_hours < 1:
        raise ControllerError("缓存时效参数必须大于等于 1")


def main() -> int:
    args = parse_args()
    code = validate_code(args.code)
    validate_args(args)
    if args.status:
        return print_status(code)
    load_dotenv()
    for script in (VALUATE_SCRIPT, PIPELINE_SCRIPT):
        if not script.exists():
            raise ControllerError(f"缺少执行脚本: {script}")

    started = datetime.now().isoformat(timespec="seconds")
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = CONTROLLER_DIR / f"{code}_{stamp}.log"
    summary_path = CONTROLLER_DIR / f"{code}_{stamp}.json"
    resume_run = select_resume_run(code, args.resume_run) if (args.resume or args.resume_run) else None
    decision = choose_recovery(resume_run, args.recovery) if resume_run else None
    stage0_required = resume_run is None and not args.skip_stage0
    financial_plan = plan_financial_data(args, code, stage0_required)
    stage0_command = [sys.executable, "scripts/valuate.py", "--code", code]
    pipeline_command = build_pipeline_command(args, code, resume_run, decision)

    print(f"模型三总控计划: {code}{' ' + args.name if args.name else ''}")
    if financial_plan["action"] == "fetch":
        print(f"  财务数据: 调用 mx-data（{financial_plan['reason']}，{len(financial_plan['queries'])} 项查询）")
        print(f"  mx-data: {financial_plan['script']}")
    elif financial_plan["action"] == "reuse":
        cache = financial_plan["cache"]
        print(f"  财务数据: 复用 {cache['file_count']} 个缓存，最新 {cache['newest_age_days']} 天")
    else:
        print("  财务数据: 未检查（复用 briefing）")
    print(f"  模式: {'续跑 ' + resume_run.name if resume_run else '新运行'}")
    if decision:
        print(f"  恢复策略: {decision.mode}（{decision.reason}）")
    print(f"  阶段零: {'执行' if stage0_required else '复用'}")
    print(f"  发布: {'否' if args.no_publish else '是'}")
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("  ⚠️ 未发现 DEEPSEEK_API_KEY；若本次需要调用 LLM，主流水线会显式失败")
    if not os.environ.get("MX_APIKEY") and not args.no_fetch_evidence:
        print("  ⚠️ 未发现 MX_APIKEY；若检索缓存不可用，主流水线会显式失败")

    if args.dry_run:
        if financial_plan["action"] == "fetch":
            for item in financial_plan["queries"]:
                command = [sys.executable, financial_plan["script"], item["query"], str(FINANCIAL_DIR)]
                print("DRY-RUN:", shlex.join(command))
        if stage0_required:
            print("DRY-RUN:", shlex.join(stage0_command))
        elif not resume_run:
            validate_briefing(expected_briefing_path(code), code)
        print("DRY-RUN:", shlex.join(pipeline_command))
        return 0

    summary = {
        "code": code,
        "name": args.name,
        "started_at": started,
        "status": "running",
        "financial_plan": financial_plan,
        "resume_run": str(resume_run) if resume_run else None,
        "recovery": decision.__dict__ | {"card_path": str(decision.card_path) if decision and decision.card_path else None} if decision else None,
        "commands": [],
        "log": str(log_path.relative_to(PROJECT_ROOT)),
    }
    before_runs = {path.resolve() for path in valuation_run_paths(code)}
    run_dir = resume_run
    exit_code = 1
    with StockLock(code):
        try:
            if stage0_required:
                if financial_plan["action"] == "fetch":
                    fetched = fetch_financial_data(financial_plan, code, args.financial_retries, log_path)
                    summary["financial_fetch"] = fetched
                    for item in financial_plan["queries"]:
                        summary["commands"].append(
                            [sys.executable, financial_plan["script"], item["query"], str(FINANCIAL_DIR)]
                        )
                summary["financial_cache"] = check_financial_cache(
                    code, args.financial_max_age_days, args.allow_stale_financial
                )
                summary["commands"].append(stage0_command)
                rc = run_command(stage0_command, "阶段零 · 财务简报", log_path)
                if rc != 0:
                    raise ControllerError(f"阶段零子进程失败，退出码 {rc}")
            else:
                summary["financial_cache"] = {"status": "not_checked", "reason": financial_plan["reason"]}
            briefing = validate_briefing(expected_briefing_path(code), code) if not resume_run else validate_briefing(run_dir / "briefing.json", code)
            summary["briefing"] = briefing
            summary["commands"].append(pipeline_command)
            rc = run_command(pipeline_command, "模型三 · 五阶段研究、计算与发布", log_path)
            if not run_dir:
                new_runs = [path for path in valuation_run_paths(code) if path.resolve() not in before_runs]
                run_dir = new_runs[0] if new_runs else (valuation_run_paths(code)[0] if valuation_run_paths(code) else None)
            if not run_dir:
                raise ControllerError("主流水线未创建运行包")
            if rc != 0:
                manifest = read_json(run_dir / "manifest.json")
                raise ControllerError(f"主流水线失败，退出码 {rc}: {manifest.get('error', '未知错误')}")
            terminal = verify_terminal_run(run_dir, args.no_publish)
            summary.update({"status": "done", "run_dir": str(run_dir.relative_to(PROJECT_ROOT)), "terminal": terminal})
            exit_code = 0
            print(f"\n✅ 模型三总控完成: {run_dir}")
        except KeyboardInterrupt:
            summary.update({"status": "failed", "error": "用户中断，总控已终止子进程"})
            if run_dir:
                summary["run_dir"] = str(run_dir.relative_to(PROJECT_ROOT))
            print("\n❌ 模型三总控已中断，子进程已终止", file=sys.stderr)
        except Exception as exc:
            summary.update({"status": "failed", "error": str(exc)})
            if run_dir:
                summary["run_dir"] = str(run_dir.relative_to(PROJECT_ROOT))
            print(f"\n❌ 模型三总控失败: {exc}", file=sys.stderr)
        finally:
            summary["finished_at"] = datetime.now().isoformat(timespec="seconds")
            summary["exit_code"] = exit_code
            write_json(summary_path, summary)
            if run_dir and run_dir.is_dir():
                write_json(run_dir / "controller_summary.json", summary)
            print(f"总控摘要: {summary_path}")
    return exit_code


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ControllerError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
