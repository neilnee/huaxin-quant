"""External factor arbitration, cached evidence, and actual price adjustment."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import pandas as pd

from scripts import daily, daily_ai_report, quant_filter
from scripts.data.corporate_actions import (
    CONSENSUS_SOURCE, FALLBACK_SOURCE, VERIFICATION_SOURCE, action_hash,
    apply_point_in_time_qfq, event_factor, external_factor_consensus,
    retry_conflicting_verifications, save_verification, supplement_verifications,
    verification_status,
)
from scripts.data.market_data_service import MarketDataService
from scripts.data.market_data_store import create_schema


class ExternalFactorConsensusTests(unittest.TestCase):
    def fixture(self, fallback_factor=0.6761053154495777):
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        create_schema(conn)
        action = {"date": "2026-04-28", "cash_dividend_per_10": 6.0,
                  "bonus_shares_per_10": 4.599999904632568,
                  "rights_shares_per_10": 0.0, "rights_price": 0.0}
        frame = pd.DataFrame({"date": ["2026-04-27", "2026-04-28", "2026-04-29"],
                              "close": [20.13, 13.5, 13.87], "volume": [100, 200, 300],
                              "amount": [2013, 2700, 4161]})
        for field in ("open", "high", "low"):
            frame[field] = frame["close"]
        expected = event_factor(20.13, action)
        for source, factor in [(VERIFICATION_SOURCE, 0.676105), (FALLBACK_SOURCE, fallback_factor)]:
            samples = [{"date": d, "normalized_factor": factor if i == 0 else 1.0,
                        "expected_factor": expected if i == 0 else 1.0,
                        "factor_diff_pct": abs(factor / expected - 1) * 100 if i == 0 else 0,
                        "raw_close_diff_pct": 0.0}
                       for i, d in enumerate(frame["date"])]
            save_verification(conn, "300501", action, {
                "status": "CONFLICT", "sample_count": 3,
                "max_factor_diff_pct": abs(factor / expected - 1) * 100,
                "max_raw_close_diff_pct": 0.0,
                "detail": {"samples": samples, "reference_date": "2026-04-28"},
            }, run_date="2026-10-30", verification_source=source)
        return conn, action, frame

    def test_haishun_consensus_changes_prices_and_keeps_original_evidence(self):
        conn, action, frame = self.fixture()
        before = conn.execute("SELECT * FROM adjustment_verifications ORDER BY verification_source").fetchall()
        resolution = external_factor_consensus(conn, "300501", action, "2026-10-30", frame)
        self.assertEqual(resolution["adopted_factor"], 0.676105)
        self.assertLess(resolution["external_factor_diff_pct"], 0.0001)
        self.assertEqual(verification_status(conn, "300501", [action], "2026-10-30", frame), "VERIFIED")
        original = frame.copy(deep=True)
        adjusted, applied = apply_point_in_time_qfq(frame, [action], "2026-10-30",
                                                   {action["date"]: resolution["adopted_factor"]})
        self.assertAlmostEqual(adjusted.iloc[0]["close"], 13.60999365)
        self.assertEqual(adjusted.iloc[1]["close"], 13.5)
        self.assertEqual(applied[0]["factor"], 0.676105)
        pd.testing.assert_frame_equal(frame, original)
        self.assertEqual(action["cash_dividend_per_10"], 6)
        self.assertEqual(before, conn.execute("SELECT * FROM adjustment_verifications ORDER BY verification_source").fetchall())

    def test_external_disagreement_stays_blocked(self):
        conn, action, frame = self.fixture(0.69)
        self.assertIsNone(external_factor_consensus(conn, "300501", action, "2026-10-30", frame))
        self.assertEqual(verification_status(conn, "300501", [action], "2026-10-30", frame), "CONFLICT")

    def test_consensus_tolerance_is_separate_from_local_factor_tolerance(self):
        conn, action, frame = self.fixture(0.677)
        self.assertIsNone(external_factor_consensus(conn, "300501", action, "2026-10-30", frame))
        self.assertIsNotNone(external_factor_consensus(conn, "300501", action, "2026-10-30", frame,
                                                       {"max_external_factor_diff_pct": 0.2}))

    def test_invalid_or_stale_evidence_never_resolves(self):
        mutations = [
            ("status", "PENDING"), ("status", "VERIFIED"), ("sample_count", 2),
            ("max_raw_close_diff_pct", 0.2), ("max_raw_close_diff_pct", None),
            ("max_factor_diff_pct", 0.5), ("action_hash", "changed"),
            ("run_date", "2026-11-02"), ("reference_date", "2026-04-29"),
            ("normalized_factor", float("nan")), ("normalized_factor", 0),
            ("expected_factor", 0.6), ("sample_raw_error", 0.2),
            ("sample_date", "2026-04-24"), ("missing_sample", None),
            ("post_factor", 0.99), ("local_raw_close", 20.14),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value):
                conn, action, frame = self.fixture()
                if field in {"status", "sample_count", "max_raw_close_diff_pct", "max_factor_diff_pct"}:
                    conn.execute(f"UPDATE adjustment_verifications SET {field}=? WHERE verification_source=?",
                                 (value, FALLBACK_SOURCE))
                else:
                    detail = json.loads(conn.execute("SELECT detail_json FROM adjustment_verifications WHERE verification_source=?",
                                                     (FALLBACK_SOURCE,)).fetchone()[0])
                    if field in {"action_hash", "run_date", "reference_date"}:
                        detail[field] = value
                    elif field == "missing_sample":
                        detail["samples"].pop()
                    elif field == "sample_date":
                        detail["samples"][0]["date"] = value
                    elif field == "post_factor":
                        detail["samples"][2]["normalized_factor"] = value
                    else:
                        detail["samples"][0]["raw_close_diff_pct" if field == "sample_raw_error" else field] = value
                    conn.execute("UPDATE adjustment_verifications SET detail_json=? WHERE verification_source=?",
                                 (json.dumps(detail), FALLBACK_SOURCE))
                self.assertIsNone(external_factor_consensus(conn, "300501", action, "2026-10-30", frame))

    def test_current_raw_prices_and_action_changes_invalidate_consensus(self):
        conn, action, frame = self.fixture()
        changed = {**action, "cash_dividend_per_10": 5.7}
        self.assertIsNone(external_factor_consensus(conn, "300501", changed, "2026-10-30", frame))
        frame.loc[0, "close"] = 20.14
        self.assertIsNone(external_factor_consensus(conn, "300501", action, "2026-10-30", frame))

    def test_missing_source_and_future_primary_are_not_consensus(self):
        for source in (VERIFICATION_SOURCE, FALLBACK_SOURCE):
            with self.subTest(source=source):
                conn, action, frame = self.fixture()
                conn.execute("DELETE FROM adjustment_verifications WHERE verification_source=?", (source,))
                self.assertIsNone(external_factor_consensus(conn, "300501", action, "2026-10-30", frame))
        conn, action, frame = self.fixture()
        self.assertIsNone(external_factor_consensus(conn, "300501", action, "2026-10-29", frame))

    def test_reused_consensus_skips_both_network_retries(self):
        conn, action, frame = self.fixture()
        primary, fallback = Mock(), Mock()
        retry_conflicting_verifications(conn, "300501", [action], frame, "2026-11-02", primary, {})
        supplement_verifications(conn, "300501", [action], frame, "2026-11-02", fallback, {})
        primary.verify.assert_not_called()
        fallback.verify.assert_not_called()

    def test_multi_event_adjustment_restarts_from_raw_and_preserves_volume(self):
        _, action, frame = self.fixture()
        other = {**action, "date": "2026-04-29", "bonus_shares_per_10": 0, "cash_dividend_per_10": 1}
        adjusted, _ = apply_point_in_time_qfq(frame, [action, other], "2026-10-30", {action["date"]: 0.676105})
        self.assertAlmostEqual(adjusted.iloc[0]["close"], 20.13 * 0.676105 * event_factor(13.5, other))
        self.assertAlmostEqual(adjusted.iloc[1]["close"], 13.4)
        pd.testing.assert_series_equal(adjusted["volume"], frame["volume"])

    def test_data_service_applies_consensus_and_reports_disagreement(self):
        for factor, accepted in [(0.6761053154495777, True), (0.69, False)]:
            with self.subTest(accepted=accepted), tempfile.TemporaryDirectory() as tmp:
                conn, action, frame = self.fixture(factor)
                path = Path(tmp) / "market.sqlite"
                target = sqlite3.connect(path)
                conn.commit()
                conn.backup(target)
                bars = frame.rename(columns={"date": "trade_date"}).copy()
                bars["code"], bars["source"], bars["fetched_at"] = "300501", "tdx", "2026-10-30"
                bars.to_sql("daily_bars", target, if_exists="append", index=False)
                target.execute("INSERT INTO corporate_actions VALUES(?,?,?,?,?,?,?,?,?,?)", (
                    "300501", action["date"], 6, action["bonus_shares_per_10"], 0, 0,
                    "tdx_xdxr", action_hash(action), "before", "before"))
                target.commit()
                target.close()
                # A later run requires a valid target-day bar but uses the same event samples.
                target = sqlite3.connect(path)
                last = bars.iloc[[-1]].copy()
                last["trade_date"] = "2026-10-30"
                last.to_sql("daily_bars", target, if_exists="append", index=False)
                target.close()
                with patch("scripts.data.market_data_service.connect_db", side_effect=lambda: sqlite3.connect(path)), \
                     patch("scripts.data.market_data_service.sync_tdx_actions", return_value={"status": "CACHED", "changed": []}), \
                     patch("scripts.data.market_data_service.BaoStockVerifier") as primary, \
                     patch("scripts.data.market_data_service.EastmoneyVerifier") as fallback:
                    frames, states = MarketDataService({"data": {}}).get_daily_bars(
                        [("300501", "海顺新材")], "2026-10-30", 3, price_mode="point_in_time_qfq")
                state = states["300501"]
                self.assertEqual("300501" in frames, accepted)
                self.assertEqual(state["adjustment_status"], "VERIFIED" if accepted else "CONFLICT")
                self.assertEqual(state["adjustment_warnings"][0]["kind"],
                                 "external_consensus" if accepted else "unresolved_conflict")
                if accepted:
                    self.assertIn(CONSENSUS_SOURCE, state["adjustment_verification_sources"])
                    self.assertAlmostEqual(frames["300501"].iloc[0]["close"], 13.60999365)
                primary.return_value.verify.assert_not_called()
                fallback.return_value.verify.assert_not_called()

    def test_quant_keeps_warning_for_stock_removed_by_conflict(self):
        state = {"source": "adjustment_conflict", "error": "复权冲突", "retryable": False,
                 "adjustment_status": "CONFLICT", "adjustment_warnings": [
                     {"kind": "unresolved_conflict", "ex_date": "2026-04-28", "reason": "已拦截"}]}
        with patch.object(quant_filter, "MarketDataService") as service:
            service.return_value.get_daily_bars.return_value = ({}, {"300501": state})
            results, stats = quant_filter.process_codes([("300501", "海顺新材")], "261030", "2026-10-30")
        self.assertEqual(results, [])
        self.assertEqual(stats["adjustment_conflict"], 1)
        self.assertEqual(stats["adjustment_warnings"][0]["code"], "300501")

    def test_daily_loads_same_day_warnings_and_prints_final_notice(self):
        warning = {"code": "300501", "name": "海顺新材", "ex_date": "2026-04-28", "reason": "采用外部因子"}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            folder = root / "cache" / "quant_runs"
            folder.mkdir(parents=True)
            path = folder / "quant_261030.json"
            payload = {"meta": {"run_date": "2026-10-30"}, "stats": {"adjustment_warnings": [warning]}}
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(daily, "PROJECT_ROOT", str(root)), patch("builtins.print") as output:
                warnings = daily.load_quant_adjustment_warnings("261030")
                daily.print_quant_adjustment_warnings(warnings)
            self.assertIn("300501 海顺新材 2026-04-28: 采用外部因子", output.call_args_list[-1].args[0])
            payload["meta"]["run_date"] = "2026-10-29"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(daily, "PROJECT_ROOT", str(root)), self.assertRaisesRegex(ValueError, "日期不一致"):
                daily.load_quant_adjustment_warnings("261030")

    def test_ai_packet_preserves_resolved_adjustment_notice(self):
        warning = {"code": "300501", "kind": "external_consensus", "reason": "采用外部因子"}
        contexts = {"market": {"meta": {"data_status": "VALID"}}, "capital": {}, "signals": {},
                    "vcp": {"summary": {"quant_stats": {"pull_fail": 0, "adjustment_warnings": [warning]}}}}
        quality = daily_ai_report.build_data_quality(contexts, {})
        self.assertEqual(quality["status"], "complete_with_warnings")
        self.assertIn({"module": "quant_adjustment", "detail": warning}, quality["warnings"])


if __name__ == "__main__":
    unittest.main()
