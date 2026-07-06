#!/usr/bin/env python3
"""Run model 1 end-to-end: fetch xuangu segments, then process the pool."""
import argparse
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from scripts.data.pool_data import PoolSegmentCache, XuanguSource, result_stats
from scripts.shared import get_latest_annual_period
from scripts.strategy_config import load_strategy_config


PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SEGMENT_DIR = PROJECT_ROOT / "cache" / "xuangu"
PROCESS_POOL_SCRIPT = PROJECT_ROOT / "scripts" / "process_pool.py"
DEFAULT_XUANGU_SCRIPT = os.environ.get("HUAXIN_XUANGU_SCRIPT", "mx_xuangu.py")
POOL_STRATEGY_FILE = "01-pool.json"
POOL_STRATEGY, POOL_STRATEGY_PATH = load_strategy_config(POOL_STRATEGY_FILE)
STRATEGY_VERSION = POOL_STRATEGY["strategy_version"]
RUNTIME_CFG = POOL_STRATEGY["runtime"]
API_QUERY_CFG = POOL_STRATEGY["api_query"]
SEGMENT_CACHE = PoolSegmentCache(SEGMENT_DIR)


@dataclass(frozen=True)
class Segment:
    name: str
    condition: str


SEGMENTS = [
    Segment(item["name"], item["condition"])
    for item in POOL_STRATEGY["market_cap_segments"]
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
    parts = list(API_QUERY_CFG["filters"])
    parts.append(segment_condition)
    parts.extend(field.format(annual_year=annual_year) for field in API_QUERY_CFG["output_fields"])
    if sort_clause:
        parts.append(sort_clause)
    return " ".join(parts)


def find_cache_for_query(query: str, cache_days: int) -> Optional[Path]:
    return SEGMENT_CACHE.find_for_query(query, cache_days)


def run_xuangu(query: str, xuangu_script: Path, dry_run: bool) -> Optional[Path]:
    return XuanguSource(SEGMENT_CACHE, xuangu_script).fetch(query, dry_run)


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
    if not args.dry_run and not xuangu_script.exists():
        raise RuntimeError(f"mx-xuangu 脚本不存在: {xuangu_script}")

    SEGMENT_DIR.mkdir(parents=True, exist_ok=True)
    fetched_or_cached: list[Path] = []

    print("=" * 60)
    print("Phase 1: Fetch xuangu segments")
    print("=" * 60)
    print(f"  strategy: {STRATEGY_VERSION} ({POOL_STRATEGY_FILE})")

    for segment in SEGMENTS:
        query = build_query(segment.condition)
        path = fetch_query(query, xuangu_script, args.cache_days, args.force_refresh, args.dry_run)
        if path:
            fetched_or_cached.append(path)

        if args.dry_run:
            continue

        rows, total = result_stats(path) if path else (0, 0)
        print(f"  分段 {segment.name}: rows={rows} total={total}")
        threshold = RUNTIME_CFG["truncation_threshold"]
        if total >= threshold or rows >= threshold:
            print(f"  ⚠️ {segment.name} 可能触发 {threshold} 条截断，追加 PE 正序/倒序拆分")
            for sort_clause in RUNTIME_CFG["split_sort_clauses"]:
                split_query = build_query(segment.condition, sort_clause)
                split_path = fetch_query(split_query, xuangu_script, args.cache_days, args.force_refresh, args.dry_run)
                if split_path:
                    fetched_or_cached.append(split_path)
                split_rows, split_total = result_stats(split_path) if split_path else (0, 0)
                print(f"    {sort_clause}: rows={split_rows} total={split_total}")
                if split_total >= threshold and split_rows >= threshold:
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
    parser.add_argument("--cache-days", type=int, default=RUNTIME_CFG["cache_max_age_days"], help=f"阶段一缓存有效天数，默认 {RUNTIME_CFG['cache_max_age_days']}")
    parser.add_argument("--delay", type=float, default=RUNTIME_CFG["fetch_delay_seconds"], help=f"分段调用间隔秒数，默认 {RUNTIME_CFG['fetch_delay_seconds']}")
    parser.add_argument(
        "--xuangu-script",
        default=DEFAULT_XUANGU_SCRIPT,
        help="mx_xuangu.py 路径，也可通过 HUAXIN_XUANGU_SCRIPT 设置",
    )
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
