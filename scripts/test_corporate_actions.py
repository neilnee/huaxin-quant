"""Regression tests for point-in-time corporate-action adjustment."""

import sqlite3
import unittest
from unittest.mock import patch

import pandas as pd

from scripts.data.corporate_actions import (
    BaoStockVerifier,
    apply_point_in_time_qfq,
    event_factor,
    load_actions,
    normalize_tdx_actions,
    reclassify_verifications,
    sync_tdx_actions,
)
from scripts.data.market_data_store import create_schema
from scripts.data.tdx_block_data import TDXBlockSource


def raw_frame():
    return pd.DataFrame({
        "date": ["2026-08-18", "2026-08-19", "2026-08-20", "2026-08-21"],
        "open": [18.86, 18.98, 18.87, 18.30],
        "high": [19.53, 19.07, 19.24, 18.97],
        "low": [18.85, 18.47, 18.54, 18.04],
        "close": [19.16, 18.64, 18.60, 18.94],
        "volume": [6472947, 6394900, 6058682, 6727538],
    })


def dividend_action(ex_date="2026-08-21"):
    return {
        "date": ex_date, "cash_dividend_per_10": 3.3,
        "bonus_shares_per_10": 0.0, "rights_shares_per_10": 0.0,
        "rights_price": 0.0,
    }


class FakeTDXSource:
    def __init__(self, frame):
        self.frame = frame
        self.calls = 0

    def fetch_corporate_actions(self, code):
        self.calls += 1
        return self.frame


class CorporateActionTests(unittest.TestCase):
    def test_tdx_source_uses_cached_server_adapter_when_live_probe_is_empty(self):
        expected = object()
        with patch("scripts.data.tdx_block_data.probe_servers", return_value=[]), patch(
            "scripts.data.market_data.TDXSource._get_client", return_value=expected
        ):
            source = TDXBlockSource()

            self.assertIs(source._client(), expected)
            self.assertIs(source._client(), expected)

    def test_tdx_normalization_excludes_future_and_non_action_rows(self):
        frame = pd.DataFrame([
            {"year": 2026, "month": 8, "day": 21, "category": 1, "fenhong": 3.3,
             "songzhuangu": 0, "peigu": 0, "peigujia": 0},
            {"year": 2026, "month": 8, "day": 22, "category": 1, "fenhong": 1,
             "songzhuangu": 0, "peigu": 0, "peigujia": 0},
            {"year": 2026, "month": 8, "day": 20, "category": 5},
        ])

        actions = normalize_tdx_actions(frame, "2026-08-21")

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0]["date"], "2026-08-21")
        self.assertEqual(actions[0]["cash_dividend_per_10"], 3.3)

    def test_jinhui_cash_dividend_matches_baostock_qfq(self):
        factor = event_factor(18.60, dividend_action())
        adjusted, applied = apply_point_in_time_qfq(raw_frame(), [dividend_action()], "2026-08-21")

        self.assertAlmostEqual(factor, 18.27 / 18.60, places=10)
        self.assertAlmostEqual(adjusted.loc[2, "close"], 18.27, places=8)
        self.assertAlmostEqual(adjusted.loc[0, "close"], 18.820064516, places=8)
        self.assertEqual(adjusted.loc[3, "close"], 18.94)
        self.assertEqual(applied[0]["previous_trade_date"], "2026-08-20")

    def test_combined_dividend_bonus_and_rights_factor(self):
        action = {
            "date": "2026-06-01", "cash_dividend_per_10": 2,
            "bonus_shares_per_10": 3, "rights_shares_per_10": 2,
            "rights_price": 4,
        }

        factor = event_factor(10.0, action)

        self.assertAlmostEqual(factor, (10.0 - 0.2 + 0.2 * 4) / (10.0 * 1.5))

    def test_adjustment_does_not_mutate_raw_frame_or_volume(self):
        original = raw_frame()
        before = original.copy(deep=True)

        adjusted, _ = apply_point_in_time_qfq(original, [dividend_action()], "2026-08-21")

        pd.testing.assert_frame_equal(original, before)
        pd.testing.assert_series_equal(adjusted["volume"], before["volume"])

    def test_future_action_does_not_enter_historical_replay(self):
        adjusted, applied = apply_point_in_time_qfq(
            raw_frame().iloc[:3], [dividend_action()], "2026-08-20"
        )

        self.assertEqual(applied, [])
        self.assertEqual(adjusted.iloc[-1]["close"], 18.60)

    def test_sync_is_idempotent_for_same_as_of_date(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        frame = pd.DataFrame([{
            "year": 2026, "month": 8, "day": 21, "category": 1, "fenhong": 3.3,
            "songzhuangu": 0, "peigu": 0, "peigujia": 0,
        }])
        source = FakeTDXSource(frame)

        first = sync_tdx_actions(conn, source, "603132", "2026-08-21")
        second = sync_tdx_actions(conn, source, "603132", "2026-08-21")

        self.assertEqual(first["status"], "SUCCESS")
        self.assertEqual(len(first["changed"]), 1)
        self.assertEqual(second["status"], "CACHED")
        self.assertEqual(source.calls, 1)
        self.assertEqual(len(load_actions(conn, "603132", "2026-08-21")), 1)
        conn.close()

    def test_schema_upgrade_preserves_existing_raw_bar(self):
        conn = sqlite3.connect(":memory:")
        conn.execute(
            """CREATE TABLE daily_bars(
                   code TEXT NOT NULL,trade_date TEXT NOT NULL,open REAL NOT NULL,high REAL NOT NULL,
                   low REAL NOT NULL,close REAL NOT NULL,volume REAL NOT NULL,amount REAL,
                   source TEXT NOT NULL,fetched_at TEXT NOT NULL,PRIMARY KEY(code,trade_date))"""
        )
        raw = ("603132", "2026-08-20", 18.87, 19.24, 18.54, 18.60, 60586, 113000000, "tdx", "before")
        conn.execute("INSERT INTO daily_bars VALUES(?,?,?,?,?,?,?,?,?,?)", raw)

        create_schema(conn)

        self.assertEqual(tuple(conn.execute("SELECT * FROM daily_bars").fetchone()), raw)
        self.assertEqual(conn.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0], "3")
        conn.close()

    def test_baostock_verification_normalizes_provider_anchor(self):
        verifier = BaoStockVerifier()
        verifier.client = object()
        raw = {"2026-08-20": 18.60, "2026-08-21": 18.94}
        qfq = {"2026-08-20": 18.2699988, "2026-08-21": 18.94}
        calls = iter((raw, qfq))
        verifier._query = lambda *args: next(calls)

        result = verifier.verify(
            "603132", dividend_action(), raw_frame(), "2026-08-21",
            max_factor_diff_pct=0.15, max_raw_close_diff_pct=0.15,
        )

        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["sample_count"], 2)

    def test_verification_can_be_reclassified_when_tolerance_changes(self):
        conn = sqlite3.connect(":memory:")
        create_schema(conn)
        conn.execute(
            """INSERT INTO adjustment_verifications VALUES(
                   '300976','2026-06-01','tdx_xdxr','baostock_qfq','CONFLICT',3,
                   0.3307,0.0,'{}','2026-08-22T12:00:00')"""
        )

        reclassify_verifications(conn, "300976", 1.0, 0.15)

        self.assertEqual(conn.execute(
            "SELECT status FROM adjustment_verifications WHERE code='300976'"
        ).fetchone()[0], "VERIFIED")
        conn.close()


if __name__ == "__main__":
    unittest.main()
