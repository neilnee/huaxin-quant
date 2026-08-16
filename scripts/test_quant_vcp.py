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

    def test_failed_breakout_reset_requires_a_cleaner_confirming_contraction(self):
        df = make_frame([99, 100, 98, 102, 95, 90, 80, 85, 90, 95, 100, 92, 85, 88, 90, 92])
        old = {
            "start_idx": 0, "end_idx": 2, "start_date": "2026-01-01", "end_date": "2026-01-05",
            "high_price": 100.0, "low_price": 95.0, "close_pullback_pct": -10.0,
            "avg_volume": 100.0,
        }
        reset = {
            "start_idx": 3, "end_idx": 6, "start_date": "2026-01-06", "end_date": "2026-01-09",
            "high_price": 105.0, "low_price": 80.0, "close_pullback_pct": -20.0,
            "avg_volume": 200.0,
        }
        confirming = {
            "start_idx": 10, "end_idx": 12, "start_date": "2026-01-15", "end_date": "2026-01-19",
            "high_price": 101.0, "low_price": 85.0, "close_pullback_pct": -15.0,
            "avg_volume": 90.0,
        }

        result = quant.detect_confirmed_reset_contraction(df, [old, reset, confirming], [confirming])

        self.assertEqual(result["type"], "CONFIRMED_RESET_CONTRACTION")
        self.assertEqual(result["score"], 6)
        self.assertEqual(result["failure_date"], str(df.iloc[4]["date"]))
        self.assertAlmostEqual(result["confirm_volume_ratio"], 0.45)

        noisy_follow = dict(confirming, avg_volume=180.0)
        self.assertIsNone(
            quant.detect_confirmed_reset_contraction(df, [old, reset, noisy_follow], [noisy_follow])
        )

    def test_terminal_micro_contraction_strengthens_but_does_not_join_standard_group(self):
        closes = [100.0] * 70 + [95.0, 100.0, 97.0, 101.0, 102.0, 101.0, 101.0, 101.0, 101.0, 101.0]
        df = make_frame(closes)
        df["volume"] = [100.0] * 71 + [60.0] * 9
        df = quant.calc_indicators(df)
        group = [{
            "start_idx": 65, "end_idx": 70, "start_date": str(df.iloc[65]["date"]),
            "end_date": str(df.iloc[70]["date"]), "high_price": 105.0, "low_price": 94.0,
            "close_pullback_pct": -10.0, "avg_volume": 100.0,
        }]

        result = quant.detect_terminal_micro_contraction(df, group, 105.0)

        self.assertEqual(result["type"], "TERMINAL_MICRO_CONTRACTION")
        self.assertEqual(result["start_date"], str(df.iloc[71]["date"]))
        self.assertEqual(result["end_date"], str(df.iloc[72]["date"]))
        self.assertEqual(result["close_pullback_pct"], -3.0)
        self.assertEqual(len(group), 1)

    def test_contraction_extension_bonus_is_added_to_structure_score(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        base_structure = {
            "state": "VCP_EARLY", "volume_pattern": "mixed", "pivot_distance": None,
            "contraction_extension_score": 0,
        }
        enhanced_structure = dict(base_structure, contraction_extension_score=12)
        overheat = {"risk_flags": [], "risk_score": 0, "hard_reject": False}

        base = quant.score_setup(df, base_structure, {}, {}, overheat)
        enhanced = quant.score_setup(df, enhanced_structure, {}, {}, overheat)

        self.assertEqual(enhanced["structure_score"] - base["structure_score"], 12)
        self.assertEqual(enhanced["components"]["contraction_extensions"], 12)
        self.assertEqual(enhanced_structure["state"], "VCP_EARLY")

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

    def test_retest_timing_has_no_minimum_wait_but_has_hard_maximum(self):
        expected = {
            0: ("OUTSIDE", None),
            1: ("FAST", None),
            2: ("FAST", None),
            3: ("STANDARD", None),
            10: ("STANDARD", None),
            11: ("LATE", "C"),
            15: ("LATE", "C"),
            16: ("OUTSIDE", None),
        }
        for days_after, result in expected.items():
            with self.subTest(days_after=days_after):
                self.assertEqual(quant.classify_retest_timing(days_after), result)

    def test_late_timing_cap_is_weaker_than_mixed_volume_cap(self):
        self.assertEqual(quant.weaker_quality_cap("B", "C"), "C")
        self.assertEqual(quant.weaker_quality_cap("B", None), "B")
        self.assertIsNone(quant.weaker_quality_cap(None, None))

    def test_failed_structure_volume_blocks_retest_before_buy_point_scoring(self):
        df = make_frame([100.0] * 70)
        df.loc[60, ["open", "high", "low", "close", "volume"]] = [100.0, 106.0, 100.0, 105.0, 200.0]
        for idx in range(61, 70):
            df.loc[idx, ["open", "high", "low", "close", "volume"]] = [102.0, 103.0, 100.0, 102.0, 80.0]
        df = quant.calc_indicators(df)
        structure = {
            "structure_valid": True,
            "state": "VCP_MATURE",
            "volume_pattern": "failed",
            "post_breakout_state": "POST_BREAKOUT_RETEST",
            "breakout": {"idx": 60, "date": str(df.iloc[60]["date"]), "level": 101.0, "volume": 200.0, "vol_ma20": 100.0},
        }
        result = quant.detect_retest_buy(
            df, structure, {"risk_flags": [], "risk_score": 0, "hard_reject": False}, code="600000"
        )
        self.assertTrue(result["hard_block"])
        self.assertEqual(result["structure_volume_alignment"], "BLOCKED")

    def test_post_breakout_pivot_failure_closes_old_vcp_lifecycle(self):
        df = make_frame([99, 100, 98, 99, 102, 104, 100, 96])
        group = [{"start_idx": 0, "end_idx": 2, "high_price": 100.0, "low_price": 95.0}]

        info = quant.evaluate_vcp_group(df, group)

        self.assertEqual(info["post_breakout_state"], "POST_BREAKOUT_FAILED")
        self.assertFalse(info["structure_valid"])
        self.assertIn("post_structure_drawdown", info["structure_invalid_reason"])

    def test_post_breakout_expiry_closes_old_vcp_lifecycle(self):
        closes = [99, 100, 98, 99, 102] + [103] * 21
        df = make_frame(closes)
        group = [{"start_idx": 0, "end_idx": 2, "high_price": 100.0, "low_price": 95.0}]

        info = quant.evaluate_vcp_group(df, group)

        self.assertEqual(info["post_breakout_state"], "POST_BREAKOUT_EXPIRED")
        self.assertFalse(info["structure_valid"])

    def test_old_vcp_cannot_emit_pullback_after_price_breakout(self):
        structure = {"state": "VCP_FORMING", "post_breakout_state": "POST_BREAKOUT_HOT"}
        result = quant.detect_pullback_buy(make_frame([100] * 20), structure, {"risk_flags": [], "risk_score": 0})
        self.assertFalse(result["hit"])
        self.assertIn("禁止旧结构PULLBACK_BUY", result["reason"])

    def test_breakout_score_uses_pre_breakout_structure_anchor(self):
        structure = {
            "state": "VCP_FORMING",
            "setup_score_context": {
                "BREAKOUT_BUY": {
                    "structure_score": 89,
                    "anchor_date": "2026-08-07",
                    "source": "pre_event_vcp",
                }
            },
        }
        score, pattern_score, _, _, context = quant.finalize_setup_score(
            "BREAKOUT_BUY",
            9,
            [],
            [],
            structure,
            {"risk_flags": ["EXTENDED_FROM_MA20"], "risk_score": 8},
        )

        self.assertEqual(score, 67)
        self.assertEqual(pattern_score, 19)
        self.assertEqual(quant.setup_quality(score), "B")
        self.assertEqual(context["setup_structure_score"], 89)
        self.assertEqual(context["setup_structure_anchor_date"], "2026-08-07")
        self.assertEqual(context["setup_structure_base"], 53)

    def test_retest_action_score_combines_breakout_and_retest_quality(self):
        structure = {
            "state": "VCP_MATURE",
            "setup_score_context": {
                "RETEST_BUY": {
                    "structure_score": 80,
                    "anchor_date": "2026-07-31",
                    "source": "pre_event_vcp",
                    "breakout_action_score": 15,
                }
            },
        }
        score, _, _, _, context = quant.finalize_setup_score(
            "RETEST_BUY", 5, [], [], structure, {"risk_flags": [], "risk_score": 0}
        )

        self.assertEqual(context["setup_action_score"], 9)
        self.assertEqual(context["setup_current_action_score"], 5)
        self.assertEqual(context["setup_breakout_action_score"], 15)
        self.assertEqual(score, 82)

    def test_first_breakout_day_can_emit_breakout_buy_once(self):
        df = make_frame([98.0] * 79 + [103.0])
        df.loc[79, ["open", "high", "low", "volume"]] = [100.0, 104.0, 99.0, 220.0]
        df = quant.calc_indicators(df)
        structure = {
            "state": "VCP_MATURE",
            "structure_valid": True,
            "structure_pivot": 100.0,
            "post_breakout_state": "POST_BREAKOUT_HOT",
            "breakout_days": 0,
            "setup_score_context": {
                "BREAKOUT_BUY": {
                    "structure_score": 85,
                    "anchor_date": str(df.iloc[-2]["date"]),
                    "source": "pre_event_vcp",
                }
            },
        }
        overheat = {"risk_flags": [], "risk_score": 0}

        result = quant.detect_breakout_buy(df, structure, overheat)
        self.assertTrue(result["hit"])
        self.assertEqual(result["setup_quality"], "A")

        structure["breakout_days"] = 1
        repeated = quant.detect_breakout_buy(df, structure, overheat)
        self.assertFalse(repeated["hit"])
        self.assertIn("禁止重复BREAKOUT_BUY", repeated["reason"])


if __name__ == "__main__":
    unittest.main()
