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


if __name__ == "__main__":
    unittest.main()
