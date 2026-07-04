"""
Huaxin Quant 共享工具模块

提供所有脚本共用的基础设施：
  - PROJECT_ROOT: 项目根路径（从本文件位置推导，消除硬编码）
  - RateLimiter: API 请求限流（基础间隔 + 随机抖动 + 指数退避 + 批次歇息）
  - DailyCache: 日线数据本地缓存层（load/save/cleanup）
  - find_key / find_field: 字段名模糊匹配
  - get_latest_annual_period: 推断最新可用年报报告期
"""

import os
import time
import pickle
import random
from datetime import datetime

# ===================== 项目根路径 =====================

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(PROJECT_ROOT, "reports")
VALUATION_REPORTS_DIR = os.path.join(REPORTS_DIR, "valuation")
REPORT_INDEXES_DIR = os.path.join(REPORTS_DIR, "indexes")
DAILY_REPORTS_DIR = os.path.join(REPORTS_DIR, "daily")
VALUATION_INDEX_PATH = os.path.join(REPORT_INDEXES_DIR, "valuation_index.csv")
VALUATION_RANKING_PATH = os.path.join(REPORT_INDEXES_DIR, "valuation_ranking.csv")

# ===================== 年号工具 =====================

def get_latest_annual_period():
    """根据当前日期推断最新可用的年报报告期。

    大部分公司年报在次年 4 月底前披露完毕，因此：
    - 5 月及之后：上年年报已出，如 2026-05 → 2025-12-31
    - 1-4 月：上上年年报，如 2026-03 → 2024-12-31
    """
    now = datetime.now()
    if now.month >= 5:
        year = now.year - 1
    else:
        year = now.year - 2
    return f"{year}-12-31"


# ===================== 限流器 =====================

class RateLimiter:
    """API 请求限流 — 基础间隔 + 随机抖动 + 连续失败指数退避"""

    def __init__(self, base_delay=1.2, jitter=0.6, backoff_base=3.0, max_delay=60.0):
        self.base_delay = base_delay
        self.jitter = jitter
        self.backoff_base = backoff_base
        self.max_delay = max_delay
        self.consecutive_fails = 0
        self.total_calls = 0

    def wait(self, is_fail=False):
        if is_fail:
            self.consecutive_fails += 1
            delay = min(self.backoff_base ** self.consecutive_fails, self.max_delay)
            delay += random.uniform(0, self.jitter)
        else:
            self.consecutive_fails = 0
            delay = self.base_delay + random.uniform(0, self.jitter)
        self.total_calls += 1

        # 每 20 次调用额外歇 5s，降低长跑触发风控的概率
        if self.total_calls > 0 and self.total_calls % 20 == 0:
            delay += 5.0

        time.sleep(delay)

    def reset_fails(self):
        self.consecutive_fails = 0


# ===================== 日线缓存层 =====================

class DailyCache:
    """日线数据本地缓存，模型二和模型四共享。

    缓存目录: cache/daily/
    文件格式: <code>_<YYMMDD>.pkl
    """

    def __init__(self, cache_dir=None):
        if cache_dir is None:
            cache_dir = os.path.join(PROJECT_ROOT, "cache", "daily")
        self.cache_dir = cache_dir

    def _path(self, code, datestr):
        return os.path.join(self.cache_dir, f"{code}_{datestr}.pkl")

    def load(self, code, datestr):
        path = self._path(code, datestr)
        if os.path.exists(path):
            try:
                return pd_read_pickle(path)
            except (pickle.UnpicklingError, EOFError, OSError):
                os.remove(path)
        return None

    def load_latest(self, code):
        """当天缓存不存在时，回退到该股票最近日期的缓存文件。

        返回 (DataFrame, datestr)，无可用缓存时返回 (None, None)。
        文件名格式: <code>_<YYMMDD>.pkl，按 YYMMDD 降序取最新。
        """
        if not os.path.isdir(self.cache_dir):
            return None, None
        prefix = f"{code}_"
        suffix = ".pkl"
        candidates = []
        for f in os.listdir(self.cache_dir):
            if f.startswith(prefix) and f.endswith(suffix):
                datestr = f[len(prefix):-len(suffix)]
                if len(datestr) == 6 and datestr.isdigit():
                    candidates.append((datestr, os.path.join(self.cache_dir, f)))
        if not candidates:
            return None, None
        candidates.sort(key=lambda x: x[0], reverse=True)
        for datestr, path in candidates:
            try:
                return pd_read_pickle(path), datestr
            except (pickle.UnpicklingError, EOFError, OSError):
                os.remove(path)
                continue
        return None, None

    def save(self, code, datestr, df):
        os.makedirs(self.cache_dir, exist_ok=True)
        df.to_pickle(self._path(code, datestr))

    def clear_today(self, datestr):
        """清除指定日期的全部缓存（--refresh 用）"""
        count = 0
        if os.path.isdir(self.cache_dir):
            for f in os.listdir(self.cache_dir):
                if f.endswith(f"_{datestr}.pkl"):
                    os.remove(os.path.join(self.cache_dir, f))
                    count += 1
        return count

    def cleanup_old(self, keep_days=5):
        """清理超过 keep_days 天的旧缓存文件"""
        cutoff = time.time() - keep_days * 86400
        cleaned = 0
        if os.path.isdir(self.cache_dir):
            for f in os.listdir(self.cache_dir):
                fpath = os.path.join(self.cache_dir, f)
                if f.endswith(".pkl") and os.path.getmtime(fpath) < cutoff:
                    os.remove(fpath)
                    cleaned += 1
        return cleaned


def pd_read_pickle(path):
    """pandas read_pickle 的延迟导入封装，避免 pandas 未安装时 import 失败"""
    import pandas as pd
    return pd.read_pickle(path)


# ===================== 字段模糊匹配 =====================

def find_key(row, *patterns, require_all=True):
    """在 dict 的 key 中做模糊匹配。

    Args:
        row: 待搜索的 dict
        patterns: 一个或多个子串
        require_all: True=所有 pattern 均命中才返回, False=任一命中即返回

    Returns:
        首个匹配的 key 名，未找到返回 None
    """
    for k in row:
        if require_all:
            if all(p in k for p in patterns):
                return k
        else:
            if any(p in k for p in patterns):
                return k
    return None


def find_field_by_alias(field_map, aliases):
    """通过别名列表查找字段。

    Args:
        field_map: {field_name: value} 的 dict
        aliases: 别名列表，按优先级排列

    Returns:
        首个匹配的 field_name，未找到返回 None
    """
    for alias in aliases:
        for field_name in field_map:
            if alias in field_name:
                return field_name
    return None
