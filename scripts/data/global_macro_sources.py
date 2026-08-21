"""Official-source adapters for the Global Macro data layer."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Callable

import requests


@dataclass
class SourceResult:
    source_id: str
    url: str
    ok: bool
    checked_at: str
    latency_ms: int
    http_status: int | None = None
    content_type: str = ""
    content_bytes: int = 0
    content_hash: str = ""
    latest_observation_date: str | None = None
    observations: list[dict] = field(default_factory=list)
    events: list[dict] = field(default_factory=list)
    error: str | None = None


class SourceParseError(ValueError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_date(value: object) -> str | None:
    text = str(value or "").strip()
    for pattern in ("%Y-%m-%d", "%m/%d/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], pattern).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"20\d{2}-\d{2}-\d{2}", text)
    return match.group(0) if match else None


def parse_number(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
        return number if math.isfinite(number) else None
    except ValueError:
        return None


def slug(value: object) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", str(value or "").strip()).strip("_")
    return normalized.upper() or "UNKNOWN"


def observation(
    source_id: str,
    series_id: str,
    observed_at: str,
    value: float,
    unit: str,
    frequency: str,
    url: str,
    raw: dict,
) -> dict:
    return {
        "source_id": source_id,
        "series_id": series_id,
        "observed_at": observed_at,
        "value": value,
        "unit": unit,
        "frequency": frequency,
        "source_url": url,
        "raw": raw,
    }


def parse_nyfed_rates(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    payload = response.json()
    rows = payload.get("refRates") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceParseError("NY Fed refRates missing")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        observed_at = parse_date(row.get("effectiveDate"))
        rate_type = slug(row.get("type"))
        value_key = next((key for key in ("percentRate", "rate", "index") if parse_number(row.get(key)) is not None), None)
        value = parse_number(row.get(value_key)) if value_key else None
        if observed_at and value is not None:
            unit = "index" if value_key == "index" else "percent"
            result.append(observation(source_id, f"NYFED_{rate_type}", observed_at, value, unit, "daily", response.url, row))
    if not result:
        raise SourceParseError("NY Fed response contains no usable rates")
    return result, []


def parse_nyfed_onrrp(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    payload = response.json()
    repo = payload.get("repo") if isinstance(payload, dict) else None
    rows = repo.get("operations") if isinstance(repo, dict) else None
    if not isinstance(rows, list):
        raise SourceParseError("NY Fed repo.operations missing")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        observed_at = parse_date(row.get("operationDate") or row.get("effectiveDate"))
        amount = next(
            (
                parse_number(row.get(key))
                for key in ("totalAmtAccepted", "amtAccepted", "totalAmountAccepted")
                if parse_number(row.get(key)) is not None
            ),
            None,
        )
        if observed_at and amount is not None:
            result.append(observation(source_id, "NYFED_ONRRP_ACCEPTED", observed_at, amount, "usd", "daily", response.url, row))
    if not result:
        raise SourceParseError("NY Fed ON RRP response contains no usable operations")
    return result, []


def parse_fiscaldata_tga(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise SourceParseError("FiscalData TGA data missing")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        account_type = str(row.get("account_type") or "")
        if "Treasury General Account" not in account_type or "Closing Balance" not in account_type:
            continue
        observed_at = parse_date(row.get("record_date"))
        # FiscalData currently publishes the Table I line-item value in
        # open_today_bal, including the row explicitly named Closing Balance.
        value = parse_number(row.get("close_today_bal"))
        if value is None:
            value = parse_number(row.get("open_today_bal"))
        if observed_at and value is not None:
            result.append(observation(source_id, "TGA_CLOSING_BALANCE", observed_at, value, "million_usd", "daily", response.url, row))
    if not result:
        raise SourceParseError("FiscalData TGA response contains no usable balances")
    return result, []


def parse_fiscaldata_debt(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    payload = response.json()
    rows = payload.get("data") if isinstance(payload, dict) else None
    fields = {
        "debt_held_public_amt": "US_DEBT_PUBLIC",
        "intragov_hold_amt": "US_DEBT_INTRAGOV",
        "tot_pub_debt_out_amt": "US_DEBT_TOTAL",
    }
    if not isinstance(rows, list):
        raise SourceParseError("FiscalData debt data missing")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        observed_at = parse_date(row.get("record_date"))
        for field_name, series_id in fields.items():
            value = parse_number(row.get(field_name))
            if observed_at and value is not None:
                result.append(observation(source_id, series_id, observed_at, value, "usd", "daily", response.url, row))
    if not result:
        raise SourceParseError("FiscalData debt response contains no usable values")
    return result, []


TREASURY_COLUMNS = {
    "1 Mo": "UST_1M", "1.5 Month": "UST_1_5M", "2 Mo": "UST_2M", "3 Mo": "UST_3M",
    "4 Mo": "UST_4M", "6 Mo": "UST_6M", "1 Yr": "UST_1Y", "2 Yr": "UST_2Y",
    "3 Yr": "UST_3Y", "5 Yr": "UST_5Y", "7 Yr": "UST_7Y", "10 Yr": "UST_10Y",
    "20 Yr": "UST_20Y", "30 Yr": "UST_30Y",
}


def parse_treasury_yield(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    rows = list(csv.DictReader(io.StringIO(response.text)))
    result = []
    for row in rows:
        observed_at = parse_date(row.get("Date"))
        if not observed_at:
            continue
        for field_name, series_id in TREASURY_COLUMNS.items():
            value = parse_number(row.get(field_name))
            if value is not None:
                result.append(observation(source_id, series_id, observed_at, value, "percent", "daily", response.url, row))
    if not result:
        raise SourceParseError("Treasury CSV contains no usable yield observations")
    return result, []


def parse_ecb_fx(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    rows = list(csv.DictReader(io.StringIO(response.text)))
    result = []
    for row in rows:
        observed_at = parse_date(row.get("TIME_PERIOD"))
        value = parse_number(row.get("OBS_VALUE"))
        if observed_at and value is not None:
            result.append(observation(source_id, "ECB_EURUSD", observed_at, value, "usd_per_eur", "daily", response.url, row))
    if not result:
        raise SourceParseError("ECB CSV contains no usable FX observations")
    return result, []


def parse_cboe_vix(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    rows = list(csv.DictReader(io.StringIO(response.text)))
    result = []
    for row in rows:
        observed_at = parse_date(row.get("DATE"))
        if not observed_at:
            continue
        for field_name in ("OPEN", "HIGH", "LOW", "CLOSE"):
            value = parse_number(row.get(field_name))
            if value is not None:
                result.append(observation(source_id, f"VIX_{field_name}", observed_at, value, "index", "daily", response.url, row))
    if not result:
        raise SourceParseError("Cboe CSV contains no usable VIX observations")
    return result, []


def _node_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in list(node):
        local_name = child.tag.rsplit("}", 1)[-1]
        if local_name in names and child.text:
            return child.text.strip()
    return ""


def parse_rss(source_id: str, response: requests.Response) -> tuple[list[dict], list[dict]]:
    root = ET.fromstring(response.content)
    nodes = [node for node in root.iter() if node.tag.rsplit("}", 1)[-1] in {"item", "entry"}]
    events = []
    for node in nodes:
        title = _node_text(node, ("title",))
        link = _node_text(node, ("link",))
        if not link:
            link_node = next((child for child in list(node) if child.tag.rsplit("}", 1)[-1] == "link"), None)
            link = str(link_node.attrib.get("href") or "") if link_node is not None else ""
        guid = _node_text(node, ("guid", "id"))
        raw_date = _node_text(node, ("pubDate", "published", "updated", "date"))
        published_at = None
        if raw_date:
            try:
                parsed = parsedate_to_datetime(raw_date)
            except (TypeError, ValueError):
                try:
                    parsed = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                except ValueError:
                    parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                published_at = parsed.astimezone(timezone.utc).isoformat(timespec="seconds")
        if not title or not (link or guid):
            continue
        identity = guid or link
        event_id = hashlib.sha256(f"{source_id}|{identity}".encode()).hexdigest()
        events.append({
            "source_id": source_id,
            "event_id": event_id,
            "published_at": published_at,
            "event_time": None,
            "title": title,
            "url": link,
            "category": "official_release",
            "official_fact": title,
            "raw": {"guid": guid, "published": raw_date},
        })
    if not events:
        raise SourceParseError("RSS contains no usable items")
    return [], events


Parser = Callable[[str, requests.Response], tuple[list[dict], list[dict]]]


@dataclass(frozen=True)
class SourceDefinition:
    source_id: str
    parser: Parser
    url_builder: Callable[[date, dict], str]


def fixed_url(url: str) -> Callable[[date, dict], str]:
    return lambda _as_of, _config: url


def onrrp_url(as_of: date, config: dict) -> str:
    start = as_of - timedelta(days=int(config.get("lookback_days", 21)))
    return (
        "https://markets.newyorkfed.org/api/rp/reverserepo/propositions/search.json"
        f"?startDate={start.isoformat()}&endDate={as_of.isoformat()}&type=results"
        "&operationTypes=Reverse%20Repo"
    )


def treasury_yield_url(as_of: date, _config: dict) -> str:
    year = as_of.year
    return (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )


def ecb_fx_url(as_of: date, config: dict) -> str:
    start = as_of - timedelta(days=int(config.get("lookback_days", 31)))
    return (
        "https://data-api.ecb.europa.eu/service/data/EXR/D.USD.EUR.SP00.A"
        f"?startPeriod={start.isoformat()}&endPeriod={as_of.isoformat()}&format=csvdata"
    )


SOURCES = {
    "nyfed_rates": SourceDefinition("nyfed_rates", parse_nyfed_rates, fixed_url("https://markets.newyorkfed.org/api/rates/all/latest.json")),
    "nyfed_onrrp": SourceDefinition("nyfed_onrrp", parse_nyfed_onrrp, onrrp_url),
    "fiscaldata_tga": SourceDefinition("fiscaldata_tga", parse_fiscaldata_tga, fixed_url("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v1/accounting/dts/operating_cash_balance?page%5Bsize%5D=10&sort=-record_date")),
    "fiscaldata_debt": SourceDefinition("fiscaldata_debt", parse_fiscaldata_debt, fixed_url("https://api.fiscaldata.treasury.gov/services/api/fiscal_service/v2/accounting/od/debt_to_penny?page%5Bsize%5D=5&sort=-record_date")),
    "treasury_yield": SourceDefinition("treasury_yield", parse_treasury_yield, treasury_yield_url),
    "ecb_fx": SourceDefinition("ecb_fx", parse_ecb_fx, ecb_fx_url),
    "cboe_vix": SourceDefinition("cboe_vix", parse_cboe_vix, fixed_url("https://cdn.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv")),
    "fed_press_rss": SourceDefinition("fed_press_rss", parse_rss, fixed_url("https://www.federalreserve.gov/feeds/press_all.xml")),
    "ecb_press_rss": SourceDefinition("ecb_press_rss", parse_rss, fixed_url("https://www.ecb.europa.eu/rss/press.html")),
}


class GlobalMacroClient:
    def __init__(self, config: dict, session: requests.Session | None = None):
        self.config = config
        self.http = config["http"]
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": self.http["user_agent"], "Accept": "*/*"})
        self.last_request_at = 0.0

    def fetch(self, source_id: str, as_of: date) -> SourceResult:
        definition = SOURCES[source_id]
        source_config = self.config["sources"][source_id]
        url = definition.url_builder(as_of, source_config)
        maximum_retries = int(self.http["maximum_retries"])
        last_error = None
        last_status = None
        last_latency_ms = 0
        last_content_type = ""
        last_content_bytes = 0
        last_content_hash = ""
        for attempt in range(maximum_retries):
            current_status = None
            elapsed_since_last = time.monotonic() - self.last_request_at
            wait_for = float(self.http["minimum_interval_seconds"]) - elapsed_since_last
            if wait_for > 0:
                time.sleep(wait_for)
            started = time.perf_counter()
            try:
                response = self.session.get(
                    url,
                    timeout=(int(self.http["connect_timeout_seconds"]), int(self.http["read_timeout_seconds"])),
                    allow_redirects=True,
                )
                self.last_request_at = time.monotonic()
                current_status = response.status_code
                last_status = current_status
                last_latency_ms = round((time.perf_counter() - started) * 1000)
                last_content_type = response.headers.get("content-type", "")
                last_content_bytes = len(response.content)
                last_content_hash = hashlib.sha256(response.content).hexdigest()
                response.raise_for_status()
                observations, events = definition.parser(source_id, response)
                dates = [item["observed_at"] for item in observations]
                dates.extend(item["published_at"][:10] for item in events if item.get("published_at"))
                return SourceResult(
                    source_id=source_id,
                    url=response.url,
                    ok=True,
                    checked_at=utc_now(),
                    latency_ms=last_latency_ms,
                    http_status=response.status_code,
                    content_type=last_content_type,
                    content_bytes=last_content_bytes,
                    content_hash=last_content_hash,
                    latest_observation_date=max(dates) if dates else None,
                    observations=observations,
                    events=events,
                )
            except (requests.RequestException, ValueError, ET.ParseError, csv.Error, json.JSONDecodeError) as exc:
                self.last_request_at = time.monotonic()
                last_latency_ms = round((time.perf_counter() - started) * 1000)
                last_status = current_status
                if current_status is None:
                    last_content_type = ""
                    last_content_bytes = 0
                    last_content_hash = ""
                last_error = f"{type(exc).__name__}: {str(exc)[:500]}"
                retryable = last_status is None or last_status in {408, 429} or (last_status is not None and last_status >= 500)
                if attempt + 1 >= maximum_retries or not retryable:
                    break
                time.sleep(float(self.http["backoff_seconds"]) * (2 ** attempt))
        return SourceResult(
            source_id=source_id,
            url=url,
            ok=False,
            checked_at=utc_now(),
            latency_ms=last_latency_ms,
            http_status=last_status,
            content_type=last_content_type,
            content_bytes=last_content_bytes,
            content_hash=last_content_hash,
            error=last_error or "unknown source error",
        )


def enabled_source_ids(config: dict) -> list[str]:
    return [source_id for source_id in SOURCES if config["sources"].get(source_id, {}).get("enabled")]


def validate_source_ids(config: dict, requested: list[str] | None) -> list[str]:
    enabled = enabled_source_ids(config)
    if not requested:
        return enabled
    unknown = [source_id for source_id in requested if source_id not in SOURCES]
    disabled = [source_id for source_id in requested if source_id in SOURCES and source_id not in enabled]
    if unknown:
        raise ValueError("未知信源: " + ",".join(unknown))
    if disabled:
        raise ValueError("信源未启用: " + ",".join(disabled))
    return list(dict.fromkeys(requested))
