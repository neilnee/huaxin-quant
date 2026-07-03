#!/usr/bin/env python3
"""Run model 1 end-to-end: fetch xuangu segments, then process the pool."""
import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.shared import get_latest_annual_period


PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEGMENT_DIR = PROJECT_ROOT / "cache" / "xuangu"
PROCESS_POOL_SCRIPT = PROJECT_ROOT / "scripts" / "process_pool.py"
DEFAULT_XUANGU_SCRIPT = Path("/Users/neil/.codex/skills/mx-xuangu/mx_xuangu.py")


@dataclass(frozen=True)
class Segment:
    name: str
    condition: str


SEGMENTS = [
    Segment("mcap_50_100", "总市值大于50亿 总市值小于100亿"),
    Segment("mcap_100_200", "总市值大于100亿 总市值小于200亿"),
    Segment("mcap_200_500", "总市值大于200亿 总市值小于500亿"),
    Segment("mcap_500_plus", "总市值大于500亿"),
]


def load_local_env() -> None:
    """Load simple KEY=VALUE entries from local .env without overriding env vars."""
    env_path = PROJECT_ROOT / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def build_query(segment_condition: str, sort_clause: str = "") -> str:
    annual_year = get_latest_annual_period().split("-", 1)[0]
    parts = [
        "营收同比增长率大于15%",
        "净利润同比增长率大于20%",
        "毛利率大于15%",
        "研发费用占营收比例大于2.5%",
        "排除ST股",
        segment_condition,
        "查询所属行业",
        "上市日期",
        "总市值",
        "市盈率",
        "市净率",
        "ROE",
        "营业收入",
        f"每股收益({annual_year}年年报)",
        f"经营活动产生的现金流量净额({annual_year}年年报)",
        "归属母公司股东的净利润",
        "资产负债率",
        "A股",
    ]
    if sort_clause:
        parts.append(sort_clause)
    return " ".join(parts)


def nested_payload(data: dict) -> dict:
    cur = data
    for key in ("data", "data"):
        if not isinstance(cur, dict):
            return {}
        cur = cur.get(key, {})
    return cur if isinstance(cur, dict) else {}


def read_raw_json(path: Path) -> Optional[dict]:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def raw_query(data: dict) -> str:
    payload = nested_payload(data)
    return str(payload.get("query") or payload.get("title") or "")


def raw_result(data: dict) -> dict:
    payload = nested_payload(data)
    all_results = payload.get("allResults")
    if isinstance(all_results, dict):
        result = all_results.get("result")
        if isinstance(result, dict):
            return result
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def result_stats(path: Path) -> tuple[int, int]:
    data = read_raw_json(path)
    if not data:
        return 0, 0
    result = raw_result(data)
    rows = result.get("dataList") or []
    total = result.get("total") or result.get("totalRecordCount") or 0
    try:
        total = int(total)
    except (TypeError, ValueError):
        total = 0
    return len(rows), total


