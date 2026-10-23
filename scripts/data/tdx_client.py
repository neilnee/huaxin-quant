"""Project-owned TDX handshake compatibility and capability-based selection."""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from mootdx.consts import HQ_HOSTS
from mootdx.quotes import Quotes, StdQuotes
from tdxpy.constants import hq_hosts
from tdxpy.hq import TdxHq_API
from tdxpy.parser.raw_parser import RawParser

from scripts.io_utils import atomic_write_json
from scripts.shared import PROJECT_ROOT


CACHE_PATH = Path(PROJECT_ROOT) / "cache" / "tdx_servers.json"
MODERN_INIT = bytes.fromhex("0c0218940001030003000d0001")
MODERN_AUTH = bytes.fromhex(
    "0c031899000120002000db0f7464786c6576656c"
    "000000295cf740110000000000000000000000000005"
)
_discovered: list[tuple[str, int]] | None = None
_failed_protocols: dict[tuple[str, str], str] = {}


class ModernTDXAPI(TdxHq_API):
    """Change connection setup only; retain existing requests and decoders."""

    def setup(self):
        # Public tdxlevel handshake documented by ShanNing-YU/a-share-tdxdata.
        for payload in (MODERN_INIT, MODERN_AUTH):
            command = RawParser(self.client)
            command.setParams(payload)
            if not command.call_api():
                raise RuntimeError("通达信新版握手返回空响应")


class ModernTDXQuotes(StdQuotes):
    """Keep the mootdx method contract without its global factory/config state."""

    def __init__(self, server: tuple[str, int], timeout: float = 10):
        self.server = server
        self.bestip = server
        self.timeout = timeout
        self.client = ModernTDXAPI(raise_exception=True, auto_retry=False)
        try:
            if not self.client.connect(*server, time_out=timeout):
                raise RuntimeError("通达信新版客户端连接失败")
        except Exception:
            self.close()
            raise

    def close(self):
        close_client(self)


def close_client(quotes) -> None:
    api = getattr(quotes, "client", None)
    if api is not None:
        try:
            api.disconnect()
        except Exception:
            pass


def _deduplicate(nodes) -> list[tuple[str, int]]:
    result = []
    for node in nodes:
        try:
            address = (str(node[0]), int(node[1]))
        except (IndexError, TypeError, ValueError):
            continue
        if address not in result:
            result.append(address)
    return result


def load_cached_servers() -> list[tuple[str, int]]:
    try:
        payload = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return _deduplicate(payload) if isinstance(payload, list) else []
    except (OSError, ValueError):
        return []


def save_cached_servers(nodes) -> None:
    try:
        atomic_write_json(CACHE_PATH, _deduplicate(nodes)[:10])
    except OSError:
        pass


def server_candidates() -> list[tuple[str, int]]:
    global _discovered
    if _discovered is None:
        discovery = importlib.import_module("mootdx.server")
        try:
            found = discovery.server(index="HQ", limit=5, sync=False)
            timings = {(r["addr"], int(r["port"])): r["time"] for r in discovery.results["HQ"]}
            _discovered = sorted(found, key=lambda node: timings.get(tuple(node), float("inf")))
        except Exception:
            # Other consumers may already have exhausted mootdx's global list.
            _discovered = []
    builtin = [(node[1], node[2]) for node in [*hq_hosts, *HQ_HOSTS]]
    return _deduplicate([*load_cached_servers(), *_discovered, *builtin])


def validate_daily_frame(frame) -> None:
    required = {"datetime", "open", "high", "low", "close", "volume", "amount"}
    if frame is None or frame.empty or not required.issubset(frame.columns):
        raise ValueError("日线为空或缺少OHLCV/成交额字段")
    prices = frame[["open", "high", "low", "close"]].astype(float)
    if not np.isfinite(prices.to_numpy()).all() or (prices <= 0).any().any():
        raise ValueError("日线价格不是有效正数")
    if (prices.high < prices.max(axis=1)).any() or (prices.low > prices.min(axis=1)).any():
        raise ValueError("日线OHLC边界异常")
    if pd.to_datetime(frame.datetime, errors="coerce").isna().any() or frame.datetime.duplicated().any():
        raise ValueError("日线日期无效或重复")
    activity = frame[["volume", "amount"]].astype(float).to_numpy()
    if not np.isfinite(activity).all() or (activity <= 0).any():
        raise ValueError("验活样本缺少有效成交量/额")


def _validate_capability(quotes, capability: str) -> None:
    if capability in {"stock", "market"}:
        validate_daily_frame(quotes.bars(symbol="000001", frequency=9, offset=1))
    if capability == "market":
        validate_daily_frame(quotes.index(symbol="000001", frequency=9, offset=1))
    elif capability == "files":
        meta = quotes.client.get_block_info_meta("block_gn.dat")
        if not meta or not meta.get("size") or not quotes.client.get_block_info("block_gn.dat", 0, 100):
            raise ValueError("板块文件元信息或样本分块为空")
    elif capability == "securities":
        if not quotes.client.get_security_count(0) or not quotes.client.get_security_list(0, 0):
            raise ValueError("证券数量或证券列表为空")
    elif capability == "xdxr":
        frame = quotes.xdxr(symbol="000001")
        if frame is None or frame.empty:
            raise ValueError("除权信息为空")


def connection_info(quotes) -> dict:
    if quotes is None:
        return {}
    return {
        "provider": "tdx",
        "protocol": getattr(quotes, "tdx_protocol", "unknown"),
        "server": list(getattr(quotes, "server", ())),
        "capability": getattr(quotes, "tdx_capability", "unknown"),
    }


def connect_tdx(capability: str = "market", timeout: float = 10):
    if capability not in {"stock", "market", "files", "securities", "xdxr"}:
        raise ValueError(f"未知通达信能力: {capability}")
    nodes = server_candidates()
    failures = []
    for protocol in ("modern", "legacy"):
        key = (protocol, capability)
        if key in _failed_protocols:
            failures.append(_failed_protocols[key])
            continue
        errors = []
        for address in nodes:
            quotes = None
            try:
                quotes = ModernTDXQuotes(address, timeout) if protocol == "modern" else Quotes.factory(
                    market="std", server=address, timeout=timeout, auto_retry=False, raise_exception=True,
                )
                _validate_capability(quotes, capability)
                quotes.tdx_protocol = protocol
                quotes.tdx_capability = capability
                save_cached_servers([address, *load_cached_servers()])
                print(f"[tdx] protocol={protocol} server={address[0]}:{address[1]} capability={capability}")
                return quotes
            except Exception as exc:
                close_client(quotes)
                cause = getattr(exc, "original_exception", exc)
                errors.append(f"{address[0]}:{address[1]} {type(cause).__name__}: {cause}")
        detail = f"{protocol}/{capability}: {len(nodes)}台均未通过; {'; '.join(errors[:5])}"
        _failed_protocols[key] = detail
        failures.append(detail)
    raise RuntimeError(f"通达信能力验活失败: {' | '.join(failures)}")
