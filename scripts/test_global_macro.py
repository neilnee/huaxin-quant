import copy
import tempfile
import unittest
from datetime import date
from pathlib import Path

import requests

from scripts.data.global_macro_sources import (
    GlobalMacroClient,
    SourceParseError,
    SourceResult,
    onrrp_url,
    parse_cboe_vix,
    parse_ecb_fx,
    parse_fiscaldata_debt,
    parse_fiscaldata_tga,
    parse_nyfed_onrrp,
    parse_nyfed_rates,
    parse_rss,
    parse_treasury_yield,
    treasury_yield_url,
)
from scripts.data.global_macro_store import GlobalMacroStore
from scripts.strategy_config import load_strategy_config


def response(body: str, content_type: str = "application/json", status: int = 200, url: str = "https://example.test/data"):
    result = requests.Response()
    result.status_code = status
    result.url = url
    result.headers["content-type"] = content_type
    result._content = body.encode("utf-8")
    result.encoding = "utf-8"
    return result


class FakeSession:
    def __init__(self, responses):
        self.headers = {}
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        result = self.responses.pop(0)
        result.url = url
        return result


class GlobalMacroParserTests(unittest.TestCase):
    def test_nyfed_rates_and_onrrp(self):
        rates, _ = parse_nyfed_rates(
            "nyfed_rates",
            response('{"refRates":[{"effectiveDate":"2026-08-19","type":"SOFR","percentRate":4.12}]}'),
        )
        self.assertEqual(rates[0]["series_id"], "NYFED_SOFR")
        self.assertEqual(rates[0]["value"], 4.12)
        index_rows, _ = parse_nyfed_rates(
            "nyfed_rates",
            response('{"refRates":[{"effectiveDate":"2026-08-19","type":"SOFRAI","index":1.2554}]}'),
        )
        self.assertEqual(index_rows[0]["unit"], "index")
        operations, _ = parse_nyfed_onrrp(
            "nyfed_onrrp",
            response('{"repo":{"operations":[{"operationDate":"2026-08-19","totalAmtAccepted":"1.25"}]}}'),
        )
        self.assertEqual(operations[0]["series_id"], "NYFED_ONRRP_ACCEPTED")
        self.assertEqual(operations[0]["unit"], "usd")

    def test_fiscaldata_tga_and_debt(self):
        tga, _ = parse_fiscaldata_tga(
            "fiscaldata_tga",
            response('{"data":[{"record_date":"2026-08-18","account_type":"Treasury General Account (TGA) Opening Balance","open_today_bal":"700000"},{"record_date":"2026-08-18","account_type":"Treasury General Account (TGA) Closing Balance","close_today_bal":"null","open_today_bal":"812345"}]}'),
        )
        self.assertEqual(tga[0]["series_id"], "TGA_CLOSING_BALANCE")
        self.assertEqual(tga[0]["value"], 812345.0)
        self.assertEqual(tga[0]["unit"], "million_usd")
        debt, _ = parse_fiscaldata_debt(
            "fiscaldata_debt",
            response('{"data":[{"record_date":"2026-08-18","debt_held_public_amt":"10","intragov_hold_amt":"2","tot_pub_debt_out_amt":"12"}]}'),
        )
        self.assertEqual({row["series_id"] for row in debt}, {"US_DEBT_PUBLIC", "US_DEBT_INTRAGOV", "US_DEBT_TOTAL"})

    def test_csv_sources_normalize_dates_and_units(self):
        treasury, _ = parse_treasury_yield(
            "treasury_yield",
            response("Date,2 Yr,10 Yr,30 Yr\n08/19/2026,4.19,4.65,5.19\n", "text/csv"),
        )
        self.assertEqual({row["series_id"] for row in treasury}, {"UST_2Y", "UST_10Y", "UST_30Y"})
        self.assertTrue(all(row["observed_at"] == "2026-08-19" for row in treasury))
        ecb, _ = parse_ecb_fx(
            "ecb_fx",
            response("TIME_PERIOD,OBS_VALUE\n2026-08-19,1.1605\n", "text/csv"),
        )
        self.assertEqual(ecb[0]["unit"], "usd_per_eur")
        vix, _ = parse_cboe_vix(
            "cboe_vix",
            response("DATE,OPEN,HIGH,LOW,CLOSE\n08/19/2026,15.9,16.0,14.7,14.89\n", "text/csv"),
        )
        self.assertEqual(len(vix), 4)

    def test_rss_keeps_minimum_official_fact(self):
        body = """<?xml version="1.0"?><rss><channel><item><title>Policy release</title>
        <link>https://example.test/release</link><guid>release-1</guid>
        <pubDate>Wed, 19 Aug 2026 18:00:00 GMT</pubDate></item></channel></rss>"""
        observations, events = parse_rss("fed_press_rss", response(body, "application/rss+xml"))
        self.assertEqual(observations, [])
        self.assertEqual(events[0]["official_fact"], "Policy release")
        self.assertEqual(events[0]["published_at"], "2026-08-19T18:00:00+00:00")

    def test_schema_change_is_a_parse_failure(self):
        with self.assertRaises(SourceParseError):
            parse_nyfed_rates("nyfed_rates", response('{"unexpected":[]}'))

    def test_dynamic_urls_use_requested_date(self):
        target = date(2026, 8, 20)
        self.assertIn("startDate=2026-07-30", onrrp_url(target, {"lookback_days": 21}))
        self.assertIn("daily-treasury-rates.csv/2026/all", treasury_yield_url(target, {}))


class GlobalMacroClientAndStoreTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = GlobalMacroStore(Path(self.tempdir.name) / "macro.sqlite")
        self.config = copy.deepcopy(load_strategy_config("global-macro.json")[0])
        self.config["http"].update({"maximum_retries": 2, "minimum_interval_seconds": 0, "backoff_seconds": 0})

    def tearDown(self):
        self.tempdir.cleanup()

    def test_client_retries_server_error_and_parses_success(self):
        session = FakeSession([
            response("temporary", "text/plain", 503),
            response('{"refRates":[{"effectiveDate":"2026-08-19","type":"EFFR","percentRate":4.0}]}'),
        ])
        result = GlobalMacroClient(self.config, session=session).fetch("nyfed_rates", date(2026, 8, 20))
        self.assertTrue(result.ok)
        self.assertEqual(len(session.calls), 2)
        self.assertEqual(result.latest_observation_date, "2026-08-19")

    def test_store_is_idempotent_and_health_marks_stale(self):
        item = {
            "source_id": "nyfed_rates", "series_id": "NYFED_SOFR", "observed_at": "2026-08-10",
            "value": 4.1, "unit": "percent", "frequency": "daily",
            "source_url": "https://example.test", "raw": {"value": 4.1},
        }
        result = SourceResult(
            source_id="nyfed_rates", url="https://example.test", ok=True,
            checked_at="2026-08-20T01:00:00+00:00", latency_ms=100, http_status=200,
            latest_observation_date="2026-08-10", observations=[item],
        )
        self.store.save_health(result)
        self.store.save_payload(result)
        self.store.save_payload(result)
        self.assertEqual(self.store.counts()["observations"], 1)
        rows = self.store.health_summary(self.config["sources"], date(2026, 8, 20), 7, 0.98)
        row = next(row for row in rows if row["source_id"] == "nyfed_rates")
        self.assertEqual(row["grade"], "YELLOW")
        self.assertTrue(row["stale"])

    def test_failed_fetch_does_not_overwrite_observations(self):
        success = SourceResult(
            source_id="nyfed_rates", url="https://example.test", ok=True,
            checked_at="2026-08-20T01:00:00+00:00", latency_ms=10,
            observations=[{
                "source_id": "nyfed_rates", "series_id": "NYFED_SOFR", "observed_at": "2026-08-19",
                "value": 4.1, "unit": "percent", "frequency": "daily",
                "source_url": "https://example.test", "raw": {},
            }],
        )
        self.store.save_payload(success)
        failed = SourceResult(
            source_id="nyfed_rates", url="https://example.test", ok=False,
            checked_at="2026-08-20T02:00:00+00:00", latency_ms=0, error="timeout",
        )
        self.assertEqual(self.store.save_payload(failed), (0, 0))
        self.assertEqual(self.store.counts()["observations"], 1)


if __name__ == "__main__":
    unittest.main()
