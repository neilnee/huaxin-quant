"""External source adapters for industry membership and capital-flow data."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from datetime import datetime
from typing import Iterable

import requests

from scripts.data.market_data import MiaoxiangSource


EASTMONEY_TOKEN = "bd1d9ddb04089700cf9c27f6f7426281"
EASTMONEY_HOSTS = (
    "https://push2.eastmoney.com",
    "https://17.push2.eastmoney.com",
    "https://79.push2.eastmoney.com",
)


class RequestBudgetExceeded(RuntimeError):
    pass


class RequestBudget:
    def __init__(self, maximum_requests: int):
        self.maximum_requests = max(int(maximum_requests), 0)
        self.used = 0

    def consume(self) -> None:
        if self.used >= self.maximum_requests:
            raise RequestBudgetExceeded(
                f"妙想请求预算已用完: {self.used}/{self.maximum_requests}"
            )
        self.used += 1


def normalize_stock_code(value: object) -> str:
    raw = str(value or "").strip().split(".", 1)[0]
    return raw.zfill(6) if raw.isdigit() and len(raw) <= 6 else raw


def is_a_share_code(code: str) -> bool:
    code = normalize_stock_code(code)
    return len(code) == 6 and code.isdigit() and code.startswith(
        ("000", "001", "002", "003", "300", "301", "430", "600", "601", "603", "605", "688", "689", "8", "9")
    )


def parse_cn_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if math.isfinite(number) else None
    text = str(value).strip().replace(",", "")
    if not text or text in {"-", "--", "None", "null", "nan"}:
        return None
    multiplier = 1.0
    if text.endswith("万亿"):
        multiplier, text = 1e12, text[:-2]
    elif text.endswith("亿") or text.endswith("亿元"):
        multiplier = 1e8
        text = text[:-2] if text.endswith("亿元") else text[:-1]
    elif text.endswith("万") or text.endswith("万元"):
        multiplier = 1e4
        text = text[:-2] if text.endswith("万元") else text[:-1]
    elif text.endswith("%"):
        text = text[:-1]
    try:
        number = float(text) * multiplier
    except ValueError:
        return None
    return number if math.isfinite(number) else None


def normalize_trade_date(value: object) -> str | None:
    match = re.search(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return match.group(0) if match else None


def source_hash(rows: Iterable[dict]) -> str:
    payload = json.dumps(list(rows), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EastmoneyIndustrySource:
    """Fetch the Eastmoney industry-board catalog and constituents."""

    def __init__(self, page_size: int = 100, timeout: int = 20, maximum_retries: int = 3, session=None):
        self.page_size = int(page_size)
        self.timeout = int(timeout)
        self.maximum_retries = int(maximum_retries)
        self.session = session or requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://quote.eastmoney.com/center/boardlist.html#industry_board",
        })

    def fetch_catalog(self, level: int = 1) -> list[dict]:
        section = {1: 2, 2: 4}.get(int(level))
        if section is None:
            raise ValueError(f"不支持的财富通行业层级: {level}")
        rows = self._fetch_all(f"m:90+s:{section}+f:!50")
        catalog = []
        for row in rows:
            code, name = str(row.get("f12") or "").strip(), str(row.get("f14") or "").strip()
            if re.fullmatch(r"BK\d+", code) and name:
                catalog.append({"bk_code": code, "bk_name": name})
        if not catalog:
            raise RuntimeError("东方财富行业板块目录为空")
        return sorted(catalog, key=lambda item: item["bk_code"])

    def fetch_members(self, bk_code: str) -> list[dict]:
        if not re.fullmatch(r"BK\d+", str(bk_code)):
            raise ValueError(f"无效财富通板块代码: {bk_code}")
        rows = self._fetch_all(f"b:{bk_code}+f:!50")
        members = []
        for row in rows:
            code = normalize_stock_code(row.get("f12"))
            if is_a_share_code(code):
                members.append({"stock_code": code, "stock_name": str(row.get("f14") or "").strip()})
        if not members:
            raise RuntimeError(f"财富通板块 {bk_code} 成分为空")
        return sorted(members, key=lambda item: item["stock_code"])

    def _fetch_all(self, filter_expression: str) -> list[dict]:
        first = self._fetch_page(filter_expression, 1)
        data = first.get("data") or {}
        rows = self._diff_rows(data.get("diff"))
        total = int(data.get("total") or len(rows))
        effective_page_size = len(rows) or self.page_size
        pages = max(1, math.ceil(total / effective_page_size))
        for page in range(2, pages + 1):
            page_data = (self._fetch_page(filter_expression, page).get("data") or {})
            rows.extend(self._diff_rows(page_data.get("diff")))
        return rows

    def _fetch_page(self, filter_expression: str, page: int) -> dict:
        params = {
            "pn": str(page), "pz": str(self.page_size), "po": "1", "np": "1",
            "ut": EASTMONEY_TOKEN, "fltt": "1", "invt": "2", "fid": "f3",
            "dect": "1", "timil": "1", "fs": filter_expression, "fields": "f12,f14",
        }
        errors = []
        for attempt in range(self.maximum_retries):
            for host in EASTMONEY_HOSTS:
                try:
                    response = self.session.get(
                        f"{host}/webguest/api/qt/clist/get", params=params, timeout=self.timeout
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("rc") == 0 and payload.get("data") is not None:
                        return payload
                    errors.append(f"{host}: rc={payload.get('rc')}")
                except Exception as exc:
                    errors.append(f"{host}: {type(exc).__name__}")
            if attempt + 1 < self.maximum_retries:
                time.sleep(min(2 ** attempt, 4))
        raise RuntimeError("东方财富板块接口失败: " + "; ".join(errors[-6:]))

    @staticmethod
    def _diff_rows(value: object) -> list[dict]:
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            return [row for row in value.values() if isinstance(row, dict)]
        return []


SECTOR_FIELD_ALIASES = {
    "amount": ("成交额(合计)", "成交额"),
    "return_pct": ("成份区间涨跌幅", "涨跌幅"),
    "main_net_inflow": ("主力净流入资金",),
    "main_inflow": ("主力流入资金",),
    "main_outflow": ("主力流出资金",),
    "large_inflow": ("大单流入资金",),
    "large_outflow": ("大单流出资金",),
    "medium_inflow": ("中单流入资金",),
    "medium_outflow": ("中单流出资金",),
    "small_inflow": ("小单流入资金",),
    "small_outflow": ("小单流出资金",),
}

STOCK_FIELD_ALIASES = {
    "amount": ("成交额",),
    "main_net_inflow": ("主力净流入资金",),
    "super_large_net_inflow": ("超大单净流入资金",),
    "large_net_inflow": ("大单净流入资金",),
    "medium_net_inflow": ("中单净流入资金",),
    "small_net_inflow": ("小单净流入资金",),
    "financing_buy": ("融资买入额",),
    "financing_repay": ("融资偿还额",),
    "financing_balance": ("融资余额",),
}


class MiaoxiangCapitalSource:
    """Fixed-query adapter for sector order flow, stock order flow and margin data."""

    def __init__(self, client: MiaoxiangSource | None = None, request_budget: RequestBudget | None = None):
        self.client = client or MiaoxiangSource()
        self.request_budget = request_budget

    @property
    def requests_used(self) -> int:
        return self.request_budget.used if self.request_budget else 0

    def _query(self, query: str):
        if self.request_budget:
            self.request_budget.consume()
        return self.client.query_tool(query)

    def fetch_sector(self, bk_code: str, bk_name: str, start_date: str, end_date: str, fortune_level: int = 1):
        level_name = {1: "一级", 2: "二级"}.get(int(fortune_level))
        if not level_name:
            raise ValueError(f"不支持的财富通行业层级: {fortune_level}")
        query = (
            f"财富通{level_name}行业板块“{bk_name}”（{bk_code}）{start_date}至{end_date}每个交易日的"
            "成交额、成份涨跌幅、主力流入资金、主力流出资金、主力净流入资金、"
            "大单流入资金、大单流出资金、中单流入资金、中单流出资金、小单流入资金、小单流出资金"
        )
        result, error = self._query(query)
        if result is None:
            return [], {}, query, error
        rows, contracts, returned = self._parse_result(result, SECTOR_FIELD_ALIASES, {bk_code})
        if bk_code not in returned:
            return [], contracts, query, f"妙想未返回预期板块 {bk_code}，实际: {sorted(returned)}"
        output = [dict(item, bk_code=bk_code) for item in rows.get(bk_code, {}).values()]
        return sorted(output, key=lambda item: item["trade_date"]), contracts, query, None

    def fetch_stocks(self, entities: list[dict], start_date: str, end_date: str):
        requested = {normalize_stock_code(item["code"]) for item in entities}
        labels = "、".join(
            f"{normalize_stock_code(item['code'])}{str(item.get('name') or '').strip()}" for item in entities
        )
        query = (
            f"{labels}在{start_date}至{end_date}每个交易日的成交额、主力净流入、超大单净流入、"
            "大单净流入、中单净流入、小单净流入、融资买入额、融资偿还额、融资余额"
        )
        result, error = self._query(query)
        if result is None:
            return {}, requested, {}, query, error
        rows, contracts, returned = self._parse_result(result, STOCK_FIELD_ALIASES, requested)
        missing = requested - returned
        normalized = {code: sorted(by_date.values(), key=lambda item: item["trade_date"])
                      for code, by_date in rows.items() if code in requested}
        return normalized, missing, contracts, query, None

    def _parse_result(self, result: dict, aliases: dict, expected_codes: set[str]):
        try:
            tables = result["data"]["data"]["searchDataResultDTO"]["dataTableDTOList"]
        except (KeyError, TypeError):
            return {}, {}, set()
        rows: dict[str, dict[str, dict]] = {}
        contracts: dict[tuple[str, str], dict] = {}
        returned = set()
        for table in tables or []:
            raw_code = str(table.get("code") or table.get("entityTagDTO", {}).get("secuCode") or "").strip()
            board_match = re.search(r"BK\d+", raw_code)
            code = board_match.group(0) if board_match else normalize_stock_code(raw_code)
            if code not in expected_codes:
                continue
            returned.add(code)
            name_map = table.get("nameMap") or {}
            raw = table.get("rawTable") or table.get("table") or {}
            dates = raw.get("headName") or []
            resolved = self._resolve_fields(name_map, aliases)
            for metric, field_code in resolved.items():
                contracts[(code, metric)] = {
                    "source_field_code": field_code,
                    "source_field_name": str(name_map.get(field_code) or ""),
                }
                values = raw.get(field_code) or []
                for index, date_value in enumerate(dates):
                    trade_date = normalize_trade_date(date_value)
                    if not trade_date or index >= len(values):
                        continue
                    item = rows.setdefault(code, {}).setdefault(trade_date, {"trade_date": trade_date})
                    item[metric] = parse_cn_number(values[index])
        return rows, contracts, returned

    @staticmethod
    def _resolve_fields(name_map: dict, aliases: dict) -> dict[str, str]:
        resolved = {}
        for field_code, label_value in name_map.items():
            if field_code == "headNameSub":
                continue
            label = str(label_value)
            for metric, patterns in aliases.items():
                if metric in resolved:
                    continue
                if any(pattern in label for pattern in patterns):
                    if metric.startswith("large_") and "超大单" in label:
                        continue
                    if metric in {"main_inflow", "large_inflow", "medium_inflow", "small_inflow"} and "净流入" in label:
                        continue
                    resolved[metric] = str(field_code)
        return resolved


def contract_field_matches_metric(entity_type: str, metric: str, field_name: str) -> bool:
    """Return whether a source label still has the controlled metric semantics."""
    aliases = SECTOR_FIELD_ALIASES if entity_type == "sector" else STOCK_FIELD_ALIASES if entity_type == "stock" else None
    if aliases is None or metric not in aliases or not str(field_name or "").strip():
        return False
    resolved = MiaoxiangCapitalSource._resolve_fields({"candidate": field_name}, aliases)
    return resolved.get(metric) == "candidate"


def utc_timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")
