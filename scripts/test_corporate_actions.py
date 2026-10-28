"""Regression tests for point-in-time corporate-action adjustment."""

import sqlite3
import json
import socket
import tempfile
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import pandas as pd

from scripts.data.corporate_actions import (
    BaoStockVerifier,
    EastmoneyVerifier,
    FALLBACK_SOURCE,
    apply_point_in_time_qfq,
    action_hash,
    event_factor,
    load_actions,
    normalize_tdx_actions,
    reclassify_verifications,
    retry_conflicting_verifications,
    save_verification,
    sync_tdx_actions,
    supplement_verifications,
    verification_status,
)
from scripts.data.market_data_store import create_schema
from scripts.data.market_data_service import MarketDataService
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
    def test_data_service_restores_frame_and_exposes_third_source(self):
        with tempfile.TemporaryDirectory() as temp:
            db_path = Path(temp) / "market.sqlite"
            conn = sqlite3.connect(db_path)
            create_schema(conn)
            action = dividend_action()
            frame = raw_frame().rename(columns={"date": "trade_date"})
            frame["code"] = "603132"
            frame["source"] = "tdx"
            frame["fetched_at"] = "2026-08-21T16:00:00"
            frame.to_sql("daily_bars", conn, if_exists="append", index=False)
            conn.execute("INSERT INTO corporate_actions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                "603132", action["date"], 3.3, 0, 0, 0, "tdx_xdxr", action_hash(action), "before", "before"))
            save_verification(conn, "603132", action, {
                "status": "CONFLICT", "sample_count": 2,
                "max_factor_diff_pct": 2, "max_raw_close_diff_pct": 0,
            }, run_date="2026-08-21")
            conn.close()
            with patch("scripts.data.market_data_service.connect_db", side_effect=lambda: sqlite3.connect(db_path)), \
                 patch("scripts.data.market_data_service.sync_tdx_actions", return_value={"status": "CACHED", "changed": []}) as sync, \
                 patch("scripts.data.market_data_service.BaoStockVerifier") as primary, \
                 patch("scripts.data.market_data_service.EastmoneyVerifier") as fallback:
                fallback.return_value.verify.return_value = {
                    "status": "VERIFIED", "sample_count": 2,
                    "max_factor_diff_pct": 0.01, "max_raw_close_diff_pct": 0,
                }
                frames, states = MarketDataService({"data": {}}).get_daily_bars(
                    [("603132", "金徽酒")], "2026-08-21", 3, price_mode="point_in_time_qfq")
                primary.return_value.verify.assert_not_called()
                fallback.return_value.verify.assert_called_once()
                self.assertIn("603132", frames)
                self.assertEqual(states["603132"]["adjustment_status"], "VERIFIED")
                self.assertEqual(states["603132"]["adjustment_verification_sources"], [FALLBACK_SOURCE])
                self.assertAlmostEqual(frames["603132"].iloc[-2]["close"], 18.27)
                fallback.return_value.close.assert_called_once()
                conn = sqlite3.connect(db_path)
                next_bar = frame.iloc[[-1]].copy()
                next_bar["trade_date"] = "2026-08-24"
                next_bar.to_sql("daily_bars", conn, if_exists="append", index=False)
                conn.close()
                sync.return_value = {"status": "SUCCESS", "changed": []}
                for primary_status in ("CONFLICT", "PENDING"):
                    with self.subTest(primary_status=primary_status):
                        conn = sqlite3.connect(db_path)
                        conn.execute(
                            """UPDATE adjustment_verifications SET status=?,sample_count=0
                               WHERE verification_source='baostock_qfq'""", (primary_status,))
                        conn.commit()
                        conn.close()
                        frames, states = MarketDataService({"data": {}}).get_daily_bars(
                            [("603132", "金徽酒")], "2026-08-24", 3, price_mode="point_in_time_qfq")
                        self.assertIn("603132", frames)
                        self.assertEqual(states["603132"]["adjustment_status"], "VERIFIED")
                        primary.return_value.verify.assert_not_called()
                        self.assertEqual(fallback.return_value.verify.call_count, 1)

    def test_changed_event_or_future_fallback_does_not_skip_conflict_retry(self):
        for changed_event in (False, True):
            with self.subTest(changed_event=changed_event):
                conn, action = self.conflict_fixture()
                save_verification(conn, "603132", action, {
                    "status": "VERIFIED", "sample_count": 3,
                }, run_date="2026-08-21" if changed_event else "2026-08-25",
                    verification_source=FALLBACK_SOURCE)
                current = {**action, "cash_dividend_per_10": 4.0} if changed_event else action
                primary = Mock()
                primary.verify.return_value = {"status": "VERIFIED", "sample_count": 3}
                retry_conflicting_verifications(conn, "603132", [current], raw_frame(), "2026-08-24", primary, {})
                primary.verify.assert_called_once()

    def test_disabled_fallback_and_historical_primary_do_not_fetch(self):
        conn, action = self.conflict_fixture()
        verifier = Mock()
        supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-24", verifier,
                                 {"fallback_verification_source": None})
        supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-20", verifier, {})
        verifier.verify.assert_not_called()

    def test_eastmoney_normalized_comparison(self):
        verifier = EastmoneyVerifier()
        self.addCleanup(verifier.close)
        with patch.object(verifier, "_query", side_effect=[
            {"2026-08-20": 18.60, "2026-08-21": 18.94},
            {"2026-08-20": 18.27, "2026-08-21": 18.94},
        ]):
            result = verifier.verify("603132", dividend_action(), raw_frame(), "2026-08-21", 1, 0.15)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertIn("eastmoney.com", result["detail"]["endpoint"])

    def test_third_source_only_fetches_recorded_primary_failures(self):
        conn, action = self.conflict_fixture()
        verifier = Mock()
        verifier.verify.return_value = {
            "status": "VERIFIED", "sample_count": 3,
            "max_factor_diff_pct": 0.02, "max_raw_close_diff_pct": 0.0,
        }
        supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-24", verifier, {})
        self.assertEqual(verification_status(conn, "603132", [action]), "VERIFIED")
        self.assertEqual(conn.execute("SELECT status FROM adjustment_verifications WHERE verification_source='baostock_qfq'").fetchone()[0], "CONFLICT")
        for run_date in ("2026-08-24", "2026-08-25"):
            supplement_verifications(conn, "603132", [action], raw_frame(), run_date, verifier, {})
        self.assertEqual(verifier.verify.call_count, 1)
        self.assertEqual(verification_status(conn, "603132", [action], "2026-08-21"), "CONFLICT")
        supplement_verifications(conn, "000001", [action], raw_frame(), "2026-08-24", verifier, {})
        conn.execute("UPDATE adjustment_verifications SET status='VERIFIED' WHERE verification_source='baostock_qfq'")
        supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-26", verifier, {})
        self.assertEqual(verifier.verify.call_count, 1)

    def test_third_source_errors_are_cached_and_keep_conflict_gate(self):
        conn, action = self.conflict_fixture()
        verifier = Mock()
        verifier.verify.side_effect = TimeoutError("third source timeout")
        for run_date in ("2026-08-24", "2026-08-24", "2026-08-25"):
            supplement_verifications(conn, "603132", [action], raw_frame(), run_date, verifier, {})
        self.assertEqual(verifier.verify.call_count, 2)
        self.assertEqual(verification_status(conn, "603132", [action]), "CONFLICT")

    def test_pending_primary_can_be_verified_or_blocked_by_third_source(self):
        for fallback_status in ("VERIFIED", "CONFLICT"):
            with self.subTest(fallback_status=fallback_status):
                conn, action = self.conflict_fixture()
                conn.execute("UPDATE adjustment_verifications SET status='PENDING',sample_count=0")
                verifier = Mock()
                verifier.verify.return_value = {"status": fallback_status, "sample_count": 3,
                                                "max_factor_diff_pct": 0.1 if fallback_status == "VERIFIED" else 2.0}
                supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-24", verifier, {})
                self.assertEqual(verification_status(conn, "603132", [action]), fallback_status)
                if fallback_status == "CONFLICT":
                    verifier.verify.side_effect = TimeoutError("offline")
                    supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-25", verifier, {})
                    self.assertEqual(verification_status(conn, "603132", [action]), "CONFLICT")

    def test_changed_action_invalidates_third_source_success(self):
        conn, action = self.conflict_fixture()
        verifier = Mock()
        verifier.verify.return_value = {"status": "VERIFIED", "sample_count": 3}
        supplement_verifications(conn, "603132", [action], raw_frame(), "2026-08-24", verifier, {})
        changed = {**action, "cash_dividend_per_10": 4.0}
        self.assertEqual(verification_status(conn, "603132", [changed]), "CONFLICT")
        supplement_verifications(conn, "603132", [changed], raw_frame(), "2026-08-24", verifier, {})
        self.assertEqual(verifier.verify.call_count, 2)

    def test_eastmoney_adapter_checks_security_prices_and_sample_coverage(self):
        verifier = EastmoneyVerifier()
        self.addCleanup(verifier.close)
        payload = {"rc": 0, "data": {"code": "603132", "klines": ["2026-08-20,18,18.6,19,17,10"]}}
        response = Mock()
        response.json.return_value = payload
        verifier.session = Mock()
        verifier.session.get.return_value = response
        self.assertEqual(verifier._query(None, "603132", "2026-08-20", "2026-08-21", "3"), {"2026-08-20": 18.6})
        self.assertEqual(verifier.session.get.call_args.kwargs["timeout"], 10)
        payload["data"]["code"] = "000001"
        with self.assertRaisesRegex(RuntimeError, "证券不符"):
            verifier._query(None, "603132", "2026-08-20", "2026-08-21", "3")
        payload["data"]["code"] = "603132"
        payload["data"]["klines"] = ["2026-08-20,18,nan,19,17,10"]
        with self.assertRaisesRegex(RuntimeError, "价格无效"):
            verifier._query(None, "603132", "2026-08-20", "2026-08-21", "3")
        verifier._query = Mock(side_effect=[
            {"2026-08-19": 18.64, "2026-08-21": 18.94},
            {"2026-08-19": 18.31, "2026-08-21": 18.94},
        ])
        with self.assertRaisesRegex(RuntimeError, "本地应有"):
            verifier.verify("603132", dividend_action(), raw_frame(), "2026-08-21", 1, 0.15)

    def conflict_fixture(self):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        create_schema(conn)
        action = dividend_action()
        save_verification(conn, "603132", action, {
            "status": "CONFLICT", "sample_count": 2,
            "max_factor_diff_pct": 2.0, "max_raw_close_diff_pct": 0.0,
            "detail": {"samples": ["original evidence"]},
        }, run_date="2026-08-21")
        return conn, action

    def test_conflict_recovers_next_trading_day_without_action_change(self):
        conn, action = self.conflict_fixture()
        verifier = Mock()
        verifier.verify.return_value = {
            "status": "VERIFIED", "sample_count": 3,
            "max_factor_diff_pct": 0.01, "max_raw_close_diff_pct": 0.0,
        }
        for run_date in ("2026-08-20", "2026-08-21"):
            retry_conflicting_verifications(conn, "603132", [action], raw_frame(), run_date, verifier, {})
        verifier.verify.assert_not_called()
        retry_conflicting_verifications(conn, "603132", [action], raw_frame(), "2026-08-24", verifier, {})
        self.assertEqual(verification_status(conn, "603132", [action]), "VERIFIED")
        retry_conflicting_verifications(conn, "603132", [action], raw_frame(), "2026-08-25", verifier, {})
        self.assertEqual(verifier.verify.call_count, 1)

    def test_conflict_timeout_preserves_evidence_and_retries_next_date(self):
        conn, action = self.conflict_fixture()
        verifier = Mock()
        verifier.verify.side_effect = TimeoutError("provider delayed")
        for run_date in ("2026-08-24", "2026-08-24", "2026-08-25"):
            retry_conflicting_verifications(conn, "603132", [action], raw_frame(), run_date, verifier, {})
        self.assertEqual(verifier.verify.call_count, 2)
        self.assertEqual(verification_status(conn, "603132", [action]), "CONFLICT")
        row = conn.execute("SELECT max_factor_diff_pct,detail_json FROM adjustment_verifications").fetchone()
        self.assertEqual(row[0], 2.0)
        detail = json.loads(row[1])
        self.assertEqual(detail["samples"], ["original evidence"])
        self.assertEqual(detail["conflict_retry_run_date"], "2026-08-25")
        self.assertIn("provider delayed", detail["conflict_retry_error"])

    def test_persistent_conflicts_all_rechecked_once_per_date(self):
        conn, action = self.conflict_fixture()
        other = dividend_action("2026-08-20")
        result = {"status": "CONFLICT", "sample_count": 2,
                  "max_factor_diff_pct": 2.0, "max_raw_close_diff_pct": 0.0}
        save_verification(conn, "603132", other, result, run_date="2026-08-21")
        verifier = Mock()
        verifier.verify.return_value = result
        for run_date in ("2026-08-24", "2026-08-24", "2026-08-25"):
            retry_conflicting_verifications(conn, "603132", [action, other], raw_frame(), run_date, verifier, {})
        self.assertEqual(verifier.verify.call_count, 4)
        self.assertEqual(verification_status(conn, "603132", [action, other]), "CONFLICT")

    def test_legacy_conflict_uses_verification_date_and_skips_unused_events(self):
        conn, action = self.conflict_fixture()
        conn.execute("UPDATE adjustment_verifications SET detail_json='{}',verified_at='2026-08-21T16:00:00'")
        verifier = Mock()
        verifier.verify.return_value = {"status": "VERIFIED", "sample_count": 3}
        retry_conflicting_verifications(conn, "603132", [action], raw_frame(), "2026-08-21", verifier, {})
        retry_conflicting_verifications(conn, "603132", [], raw_frame(), "2026-08-24", verifier, {})
        verifier.verify.assert_not_called()
        retry_conflicting_verifications(conn, "603132", [action], raw_frame(), "2026-08-24", verifier, {})
        verifier.verify.assert_called_once()

    def test_baostock_login_timeout_is_bounded_and_not_retried(self):
        verifier = BaoStockVerifier()
        original_timeout = socket.getdefaulttimeout()
        observed = []

        def timed_out_login():
            observed.append(socket.getdefaulttimeout())
            raise TimeoutError("login recv timed out")

        with patch("baostock.login", side_effect=timed_out_login) as login:
            with self.assertRaises(TimeoutError):
                verifier._login()
            with self.assertRaisesRegex(RuntimeError, "本轮已失败"):
                verifier._login()

        self.assertEqual(observed, [10])
        self.assertEqual(login.call_count, 1)
        self.assertEqual(socket.getdefaulttimeout(), original_timeout)

    def test_tdx_source_uses_cached_server_adapter_when_live_probe_is_empty(self):
        expected = object()
        with patch("scripts.data.tdx_block_data.connect_tdx", return_value=expected) as connect:
            source = TDXBlockSource()

            self.assertIs(source._client(), expected)
            self.assertIs(source._client(), expected)
            connect.assert_called_once_with("market", 10)

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