def find_cache_for_query(query: str, cache_days: int) -> Optional[Path]:
    if not SEGMENT_DIR.exists():
        return None
    cutoff = time.time() - cache_days * 86400
    candidates = sorted(
        SEGMENT_DIR.glob("*_raw.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for path in candidates:
        if path.stat().st_mtime < cutoff:
            continue
        data = read_raw_json(path)
        if data and raw_query(data) == query:
            return path
    return None


def run_xuangu(query: str, xuangu_script: Path, dry_run: bool) -> Optional[Path]:
    cmd = [
        sys.executable,
        str(xuangu_script),
        "--output-dir",
        str(SEGMENT_DIR),
        "--query",
        query,
    ]
    print(f"\n  拉取: {query}")
    if dry_run:
        print("  DRY-RUN:", " ".join(cmd))
        return None

    sys.stdout.flush()
    before = {p.resolve() for p in SEGMENT_DIR.glob("*_raw.json")}
    completed = subprocess.run(cmd, cwd=str(xuangu_script.parent), text=True)
    if completed.returncode != 0:
        raise RuntimeError(f"mx-xuangu 调用失败: returncode={completed.returncode}")

    after = {p.resolve() for p in SEGMENT_DIR.glob("*_raw.json")}
    new_files = [Path(p) for p in after - before]
    if new_files:
        return max(new_files, key=lambda p: p.stat().st_mtime)

    # Some skill versions overwrite an existing filename. Fall back to exact query match.
    return find_cache_for_query(query, cache_days=1)


def fetch_query(query: str, xuangu_script: Path, cache_days: int, force_refresh: bool, dry_run: bool) -> Optional[Path]:
    if not force_refresh:
        cached = find_cache_for_query(query, cache_days)
        if cached:
            rows, total = result_stats(cached)
            age = (date.today() - datetime.fromtimestamp(cached.stat().st_mtime).date()).days
            print(f"  命中缓存: {cached.name[-55:]} | rows={rows} total={total} age={age}天")
            return cached
    return run_xuangu(query, xuangu_script, dry_run)


def fetch_segments(args: argparse.Namespace) -> list[Path]:
    load_local_env()
    if not os.environ.get("MX_APIKEY") and not args.dry_run:
        raise RuntimeError("缺少 MX_APIKEY 环境变量，无法调用 mx-xuangu")

    xuangu_script = Path(args.xuangu_script).expanduser()
    if not xuangu_script.exists():
        raise RuntimeError(f"mx-xuangu 脚本不存在: {xuangu_script}")

    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    fetched_or_cached: list[Path] = []

    print("=" * 60)
    print("Phase 1: Fetch xuangu segments")
    print("=" * 60)

    for segment in SEGMENTS:
        query = build_query(segment.condition)
        path = fetch_query(query, xuangu_script, args.cache_days, args.force_refresh, args.dry_run)
        if path:
            fetched_or_cached.append(path)

        if args.dry_run:
            continue

        rows, total = result_stats(path) if path else (0, 0)
        print(f"  分段 {segment.name}: rows={rows} total={total}")
        if total >= 200 or rows >= 200:
            print(f"  ⚠️ {segment.name} 可能触发 200 条截断，追加 PE 正序/倒序拆分")
            for sort_clause in ("按市盈率从小到大", "按市盈率从大到小"):
                split_query = build_query(segment.condition, sort_clause)
                split_path = fetch_query(split_query, xuangu_script, args.cache_days, args.force_refresh, args.dry_run)
                if split_path:
                    fetched_or_cached.append(split_path)
                split_rows, split_total = result_stats(split_path) if split_path else (0, 0)
                print(f"    {sort_clause}: rows={split_rows} total={split_total}")
                if split_total >= 200 and split_rows >= 200:
                    print(f"    ⚠️ {sort_clause} 仍可能截断，建议后续再加市值/估值子分段")

        time.sleep(args.delay)

    return fetched_or_cached


def run_process_pool(dry_run: bool) -> int:
    cmd = [sys.executable, str(PROCESS_POOL_SCRIPT)]
    print("\n" + "=" * 60)
    print("Phase 2-5: Process pool")
    print("=" * 60)
    if dry_run:
        print("DRY-RUN:", " ".join(cmd))
        return 0
    sys.stdout.flush()
    completed = subprocess.run(cmd, cwd=str(PROJECT_ROOT), text=True)
    return completed.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run model 1 pool screening end-to-end.")
    parser.add_argument("--skip-fetch", action="store_true", help="只执行阶段二到五，复用现有 cache/xuangu 数据")
    parser.add_argument("--force-refresh", action="store_true", help="忽略缓存，强制重新拉取阶段一分段数据")
    parser.add_argument("--no-process", action="store_true", help="只执行阶段一拉取，不运行 process_pool.py")
    parser.add_argument("--dry-run", action="store_true", help="只打印将执行的查询和命令，不发起网络请求")
    parser.add_argument("--cache-days", type=int, default=5, help="阶段一缓存有效天数，默认 5")
    parser.add_argument("--delay", type=float, default=1.5, help="分段调用间隔秒数，默认 1.5")
    parser.add_argument("--xuangu-script", default=str(DEFAULT_XUANGU_SCRIPT), help="mx_xuangu.py 路径")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.skip_fetch and args.force_refresh:
        print("ERROR: --skip-fetch 与 --force-refresh 不能同时使用")
        return 2

    try:
        if not args.skip_fetch:
            paths = fetch_segments(args)
            if not args.dry_run:
                print(f"\n  阶段一完成: {len(paths)} 个 raw.json 文件可用于后续处理")

        if args.no_process:
            return 0

        return run_process_pool(args.dry_run)
    except RuntimeError as exc:
        print(f"\nERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
