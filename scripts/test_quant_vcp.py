"""Regression tests for Model 2 close-based VCP contraction detection."""
import sys
import unittest
from pathlib import Path

import pandas as pd


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts import quant_filter as quant


def make_frame(closes, highs=None, lows=None):
    highs = highs or [close + 1 for close in closes]
    lows = lows or [close - 1 for close in closes]
    return pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=len(closes), freq="B").strftime("%Y-%m-%d"),
            "open": closes,
            "high": highs,
            "low": lows,
            "close": closes,
            "volume": [100] * len(closes),
        }
    )


class CloseBasedContractionTests(unittest.TestCase):
    def test_contraction_uses_close_swings_and_keeps_intraday_audit_range(self):
        closes = [90, 94, 100, 104, 106, 103, 100, 97, 95, 96, 98, 101, 103]
        highs = [91, 95, 101, 105, 120, 104, 101, 98, 96, 97, 99, 102, 104]
        lows = [89, 93, 99, 103, 105, 102, 99, 96, 88, 95, 97, 100, 102]
        contractions = quant.detect_contractions(make_frame(closes, highs, lows))
        self.assertEqual(len(contractions), 1)
        contraction = contractions[0]
        self.assertEqual(contraction["start_close"], 106.0)
        self.assertEqual(contraction["end_close"], 95.0)
        self.assertAlmostEqual(contraction["close_pullback_pct"], -10.38, places=2)
        self.assertEqual(contraction["pullback_pct"], contraction["close_pullback_pct"])
        self.assertEqual(contraction["intraday_high"], 120.0)
        self.assertEqual(contraction["intraday_low"], 88.0)
        self.assertTrue(quant.has_intraday_close_divergence(contraction))

    def test_intraday_wick_without_a_close_swing_is_not_a_contraction(self):
        closes = [100, 101, 100, 101, 100, 101, 100, 101, 100]
        highs = [101, 102, 103, 125, 102, 103, 102, 103, 101]
        lows = [99, 100, 99, 100, 76, 100, 99, 100, 99]
        self.assertEqual(quant.detect_contractions(make_frame(closes, highs, lows)), [])

    def test_every_detected_contraction_is_a_real_close_decline(self):
        closes = [90, 94, 100, 106, 103, 99, 95, 97, 101, 104, 102, 99, 96, 98, 101]
        contractions = quant.detect_contractions(make_frame(closes))
        self.assertTrue(contractions)
        for contraction in contractions:
            self.assertLess(contraction["end_close"], contraction["start_close"])
            self.assertLess(contraction["close_pullback_pct"], 0)

    def test_minimum_close_pullback_threshold_is_enforced(self):
        closes = [90, 94, 98, 100, 99, 98, 97, 96.1, 97, 98, 99]
        self.assertEqual(quant.detect_contractions(make_frame(closes)), [])

    def test_contraction_ordering_and_reset_use_close_measure(self):
        contractions = [
            {"close_pullback_pct": -12.0, "pullback_pct": -30.0},
            {"close_pullback_pct": -9.0, "pullback_pct": -4.0},
        ]
        self.assertTrue(quant.contraction_decrease_status(contractions)["is_strict"])
        self.assertFalse(quant.contraction_group_has_reset_expansion(contractions))
        expanded = [
            {"close_pullback_pct": -5.0, "pullback_pct": -5.0},
            {"close_pullback_pct": -13.0, "pullback_pct": -5.0},
        ]
        self.assertTrue(quant.contraction_group_has_reset_expansion(expanded))

    def test_time_gap_splits_independent_vcp_clusters(self):
        contractions = [
            {"start_idx": 10, "end_idx": 15},
            {"start_idx": 42, "end_idx": 46},
            {"start_idx": 50, "end_idx": 54},
        ]
        clusters = quant.split_contraction_clusters(contractions)
        self.assertEqual([len(cluster) for cluster in clusters], [1, 2])
        self.assertEqual(quant.contraction_group_span_days(clusters[-1]), 13)

    def test_retest_selling_uses_board_specific_drop_threshold(self):
        df = make_frame([100, 101, 102, 101, 100, 99, 98, 82])
        df["open"] = [100, 101, 102, 101, 100, 99, 98, 100]
        df["volume"] = [100, 100, 100, 100, 100, 100, 100, 145]
        df["ATR14_pct"] = 3.0
        breakout = {"volume": 150.0}
        cfg = quant.SETUP_CFG["retest_buy"]["failed_retest_selling"]

        blocked, details = quant.detect_failed_retest_selling(df, breakout, "688392", cfg)

        self.assertTrue(blocked)
        self.assertEqual(details["drop_threshold_pct"], 10.0)
        self.assertEqual(quant.failed_retest_drop_threshold("600000", df.iloc[-1], cfg), 7.0)
        high_atr = df.iloc[-1].copy()
        high_atr["ATR14_pct"] = 12.0
        self.assertEqual(quant.failed_retest_drop_threshold("688392", high_atr, cfg), 15.0)

    def test_mixed_structure_volume_caps_retest_quality_at_b(self):
        result = quant.base_setup_result(True, "test", score=85, quality_cap="B")
        self.assertEqual(result["setup_quality"], "B")

    def test_failed_structure_volume_blocks_retest_before_buy_point_scoring(self):
        df = make_frame([100.0] * 70)
        df.loc[60, ["open", "high", "low", "close", "volume"]] = [100.0, 106.0, 100.0, 105.0, 200.0]
        for idx in range(61, 70):
            df.loc[idx, ["open", "high", "low", "close", "volume"]] = [102.0, 103.0, 100.0, 102.0, 80.0]
        df = quant.calc_indicators(df)
        structure = {"structure_valid": True, "state": "VCP_MATURE", "volume_pattern": "failed"}
        result = quant.detect_retest_buy(
            df, structure, {"risk_flags": [], "risk_score": 0, "hard_reject": False}, code="600000"
        )
        self.assertTrue(result["hard_block"])
        self.assertEqual(result["structure_volume_alignment"], "BLOCKED")


if __name__ == "__main__":
    unittest.main()
