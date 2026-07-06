"""
数据源抽象层 — 日线数据获取的统一入口

当前实现:
  - MiaoxiangSource: 东方财富妙想 API（主数据源）
  - TDXSource: 通达信 mootdx（备用数据源）

设计原则:
  - DataSource 抽象基类定义 fetch_bars(code, name) → DataFrame | None 接口
  - 各实现封装自己的 API 调用、字段解析、重试逻辑
  - 返回统一的 OHLCV DataFrame (date, open, high, low, close, volume, turnover)
"""

import abc
import os
import time
from typing import Optional, Tuple

import pandas as pd
import requests

from scripts.shared import RateLimiter

# ===================== 常量 =====================

MX_BASE_URL = "https://mkapi2.dfcfs.com/finskillshub/api/claw/query"
MAX_RETRIES = 3

# 字段别名：整合 quant_filter 的精确匹配 + tracker 的模糊匹配
# 按优先级排列，先精确后模糊
FIELD_GROUPS = [
    ("收盘价", ["收盘价", "收盘", "当日收盘价", "最新价"]),
    ("开盘价", ["开盘价", "开盘", "当日开盘价"]),
    ("最高价", ["最高价", "最高", "当日最高价"]),
    ("最低价", ["最低价", "最低", "当日最低价"]),
    ("成交量", ["成交量", "成交数量", "成交股数"]),
    ("换手率", ["换手率", "换手"]),
]


# ===================== 抽象基类 =====================

