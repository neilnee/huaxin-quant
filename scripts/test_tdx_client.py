"""Offline regressions for handshake compatibility and server selection."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd
from tdxpy.exceptions import TdxFunctionCallError
from tdxpy.hq import TdxHq_API
from tdxpy.parser.base import ResponseHeaderRecvFails
from tdxpy.parser.std.get_security_bars import GetSecurityBarsCmd

from scripts.data import tdx_client
from scripts.data.market_data import TDXSource
from scripts.data.market_data_service import MarketDataService
from scripts.data.tdx_block_data import TDXBlockSource


def daily_frame():
    return pd.DataFrame([{
        "datetime": "2026-10-23 15:00", "open": 14.77, "high": 15.38,
        "low": 14.61, "close": 15.23, "volume": 265964.0, "amount": 399586048.0,
    }])


def quotes(frame=None):
    result = Mock()
    result.bars.return_value = daily_frame() if frame is None else frame
    result.index.return_value = daily_frame()
    result.server = ("127.0.0.1", 7709)
    return result


class TDXClientTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.cache = Path(self.folder.name) / "servers.json"
        self.cache_patch = patch.object(tdx_client, "CACHE_PATH", self.cache)
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)
        self.discovered_patch = patch.object(tdx_client, "_discovered", None)
        self.discovered_patch.start()
        self.addCleanup(self.discovered_patch.stop)
        self.failed_patch = patch.object(tdx_client, "_failed_protocols", {})
        self.failed_patch.start()
        self.addCleanup(self.failed_patch.stop)

    def test_handshake_is_local_and_preserves_original_api(self):
        original_setup = TdxHq_API.setup
        api = tdx_client.ModernTDXAPI()
        api.client = object()
        with patch.object(tdx_client, "RawParser") as parser:
            parser.return_value.call_api.return_value = b"ack"
            api.setup()
            self.assertEqual(parser.return_value.setParams.call_args_list[0].args, (tdx_client.MODERN_INIT,))
            self.assertEqual(parser.return_value.setParams.call_args_list[1].args, (tdx_client.MODERN_AUTH,))
            self.assertEqual(parser.return_value.call_api.call_count, 2)
        api.client = None
        self.assertIs(TdxHq_API.setup, original_setup)

    def test_empty_handshake_is_not_a_successful_connection(self):
        api = tdx_client.ModernTDXAPI()
        api.client = object()
        with patch.object(tdx_client, "RawParser") as parser:
            parser.return_value.call_api.return_value = b""
            with self.assertRaisesRegex(RuntimeError, "空响应"):
                api.setup()
        api.client = None

    def test_real_wire_fixture_keeps_existing_ohlcv_and_source_contract(self):
        # Captured after modern handshake; Oct 21/22 matched the existing TDX DB.
        body = bytes.fromhex(
            "03009d28350184ea0100ae01ec0440f40c48ffe54d4d"
            "9e2835010ad602b402dc030097af47df92ff4c"
            "9f283501c6018c07a209e00280dd81489889be4d"
        )
        decoder = GetSecurityBarsCmd(None)
        decoder.category = 9
        frame = pd.DataFrame(decoder.parseResponse(body))
        rows = MarketDataService.normalized_bars(frame, "603766", "2026-10-22")
        self.assertEqual([r[1] for r in rows], ["2026-10-21", "2026-10-22"])
        self.assertEqual(rows[1][2:9], (14.99, 15.17, 14.77, 14.84, 89902.0, 133994232.0, "tdx"))
        latest = MarketDataService.normalized_bars(frame, "603766", "2026-10-23")[-1]
        self.assertEqual(latest[2:9], (14.77, 15.38, 14.61, 15.23, 265964.0, 399586048.0, "tdx"))

    def test_candidates_keep_full_pool_if_discovery_was_consumed(self):
        discovery = Mock()
        discovery.server.side_effect = ValueError("Set of Tasks/Futures is empty")
        tdx_client.save_cached_servers([("cached", 7709)])
        with patch.object(tdx_client.importlib, "import_module", return_value=discovery), \
             patch.object(tdx_client, "hq_hosts", [("a", "old", 7709)]), \
             patch.object(tdx_client, "HQ_HOSTS", [("b", "new", 7709)]):
            expected = [("cached", 7709), ("old", 7709), ("new", 7709)]
            self.assertEqual(tdx_client.server_candidates(), expected)
            self.assertEqual(tdx_client.server_candidates(), expected)
        self.assertEqual(discovery.server.call_count, 1)

    def test_discovery_latency_is_sorted_without_using_console_mode(self):
        discovery = Mock()
        discovery.server.return_value = [("slow", 7709), ("fast", 7709)]
        discovery.results = {"HQ": [
            {"addr": "slow", "port": 7709, "time": 20},
            {"addr": "fast", "port": 7709, "time": 2},
        ]}
        with patch.object(tdx_client.importlib, "import_module", return_value=discovery):
            self.assertEqual(tdx_client.server_candidates()[:2], [("fast", 7709), ("slow", 7709)])

    def test_sixth_candidate_is_tried_and_only_validated_node_is_added_to_cache(self):
        nodes = [(f"host{i}", 7709) for i in range(6)]
        good = quotes()
        bad = [quotes(pd.DataFrame()) for _ in range(5)]
        with patch.object(tdx_client, "server_candidates", return_value=nodes), \
             patch.object(tdx_client, "ModernTDXQuotes", side_effect=[*bad, good]), \
             patch.object(tdx_client.Quotes, "factory") as legacy:
            selected = tdx_client.connect_tdx("stock")
        self.assertIs(selected, good)
        legacy.assert_not_called()
        self.assertEqual(tdx_client.load_cached_servers(), [nodes[-1]])
        for failed in bad:
            failed.client.disconnect.assert_called_once()

    def test_modern_failure_uses_legacy_without_replacing_good_cache_with_failed_nodes(self):
        tdx_client.save_cached_servers([("previous", 7709)])
        bad = quotes(pd.DataFrame())
        good = quotes()
        with patch.object(tdx_client, "server_candidates", return_value=[("candidate", 7709)]), \
             patch.object(tdx_client, "ModernTDXQuotes", return_value=bad), \
             patch.object(tdx_client.Quotes, "factory", return_value=good):
            self.assertIs(tdx_client.connect_tdx("market"), good)
        self.assertEqual(good.tdx_protocol, "legacy")
        self.assertEqual(tdx_client.load_cached_servers(), [("candidate", 7709), ("previous", 7709)])

    def test_all_failures_are_cached_per_capability_and_preserve_server_cache(self):
        tdx_client.save_cached_servers([("previous", 7709)])
        before = self.cache.read_bytes()
        with patch.object(tdx_client, "server_candidates", return_value=[("bad", 7709)]), \
             patch.object(tdx_client, "ModernTDXQuotes", side_effect=RuntimeError("bad handshake")) as modern, \
             patch.object(tdx_client.Quotes, "factory", side_effect=RuntimeError("bad legacy")) as legacy:
            for _ in range(2):
                with self.assertRaisesRegex(RuntimeError, "bad handshake"):
                    tdx_client.connect_tdx("market")
            self.assertEqual(modern.call_count, 1)
            self.assertEqual(legacy.call_count, 1)
        self.assertEqual(self.cache.read_bytes(), before)
        file_client = quotes()
        file_client.client.get_block_info_meta.return_value = {"size": 1000}
        file_client.client.get_block_info.return_value = b"data"
        with patch.object(tdx_client, "server_candidates", return_value=[("good", 7709)]), \
             patch.object(tdx_client, "ModernTDXQuotes", return_value=file_client):
            self.assertIs(tdx_client.connect_tdx("files"), file_client)
        file_client.index.assert_not_called()
        file_client.bars.assert_not_called()

    def test_malformed_frame_does_not_pass_health_check(self):
        changes = [{"high": 14.0}, {"low": 16.0}, {"close": float("nan")},
                   {"datetime": "invalid"}, {"volume": 0}, {"amount": float("inf")}]
        for change in changes:
            with self.subTest(change=change):
                frame = daily_frame().assign(**change)
                with self.assertRaises(ValueError):
                    tdx_client.validate_daily_frame(frame)
        with self.assertRaises(ValueError):
            tdx_client.validate_daily_frame(pd.concat([daily_frame(), daily_frame()]))

    def test_protocol_failure_keeps_miaoxiang_fallback_and_does_not_reprobe_per_stock(self):
        source = TDXBlockSource()
        fallback = Mock()
        fallback.fetch_bars.return_value = (daily_frame(), None)
        service = MarketDataService({"data": {}})
        with patch.object(tdx_client, "server_candidates", return_value=[("bad", 7709)]), \
             patch.object(tdx_client, "ModernTDXQuotes", side_effect=RuntimeError("modern unavailable")) as modern, \
             patch.object(tdx_client.Quotes, "factory", side_effect=RuntimeError("legacy unavailable")) as legacy:
            for code in ["603766", "600838"]:
                rows = service.fetch_stock_bars(source, fallback, code, code, 200, "2026-10-23")
                self.assertEqual(rows[-1][1], "2026-10-23")
                self.assertEqual(rows[-1][8], "miaoxiang")
            self.assertEqual(modern.call_count, 1)
            self.assertEqual(legacy.call_count, 1)
        self.assertEqual(fallback.fetch_bars.call_count, 2)

    def test_stock_transport_failure_reselects_once_and_reuses_recovered_connection(self):
        broken, recovered = quotes(), quotes()
        wrapped = TdxFunctionCallError("calling function error")
        wrapped.original_exception = ConnectionResetError("peer reset")
        broken.bars.side_effect = wrapped
        source = TDXBlockSource()
        fallback = Mock()
        service = MarketDataService({"data": {}})
        with patch("scripts.data.tdx_block_data.connect_tdx", side_effect=[broken, recovered]) as connect:
            for code in ("603766", "600838"):
                rows = service.fetch_stock_bars(source, fallback, code, code, 200, "2026-10-23")
                self.assertEqual(rows[-1][8], "tdx")
            self.assertEqual(connect.call_count, 2)
        broken.client.disconnect.assert_called_once()
        broken.bars.assert_called_once_with(symbol="603766", frequency=9, offset=200)
        self.assertEqual(recovered.bars.call_count, 2)
        fallback.fetch_bars.assert_not_called()
        self.assertIs(source._quotes, recovered)

    def test_index_transport_failure_reselects_and_retries_same_request(self):
        broken, recovered = quotes(), quotes()
        broken.index.side_effect = ResponseHeaderRecvFails("connection closed before header")
        source = TDXBlockSource()
        with patch("scripts.data.tdx_block_data.connect_tdx", side_effect=[broken, recovered]) as connect:
            self.assertIs(source.fetch_index_bars("000300", 300), recovered.index.return_value)
            self.assertEqual(connect.call_count, 2)
        broken.client.disconnect.assert_called_once()
        recovered.index.assert_called_once_with(symbol="000300", frequency=9, offset=300)

    def test_second_transport_failure_falls_back_and_does_not_keep_dead_connection(self):
        first, second, recovered = quotes(), quotes(), quotes()
        first.bars.side_effect = TimeoutError("read timeout")
        second.bars.side_effect = BrokenPipeError("socket closed")
        source = TDXBlockSource()
        fallback = Mock()
        fallback.fetch_bars.return_value = (daily_frame(), None)
        service = MarketDataService({"data": {}})
        with patch("scripts.data.tdx_block_data.connect_tdx", side_effect=[first, second, recovered]) as connect:
            rows = service.fetch_stock_bars(source, fallback, "603766", "603766", 200, "2026-10-23")
            self.assertEqual(rows[-1][8], "miaoxiang")
            self.assertEqual(connect.call_count, 2)
            self.assertIsNone(source._quotes)
            rows = service.fetch_stock_bars(source, fallback, "600838", "600838", 200, "2026-10-23")
            self.assertEqual(rows[-1][8], "tdx")
            self.assertEqual(connect.call_count, 3)
        first.client.disconnect.assert_called_once()
        second.client.disconnect.assert_called_once()
        fallback.fetch_bars.assert_called_once()

    def test_reselection_failure_does_not_retry_selection_or_retain_dead_connection(self):
        broken = quotes()
        broken.bars.side_effect = ConnectionResetError("peer reset")
        source = TDXBlockSource()
        with patch("scripts.data.tdx_block_data.connect_tdx", side_effect=[broken, RuntimeError("pool exhausted")]) as connect:
            with self.assertRaisesRegex(RuntimeError, "pool exhausted"):
                source.fetch_stock_bars("603766", 200)
            self.assertEqual(connect.call_count, 2)
        self.assertIsNone(source._quotes)
        broken.client.disconnect.assert_called_once()

    def test_parse_errors_and_empty_or_stale_bars_do_not_reconnect(self):
        wrapped = TdxFunctionCallError("calling function error")
        wrapped.original_exception = ValueError("malformed bars")
        for result in (ValueError("bad parameter"), wrapped, pd.DataFrame(),
                       daily_frame().assign(datetime="2026-10-22 15:00")):
            with self.subTest(result=type(result).__name__):
                selected = quotes()
                if isinstance(result, Exception):
                    selected.bars.side_effect = result
                else:
                    selected.bars.return_value = result
                source = TDXBlockSource()
                fallback = Mock()
                fallback.fetch_bars.return_value = (daily_frame(), None)
                service = MarketDataService({"data": {}})
                with patch("scripts.data.tdx_block_data.connect_tdx", return_value=selected) as connect:
                    rows = service.fetch_stock_bars(source, fallback, "603766", "603766", 200, "2026-10-23")
                    self.assertEqual(rows[-1][8], "miaoxiang")
                    connect.assert_called_once_with("market", 10)
                selected.client.disconnect.assert_not_called()
                fallback.fetch_bars.assert_called_once()

    def test_standalone_stock_adapter_recovers_transport_before_returning_bars(self):
        broken, recovered = quotes(), quotes()
        broken.bars.side_effect = ConnectionResetError("peer reset")
        source = TDXSource()
        with patch.object(tdx_client, "connect_tdx", side_effect=[broken, recovered]) as connect:
            frame, error = source.fetch_bars("603766", "test")
            self.assertIsNone(error)
            self.assertEqual(frame.iloc[-1]["date"], "2026-10-23")
            self.assertEqual(connect.call_count, 2)
        broken.client.disconnect.assert_called_once()
        recovered.bars.assert_called_once_with(symbol="603766", frequency=9, offset=200)
        self.assertIs(source._client, recovered)

    def test_both_adapters_share_selector_with_separate_capabilities(self):
        selected = quotes()
        with patch("scripts.data.tdx_block_data.connect_tdx", return_value=selected) as connect:
            source = TDXBlockSource()
            source._client("files")
            source._client("files")
            source._corporate_action_client()
            self.assertEqual(connect.call_args_list[0].args, ("files", 10))
            self.assertEqual(connect.call_args_list[1].args, ("xdxr", 10))
            self.assertEqual(connect.call_count, 2)
        with patch.object(tdx_client, "connect_tdx", return_value=selected) as connect:
            source = TDXSource()
            self.assertIs(source._get_client(), selected)
            self.assertIs(source._get_client(), selected)
            connect.assert_called_once_with("stock")


if __name__ == "__main__":
    unittest.main()
