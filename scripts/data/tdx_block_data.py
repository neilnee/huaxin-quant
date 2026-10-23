"""TDX block-file download and parsing for the independent market-regime module."""

from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import dataclass
from typing import Iterable

from tdxpy.reader.block_reader import BlockReader, BlockReader_TYPE_FLAT

from scripts.data.tdx_client import connect_tdx, connection_info, request_tdx


DEFAULT_CHUNK_SIZE = 0x7530


@dataclass(frozen=True)
class BlockFile:
    filename: str
    declared_size: int
    payload: bytes
    rows: list[dict]

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


@dataclass(frozen=True)
class IndustryData:
    """Point-in-time TDX industry membership plus its code dictionary."""

    tdxhy_payload: bytes
    incon_payload: bytes
    mappings: list[dict]
    definitions: dict[str, tuple[str, str]]

    @property
    def tdxhy_sha256(self) -> str:
        return hashlib.sha256(self.tdxhy_payload).hexdigest()

    @property
    def incon_sha256(self) -> str:
        return hashlib.sha256(self.incon_payload).hexdigest()


class TDXBlockSource:
    """Small TDX adapter shared only by the market-regime data layer.

    tdxpy 0.2.7 requests every block fragment with the complete file length.  This
    source always sends the protocol chunk length for the final argument.
    """

    def __init__(self, timeout: int = 10, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.timeout = timeout
        self.chunk_size = chunk_size
        self._quotes = None
        self._action_quotes = None
        self._file_quotes = None
        self._security_quotes = None

    def _client(self, capability="market"):
        attribute = {"market": "_quotes", "files": "_file_quotes", "securities": "_security_quotes"}[capability]
        if getattr(self, attribute) is None:
            setattr(self, attribute, connect_tdx(capability, self.timeout))
        return getattr(self, attribute)

    def _corporate_action_client(self):
        """Select an HQ server by its XDXR capability, independently of index bars."""
        if self._action_quotes is not None:
            return self._action_quotes
        self._action_quotes = connect_tdx("xdxr", self.timeout)
        return self._action_quotes

    def connection_info(self):
        return {name: connection_info(client) for name, client in {
            "market": self._quotes, "files": self._file_quotes,
            "securities": self._security_quotes, "xdxr": self._action_quotes,
        }.items() if client is not None}

    def fetch_block_file(self, filename: str) -> BlockFile:
        quotes = self._client("files")
        meta = quotes.client.get_block_info_meta(filename)
        if not meta or not meta.get("size"):
            raise RuntimeError(f"未获得板块文件元信息: {filename}")
        declared_size = int(meta["size"])
        payload = bytearray()
        for start in range(0, declared_size, self.chunk_size):
            size = min(self.chunk_size, declared_size - start)
            part = quotes.client.get_block_info(filename, start, size)
            if not part:
                raise RuntimeError(f"板块文件分块为空: {filename} start={start}")
            payload.extend(part)
        payload = bytes(payload[:declared_size])
        if len(payload) != declared_size:
            raise RuntimeError(f"板块文件大小不一致: {filename} {len(payload)} != {declared_size}")
        rows = BlockReader().get_data(bytearray(payload), BlockReader_TYPE_FLAT)
        if not rows:
            raise RuntimeError(f"板块文件解析为空: {filename}")
        return BlockFile(filename, declared_size, payload, rows)

    def fetch_blocks(self, filenames: Iterable[str]) -> list[BlockFile]:
        return [self.fetch_block_file(name) for name in filenames]

    def fetch_report_file(self, filename: str) -> bytes:
        """Download a report/config file until the server returns an empty chunk."""
        payload = self._client("files").client.get_report_file_by_size(filename)
        data = bytes(payload)
        if not data:
            raise RuntimeError(f"通达信配置文件为空: {filename}")
        return data

    def fetch_industry_data(self) -> IndustryData:
        """Fetch tdxhy.cfg and industry names embedded in zhb.zip/incon.dat."""
        tdxhy = self.fetch_report_file("tdxhy.cfg")
        zhb = self.fetch_report_file("zhb.zip")
        try:
            with zipfile.ZipFile(io.BytesIO(zhb)) as archive:
                incon = archive.read("incon.dat")
        except Exception as exc:
            raise RuntimeError(f"zhb.zip 中未能读取 incon.dat: {exc}") from exc
        definitions: dict[str, tuple[str, str]] = {}
        section = ""
        for line in incon.decode("gbk", "replace").splitlines():
            if line.startswith("#"):
                section = line[1:].strip()
            elif "|" in line:
                code, name = line.split("|", 1)
                definitions[code.strip()] = (section, name.strip())
        mappings = []
        for line in tdxhy.decode("gbk", "replace").splitlines():
            fields = line.split("|")
            if len(fields) < 6:
                continue
            code = fields[1].strip().zfill(6)
            tdx_code, sw_code = fields[2].strip(), fields[5].strip()
            if code.isdigit() and (tdx_code or sw_code):
                mappings.append({"market": fields[0].strip(), "code": code, "tdx_code": tdx_code, "sw_code": sw_code})
        if not mappings or not definitions:
            raise RuntimeError("通达信行业配置解析为空")
        return IndustryData(tdxhy, incon, mappings, definitions)

    def _fetch_bars(self, method: str, code: str, offset: int):
        return request_tdx(
            self._client,
            lambda: setattr(self, "_quotes", None),
            lambda quotes: getattr(quotes, method)(symbol=code, frequency=9, offset=offset),
        )

    def fetch_index_bars(self, code: str, offset: int):
        return self._fetch_bars("index", code, offset)

    def fetch_stock_bars(self, code: str, offset: int):
        return self._fetch_bars("bars", code, offset)

    def fetch_corporate_actions(self, code: str):
        """Return the raw TDX XDXR frame; normalization belongs to the data layer."""
        return self._corporate_action_client().xdxr(symbol=code)

    def fetch_security_lists(self):
        quotes = self._client("securities")
        return {0: quotes.stocks(market=0), 1: quotes.stocks(market=1)}