class DataSource(abc.ABC):
    """日线数据源抽象基类。

    子类只需实现 fetch_bars() 和 display_name。
    """

    @abc.abstractmethod
    def fetch_bars(self, code: str, name: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """拉取近约 200 个交易日日线数据。

        Args:
            code: 6 位股票代码，如 '300604'
            name: 股票名称，如 '长川科技'

        Returns:
            (DataFrame, None) — 成功，DataFrame 列: date, open, high, low, close, volume, turnover
            (None, error_message) — 失败，error_message 为人类可读的错误原因
        """
        ...

    @property
    @abc.abstractmethod
    def display_name(self) -> str:
        """人类可读的数据源名称，如 'mx-api'、'tdx'"""
        ...


# ===================== 妙想 API 数据源 =====================

class MiaoxiangSource(DataSource):
    """东方财富妙想 API 数据源。

    整合了 quant_filter.py 和 tracker.py 两套 fetch_daily() 的优点:
      - 表选择: 遍历所有表索引查找完整 OHLCV 表（quant_filter 方式，更稳健）
      - 字段解析: 别名列表 + 模糊子串匹配（tracker 方式，支持 ETF 字段变体）
      - 错误消息: quant_filter 的详细版本
    """

    display_name = "mx-api"

    def __init__(self, rate_limiter: Optional[RateLimiter] = None):
        self._rl = rate_limiter or RateLimiter()

    # ── 公共接口 ──

    def fetch_bars(self, code: str, name: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """通过妙想 API 获取日线数据。返回 (DataFrame, None) 或 (None, error_msg)。

        查询策略：以代码为主锚点，避免 NLP 对简称（尤其是 ETF）匹配到残缺数据表。
        1. 主查询："{code} {name}近200个交易日..."
        2. 兜底：  "{code}近200个交易日..."  （纯代码，消除歧义）
        """
        api_key = os.environ.get("MX_APIKEY")
        if not api_key:
            return None, "MX_APIKEY 未设置"

        queries = [
            f"{code} {name}近200个交易日每日开盘价、最高价、最低价、收盘价、成交量、换手率",
            f"{code}近200个交易日每日开盘价、最高价、最低价、收盘价、成交量、换手率",
        ]

        headers = {"Content-Type": "application/json", "apikey": api_key}
        last_error = None

        for qi, query in enumerate(queries):
            payload = {"toolQuery": query, "toolType": "query_tool"}
            result = self._do_request(headers, payload)
            if result is None:
                last_error = "网络请求失败(重试3次仍失败)"
                continue

            raw, resolved, err = self._extract_table(result)
            if raw is not None:
                df = self._parse_rows(raw, resolved)
                if df is not None:
                    return df, None
                last_error = "无有效交易日数据(可能长期停牌)"
                continue

            last_error = err or "未找到完整历史行情表(字段缺失或结构异常)"

        return None, last_error

    def _extract_table(self, result: dict) -> Tuple[Optional[dict], Optional[dict], Optional[str]]:
        """从 API 响应中提取完整 OHLCV 表。

        返回 (raw_table, resolved_fields, error_msg)。
        表不完整或结构异常时 raw_table 为 None。
        """
        try:
            inner_msg = result.get("data", {}).get("data", {}).get("message", "")
        except (KeyError, TypeError, AttributeError):
            inner_msg = ""

        if inner_msg and ("上限" in str(inner_msg) or "额度" in str(inner_msg)):
            return None, None, str(inner_msg)

        try:
            tables = result["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
        except (KeyError, TypeError):
            return None, None, "数据结构异常(缺 dataTableDTOList)"

        raw, resolved = self._select_table(tables)
        if raw is None:
            return None, None, str(inner_msg) if inner_msg else None

        return raw, resolved, None

    # ── HTTP 请求 + 重试 ──

    def _do_request(self, headers, payload):
        """执行 HTTP 请求，含重试和错误码处理。"""
        for attempt in range(MAX_RETRIES):
            try:
                resp = requests.post(MX_BASE_URL, headers=headers, json=payload, timeout=30)
                resp.raise_for_status()
                data = resp.json()
            except requests.exceptions.Timeout:
                if attempt < 1:
                    self._rl.wait(is_fail=True)
                    continue
                return None
            except requests.exceptions.ConnectionError:
                if attempt < 1:
                    self._rl.wait(is_fail=True)
                    continue
                return None
            except Exception:
                return None

            code = data.get("code", -1)
            if code == 0:
                self._rl.reset_fails()
                return data
            if code == 112:  # 频率限制
                if attempt < 2:
                    self._rl.wait(is_fail=True)
                    time.sleep(5)
                    continue
                return None
            if code in (113, 114):  # 调用上限 / Key 失效 — 致命
                return None
            if code == 115:  # 无数据
                return None
            if attempt == 0:
                self._rl.wait(is_fail=True)
                continue
            return None

        return None

    # ── 表选择（quant_filter 的遍历方式）──

    def _select_table(self, tables):
        """遍历所有返回的表，选择第一个包含全部 OHLCV 字段的历史行情表。"""
        for idx, table in enumerate(tables or []):
            try:
                raw = table["rawTable"]
                name_map = table["nameMap"]
            except (KeyError, TypeError):
                continue

            ind_map = {v: k for k, v in name_map.items() if k != "headNameSub"}
            resolved = self._resolve_fields(ind_map)
            if resolved is None:
                continue
            if not raw.get("headName"):
                continue
            return raw, resolved

        return None, None

    # ── 字段解析（tracker 的模糊匹配方式）──

    def _resolve_fields(self, ind_map):
        """精确匹配 → 模糊子串匹配，返回 {canonical_name: raw_key}。"""
        resolved = {}
        for canonical, aliases in FIELD_GROUPS:
            # 第一轮：精确匹配
            for alias in aliases:
                if alias in ind_map:
                    resolved[canonical] = ind_map[alias]
                    break
            if canonical in resolved:
                continue
            # 第二轮：模糊子串匹配
            for alias in aliases:
                for field_name in ind_map:
                    if alias in field_name:
                        resolved[canonical] = ind_map[field_name]
                        break
                if canonical in resolved:
                    break
            if canonical not in resolved:
                return None
        return resolved

    # ── 行解析 ──

    def _parse_rows(self, raw, resolved):
        """将 rawTable 的行转换为标准化 DataFrame。"""
        rows = []
        for i, d in enumerate(raw["headName"]):
            vol = raw[resolved["成交量"]][i]
            turn = raw[resolved["换手率"]][i]
            opn = raw[resolved["开盘价"]][i]
            high = raw[resolved["最高价"]][i]
            low = raw[resolved["最低价"]][i]
            close = raw[resolved["收盘价"]][i]

            # 停牌日任一字段为 '-' 则跳过
            if "-" in (vol, turn, opn, high, low, close):
                continue

            rows.append({
                "date": d,
                "open": float(opn),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": float(vol),
                "turnover": float(turn),
            })

        if not rows:
            return None

        return pd.DataFrame(rows)


# ===================== 通达信数据源（mootdx） =====================

class TDXSource(DataSource):
    """通达信数据源，基于 mootdx 库连接公共行情服务器。

    作为妙想 API 限流时的备用数据源，免费、无额度限制。
    优先尝试已验证可用的快速服务器，失败后回退到 bestip 自动探测。
    依赖: pip install 'mootdx[all]'
    """

    display_name = "tdx"

    # 已验证的低延迟服务器（定期通过 `python -m mootdx bestip -vv` 更新）
    _KNOWN_SERVERS = [
        ("180.153.18.170", 7709),
        ("60.191.117.167", 7709),
        ("115.238.56.198", 7709),
        ("115.238.90.165", 7709),
        ("123.125.108.14", 7709),
    ]

    _FALLBACK_SERVERS = [
        ("218.75.126.9", 7709),
        ("60.12.136.250", 7709),
        ("218.6.170.47", 7709),
    ]

    def __init__(self):
        self._client = None
        self._server = None

    def _try_connect(self, server):
        """尝试连接指定服务器，成功返回 client，失败返回 None。"""
        from mootdx.quotes import Quotes
        try:
            client = Quotes.factory(market='std', server=server, timeout=8)
            # 快速验证连接
            raw = client.bars(symbol='000001', frequency=9, offset=1)
            if raw is not None and not raw.empty:
                return client
        except Exception:
            pass
        return None

    def _get_client(self):
        """获取或创建 mootdx 客户端。

        连接策略：已知快速服务器 → 已知备用服务器 → bestip 自动探测。
        """
        if self._client is not None:
            return self._client

        from mootdx.quotes import Quotes

        # 第一轮：已验证的快速服务器
        for server in self._KNOWN_SERVERS:
            client = self._try_connect(server)
            if client is not None:
                self._client = client
                self._server = server
                return client

        # 第二轮：备用服务器
        for server in self._FALLBACK_SERVERS:
            client = self._try_connect(server)
            if client is not None:
                self._client = client
                self._server = server
                return client

        # 第三轮：bestip 自动探测
        try:
            self._client = Quotes.factory(market='std', bestip=True, timeout=15)
            self._server = ("auto", 0)
            return self._client
        except Exception:
            raise RuntimeError("通达信所有服务器不可达")

    def fetch_bars(self, code: str, name: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """通过 mootdx 获取日线数据。

        mootdx 自动根据 code 前缀识别沪深市场（6→SH, 0/2/3→SZ）。
        """
        try:
            client = self._get_client()
        except ImportError:
            return None, "mootdx 未安装，运行: pip install 'mootdx[all]'"
        except Exception as exc:
            return None, f"TDX 服务器探测失败: {exc}"

        try:
            raw = client.bars(symbol=code, frequency=9, offset=200)
        except Exception as exc:
            self._client = None
            self._server = None
            return None, f"TDX 请求失败: {exc}"

        if raw is None or raw.empty:
            return None, "TDX 返回空数据"

        # 过滤停牌日（volume=0，量价均为 0 的无交易行）
        normal = raw[raw["volume"] > 0].copy()
        if normal.empty:
            return None, "TDX 无有效交易日（可能长期停牌）"

        # 标准化为统一的 OHLCV DataFrame
        result = pd.DataFrame({
            "date":     normal["datetime"].astype(str).str[:10],
            "open":     normal["open"].astype(float),
            "high":     normal["high"].astype(float),
            "low":      normal["low"].astype(float),
            "close":    normal["close"].astype(float),
            "volume":   normal["volume"].astype(float),
            "turnover": 0.0,
        })

        # mootdx 返回最新在前，转为升序
        result = result.sort_values("date").reset_index(drop=True)
        return result, None
