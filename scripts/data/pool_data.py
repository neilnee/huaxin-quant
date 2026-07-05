"""Model 1 pool data layer.

This module owns xuangu raw JSON cache access, raw payload normalization,
field parsing helpers, and the external xuangu source adapter. Model 1
strategy rules remain in process_pool.py and strategies/01-pool.json.
"""

import json
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional


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


@dataclass
class SegmentLoadResult:
    stocks: dict[str, dict]
    segment_counts: list[tuple[str, int, int]]
    stale_files: list[tuple[str, int]]


class PoolSegmentCache:
    """cache/xuangu raw JSON cache helper."""

    def __init__(self, cache_dir: Path):
        self.cache_dir = Path(cache_dir)

    def raw_files(self):
        if not self.cache_dir.exists():
            return []
        return sorted(
            self.cache_dir.glob("*_raw.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )

    def find_for_query(self, query: str, cache_days: int) -> Optional[Path]:
        cutoff = time.time() - cache_days * 86400
        for path in self.raw_files():
            if path.stat().st_mtime < cutoff:
                continue
            data = read_raw_json(path)
            if data and raw_query(data) == query:
                return path
        return None

    def load_recent_segments(self, today: date, cache_days: int) -> SegmentLoadResult:
        stocks: dict[str, dict] = {}
        segment_counts = []
        stale_files = []

        for json_file in self.raw_files():
            mtime = datetime.fromtimestamp(json_file.stat().st_mtime).date()
            age = (today - mtime).days
            if age > cache_days:
                stale_files.append((json_file.name, age))
                continue

            data = read_raw_json(json_file)
            result = raw_result(data or {})
            rows = result.get("dataList", [])
            total = result.get("total", 0)
            segment_counts.append((json_file.name, len(rows), total))

            for row in rows:
                code = str(row.get("SECURITY_CODE", "")).strip().zfill(6)
                if code and len(code) == 6:
                    stocks[code] = row

        return SegmentLoadResult(stocks, segment_counts, stale_files)


class XuanguSource:
    """Adapter for the local mx_xuangu.py script."""

    def __init__(self, cache: PoolSegmentCache, xuangu_script: Path):
        self.cache = cache
        self.xuangu_script = Path(xuangu_script).expanduser()

    def fetch(self, query: str, dry_run: bool) -> Optional[Path]:
        cmd = [
            sys.executable,
            str(self.xuangu_script),
            "--output-dir",
            str(self.cache.cache_dir),
            "--query",
            query,
        ]
        print(f"\n  拉取: {query}")
        if dry_run:
            print("  DRY-RUN:", " ".join(cmd))
            return None

        self.cache.cache_dir.mkdir(parents=True, exist_ok=True)
        sys.stdout.flush()
        before = {p.resolve() for p in self.cache.cache_dir.glob("*_raw.json")}
        completed = subprocess.run(cmd, cwd=str(self.xuangu_script.parent), text=True)
        if completed.returncode != 0:
            raise RuntimeError(f"mx-xuangu 调用失败: returncode={completed.returncode}")

        after = {p.resolve() for p in self.cache.cache_dir.glob("*_raw.json")}
        new_files = [Path(p) for p in after - before]
        if new_files:
            return max(new_files, key=lambda p: p.stat().st_mtime)

        # Some skill versions overwrite an existing filename. Fall back to exact query match.
        return self.cache.find_for_query(query, cache_days=1)


def pick_annual(v: str) -> str:
    """从 '值1|周期1, 值2|周期2' 中取年报周期的值，无年报则取第一个。"""
    if v is None or v == "" or v == "-":
        return ""
    s = str(v)
    if "|" not in s:
        return s
    parts = [p.strip() for p in s.split(",")]
    for p in parts:
        if "年报" in p and "一季报" not in p and "三季报" not in p and "半年报" not in p:
            return p.split("|")[0].strip()
    for p in parts:
        if "半年报" in p or "中报" in p:
            return p.split("|")[0].strip()
    return parts[0].split("|")[0].strip()


def parse_num(v) -> Optional[float]:
    """'17.04亿' -> 1704000000.0, '5000万' -> 50000000.0."""
    if v is None or v == "" or v == "-":
        return None
    s = pick_annual(v).strip().replace(",", "").replace("%", "")
    try:
        if "亿" in s:
            return float(s.replace("亿", "")) * 1e8
        if "万" in s:
            return float(s.replace("万", "")) * 1e4
        if "元" in s:
            return float(s.replace("元", ""))
        return float(s)
    except ValueError:
        return None


def parse_pct(v) -> Optional[float]:
    if v is None or v == "" or v == "-":
        return None
    s = pick_annual(v).strip().replace(",", "").replace("%", "")
    try:
        return float(s)
    except ValueError:
        return None


def parse_date(v) -> Optional[datetime]:
    if not v:
        return None
    s = str(v).split("|")[0].strip()
    try:
        return datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        return None


def find_key(row: dict, *patterns, require_all=True) -> Optional[str]:
    """Fuzzy match field key. If require_all, ALL patterns must match."""
    for k in row:
        if require_all:
            if all(p in k for p in patterns):
                return k
        else:
            if any(p in k for p in patterns):
                return k
    return None
