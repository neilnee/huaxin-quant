"""Small shared utilities that are independent of the canonical market data store."""

import os
import random
import sys
import time
from datetime import datetime, timedelta


MIN_PYTHON = (3, 11)
if sys.version_info < MIN_PYTHON:
    raise RuntimeError(
        "Huaxin Quant requires Python 3.11 or newer. "
        "Create and activate .venv before running project scripts."
    )


PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
VALUATION_REPORTS_DIR = os.path.join(REPORTS_DIR, "valuation")
REPORT_INDEXES_DIR = os.path.join(REPORTS_DIR, "indexes")
DAILY_REPORTS_DIR = os.path.join(REPORTS_DIR, "daily")
VALUATION_INDEX_PATH = os.path.join(REPORT_INDEXES_DIR, "valuation_index.csv")
VALUATION_RANKING_PATH = os.path.join(REPORT_INDEXES_DIR, "valuation_ranking.csv")


def get_latest_annual_period():
    now = datetime.now()
    year = now.year - 1 if now.month >= 5 else now.year - 2
    return f"{year}-12-31"


def expected_trade_date(run_date=None):
    if run_date is None:
        current = datetime.now()
        date = current.date() - timedelta(days=1 if current.hour < 15 else 0)
    elif isinstance(run_date, str):
        date = datetime.strptime(run_date, "%Y-%m-%d").date()
    else:
        date = run_date
    while date.weekday() >= 5:
        date -= timedelta(days=1)
    return date.strftime("%Y-%m-%d")


def default_pipeline_date():
    return expected_trade_date().replace("-", "")[2:]


def normalize_date_arg(value):
    raw = str(value).strip()
    if len(raw) == 6 and raw.isdigit():
        return datetime.strptime(raw, "%y%m%d").strftime("%Y-%m-%d")
    if len(raw) == 8 and raw.isdigit():
        return datetime.strptime(raw, "%Y%m%d").strftime("%Y-%m-%d")
    if len(raw) >= 10:
        return datetime.strptime(raw[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    raise ValueError(f"无法解析日期: {value}")


class RateLimiter:
    def __init__(self, base_delay=1.2, jitter=0.6, backoff_base=3.0, max_delay=60.0):
        self.base_delay, self.jitter = base_delay, jitter
        self.backoff_base, self.max_delay = backoff_base, max_delay
        self.consecutive_fails = self.total_calls = 0

    def wait(self, is_fail=False):
        if is_fail:
            self.consecutive_fails += 1
            delay = min(self.backoff_base ** self.consecutive_fails, self.max_delay)
        else:
            self.consecutive_fails = 0
            delay = self.base_delay
        self.total_calls += 1
        time.sleep(delay + random.uniform(0, self.jitter) + (5 if self.total_calls % 20 == 0 else 0))

    def reset_fails(self):
        self.consecutive_fails = 0


def find_key(row, *patterns, require_all=True):
    for key in row:
        if all(pattern in key for pattern in patterns) if require_all else any(pattern in key for pattern in patterns):
            return key
    return None


def find_field_by_alias(field_map, aliases):
    for alias in aliases:
        for field_name in field_map:
            if alias in field_name:
                return field_name
    return None
