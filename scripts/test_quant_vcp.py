"""Regression tests for Model 2 close-based VCP contraction detection."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

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
    def test_confirmed_breakout_consumes_old_contractions_before_new_vcp(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        contractions = [
            {"start_idx": 50, "end_idx": 54, "start_date": "2026-03-12", "end_date": "2026-03-18", "high_price": 110.0, "low_price": 98.0, "close_pullback_pct": -10.0, "avg_volume": 100.0},
            {"start_idx": 56, "end_idx": 59, "start_date": "2026-03-20", "end_date": "2026-03-25", "high_price": 108.0, "low_price": 100.0, "close_pullback_pct": -7.0, "avg_volume": 90.0},
            {"start_idx": 65, "end_idx": 69, "start_date": "2026-04-02", "end_date": "2026-04-08", "high_price": 120.0, "low_price": 115.0, "close_pullback_pct": -4.0, "avg_volume": 80.0},
        ]
        prior = {
            "group": contractions[:2],
            "breakout": {"idx": 62, "date": "2026-03-30", "level": 110.0},
            "post_breakout_state": "POST_BREAKOUT_HOT",
            "vcp_eligible": True,
        }

        def evaluate(_df, group):
            return {
                "group": group, "structure_pivot": max(item["high_price"] for item in group),
                "market_pivot": 120.0, "pivot_price": max(item["high_price"] for item in group),
                "pivot_distance": -1.0, "last_contraction_low": group[-1]["low_price"],
                "structure_age_days": len(_df) - group[-1]["end_idx"] - 1,
                "structure_valid": True, "structure_invalid_reason": "", "post_structure_gain": 1.0,
                "post_structure_drawdown": -1.0, "post_breakout_state": "PRE_BREAKOUT",
                "post_breakout_failure": None, "post_group_support_break": None,
                "breakout": None, "breakout_days": None,
            }

        with patch.object(quant, "find_latest_consumed_breakout", return_value=prior), patch.object(
            quant, "evaluate_vcp_group", side_effect=evaluate
        ):
            current, _ = quant.select_current_vcp_group(df, contractions)

        self.assertEqual(current["group"], [contractions[2]])
        self.assertIs(current["prior_breakout_context"], prior)

    def test_price_breakout_boundary_does_not_promote_rejected_prior_group(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        contractions = [
            {"start_idx": 50, "end_idx": 54, "high_price": 110.0, "low_price": 98.0, "close_pullback_pct": -10.0, "avg_volume": 100.0},
            {"start_idx": 56, "end_idx": 59, "high_price": 108.0, "low_price": 100.0, "close_pullback_pct": -7.0, "avg_volume": 90.0},
            {"start_idx": 65, "end_idx": 69, "high_price": 120.0, "low_price": 115.0, "close_pullback_pct": -4.0, "avg_volume": 80.0},
        ]
        boundary = {
            "group": contractions[:2],
            "breakout": {"idx": 62, "date": "2026-03-30", "level": 110.0},
            "post_breakout_state": "POST_BREAKOUT_HOT",
            "vcp_eligible": False,
        }

        def evaluate(_df, group):
            return {
                "group": group, "structure_pivot": max(item["high_price"] for item in group),
                "market_pivot": 120.0, "pivot_price": max(item["high_price"] for item in group),
                "pivot_distance": -1.0, "last_contraction_low": group[-1]["low_price"],
                "structure_age_days": len(_df) - group[-1]["end_idx"] - 1,
                "structure_valid": True, "structure_invalid_reason": "", "post_structure_gain": 1.0,
                "post_structure_drawdown": -1.0, "post_breakout_state": "PRE_BREAKOUT",
                "post_breakout_failure": None, "post_group_support_break": None,
                "breakout": None, "breakout_days": None,
            }

        with patch.object(quant, "find_latest_consumed_breakout", return_value=boundary), patch.object(
            quant, "evaluate_vcp_group", side_effect=evaluate
        ):
            current, _ = quant.select_current_vcp_group(df, contractions)

        self.assertEqual(current["group"], [contractions[2]])
        self.assertIsNone(current.get("prior_breakout_context"))

    def test_unqualified_group_failure_does_not_poison_fresh_candidate(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        contractions = [
            {"start_idx": 50, "end_idx": 54, "high_price": 110.0, "low_price": 98.0, "close_pullback_pct": -10.0, "avg_volume": 100.0},
            {"start_idx": 56, "end_idx": 59, "high_price": 108.0, "low_price": 100.0, "close_pullback_pct": -7.0, "avg_volume": 90.0},
            {"start_idx": 65, "end_idx": 69, "high_price": 120.0, "low_price": 115.0, "close_pullback_pct": -4.0, "avg_volume": 80.0},
        ]
        rejected_boundary = {
            "group": contractions[:2],
            "breakout": {"idx": 62, "date": "2026-03-30", "level": 110.0},
            "post_breakout_state": "POST_BREAKOUT_FAILED",
            "post_breakout_failure": {"idx": 70, "date": "2026-04-10"},
            "vcp_eligible": False,
        }

        def evaluate(_df, group):
            valid = group == [contractions[2]]
            return {
                "group": group, "structure_pivot": max(item["high_price"] for item in group),
                "market_pivot": 120.0, "pivot_price": max(item["high_price"] for item in group),
                "pivot_distance": -1.0, "last_contraction_low": group[-1]["low_price"],
                "structure_age_days": len(_df) - group[-1]["end_idx"] - 1,
                "structure_valid": valid,
                "structure_invalid_reason": "" if valid else "post_structure_drawdown",
                "post_structure_gain": 1.0, "post_structure_drawdown": -1.0,
                "post_breakout_state": "PRE_BREAKOUT" if valid else "POST_BREAKOUT_FAILED",
                "post_breakout_failure": None if valid else {"idx": 70, "date": "2026-04-10"},
                "post_group_support_break": None, "breakout": None, "breakout_days": None,
            }

        with patch.object(quant, "find_latest_consumed_breakout", return_value=rejected_boundary), patch.object(
            quant, "evaluate_vcp_group", side_effect=evaluate
        ):
            current, _ = quant.select_current_vcp_group(
                df, contractions, qualified_failure_only=True
            )

        self.assertEqual(current["group"], [contractions[2]])
        self.assertNotIn("qualified_failure_context", current)

    def test_qualified_failure_blocks_old_groups_but_keeps_post_failure_group(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        contractions = [
            {"start_idx": 50, "end_idx": 54, "high_price": 110.0, "low_price": 98.0, "close_pullback_pct": -10.0, "avg_volume": 100.0},
            {"start_idx": 56, "end_idx": 59, "high_price": 108.0, "low_price": 100.0, "close_pullback_pct": -7.0, "avg_volume": 90.0},
            {"start_idx": 65, "end_idx": 69, "high_price": 120.0, "low_price": 115.0, "close_pullback_pct": -4.0, "avg_volume": 80.0},
        ]
        qualified_boundary = {
            "group": contractions[:2],
            "breakout": {"idx": 60, "date": "2026-03-27", "level": 110.0},
            "post_breakout_state": "POST_BREAKOUT_FAILED",
            "post_breakout_failure": {"idx": 62, "date": "2026-03-31"},
            "vcp_eligible": True,
        }

        def evaluate(_df, group):
            return {
                "group": group, "structure_pivot": max(item["high_price"] for item in group),
                "market_pivot": 120.0, "pivot_price": max(item["high_price"] for item in group),
                "pivot_distance": -1.0, "last_contraction_low": group[-1]["low_price"],
                "structure_age_days": len(_df) - group[-1]["end_idx"] - 1,
                "structure_valid": True, "structure_invalid_reason": "",
                "post_structure_gain": 1.0, "post_structure_drawdown": -1.0,
                "post_breakout_state": "PRE_BREAKOUT", "post_breakout_failure": None,
                "post_group_support_break": None, "breakout": None, "breakout_days": None,
            }

        with patch.object(quant, "find_latest_consumed_breakout", return_value=qualified_boundary), patch.object(
            quant, "evaluate_vcp_group", side_effect=evaluate
        ):
            current, _ = quant.select_current_vcp_group(
                df, contractions, qualified_failure_only=True
            )

        self.assertEqual(current["group"], [contractions[2]])
        self.assertIs(current["qualified_failure_context"], qualified_boundary)
        self.assertTrue(current["rebuild_after_failed_breakout"])

    def test_qualified_failure_can_surface_low_volume_rebuild_watch(self):
        closes = [100.0] * 65 + [105.0, 102.0, 96.0, 91.0, 90.0, 91.0, 92.0, 92.0, 93.0, 93.0, 93.0]
        df = make_frame(closes)
        df["volume"] = [100.0] * len(df)
        df.loc[65, "volume"] = 300.0
        df.loc[70:, "volume"] = 80.0
        context = {
            "vcp_eligible": True,
            "breakout": {"idx": 65, "date": str(df.iloc[65]["date"]), "volume": 300.0},
            "post_breakout_failure": {"idx": 69, "date": str(df.iloc[69]["date"])},
        }

        result = quant.detect_post_failure_rebuild_watch(df, context)

        self.assertIsNotNone(result)
        self.assertEqual(result["base_days"], 5)
        self.assertLess(result["avg_volume_vs_impulse_ratio"], 0.55)

        structure = {"post_failure_rebuild_watch": result}
        quant.attach_prior_breakout_bonus(structure)
        self.assertEqual(structure["prior_breakout_context_tag"], "放量启动后低量重建")
        self.assertIn("不开放任何买点权限", structure["prior_breakout_bonus_reasons"][-1])

    def test_rebuild_discovery_cannot_emit_setup_even_if_detector_hits(self):
        structure = {
            "state": "TREND_REBUILD",
            "post_breakout_state": "POST_BREAKOUT_FAILED",
            "right_edge_rebuild_watch": {"contraction_count": 1},
        }
        hit = {"hit": True, "setup_quality": "A"}

        result = quant.classify_result(structure, hit, hit, hit, {}, {})

        self.assertEqual(result, ("VCP", "NONE", "WAIT_REBUILD", "0"))

    def test_setup_anchor_snapshot_disables_rebuild_discovery(self):
        df = quant.calc_indicators(make_frame([100.0] * 90))
        structure = {"state": "NONE"}
        with patch.object(
            quant, "detect_vcp_structure", return_value=structure
        ) as detector, patch.object(
            quant, "detect_overheat", return_value={}
        ), patch.object(
            quant,
            "score_setup",
            return_value={"structure_score": 27.0},
        ):
            result = quant.setup_anchor_snapshot(df, 85)

        detector.assert_called_once()
        called_df = detector.call_args.args[0]
        self.assertEqual(len(called_df), 85)
        self.assertFalse(detector.call_args.kwargs["enable_rebuild_discovery"])
        self.assertEqual(result["structure_score"], 27.0)

    def test_consumed_breakout_requires_same_valid_vcp_on_previous_session(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        group = [
            {"start_idx": 50, "end_idx": 54},
            {"start_idx": 56, "end_idx": 59},
        ]
        valid = {
            "has_structure": True,
            "state": "VCP_FORMING",
            "contraction_group": group,
            "structure_pivot": 110.0,
        }
        rejected = dict(valid, has_structure=False, state="REJECT")

        with patch.object(quant, "detect_vcp_structure", return_value=valid):
            self.assertTrue(quant.prior_breakout_vcp_eligible(df, group, 110.0, 62))
        with patch.object(quant, "detect_vcp_structure", return_value=rejected):
            self.assertFalse(quant.prior_breakout_vcp_eligible(df, group, 110.0, 62))

        different_group = dict(valid, contraction_group=[{"start_idx": 51, "end_idx": 54}])
        with patch.object(quant, "detect_vcp_structure", return_value=different_group):
            self.assertFalse(quant.prior_breakout_vcp_eligible(df, group, 110.0, 62))

    def test_prior_breakout_bonus_is_reference_only(self):
        structure = {
            "contraction_group": [{"start_idx": 20}],
            "structure_score_estimate": 58,
            "prior_breakout_context": {
                "breakout": {"date": "2026-08-03"},
                "structure_pivot": 16.76,
                "post_breakout_state": "POST_BREAKOUT_HOT",
                "post_breakout_failure": None,
            },
            "setup_score_context": {"RETEST_BUY": {"structure_score": 47}},
        }

        quant.attach_prior_breakout_bonus(structure)

        self.assertEqual(structure["structure_score_estimate"], 58)
        self.assertEqual(structure["prior_breakout_bonus_score"], 47)
        self.assertEqual(structure["prior_breakout_context_tag"], "之前已有突破并强势整理")
        self.assertIn("不计入当前结构分", structure["prior_breakout_bonus_reasons"][-1])

    def test_strong_impulse_context_is_tag_only(self):
        closes = [100.0] * 70 + [101, 102, 104, 107, 111, 116, 119, 118, 117, 116]
        df = quant.calc_indicators(make_frame(closes))
        df.loc[70:75, "volume"] = [110, 120, 140, 180, 240, 300]
        df.loc[76:79, "volume"] = [180, 130, 90, 80]
        structure = {
            "state": "VCP_EARLY",
            "structure_valid": True,
            "volume_pattern": "drying",
            "contraction_group": [{
                "start_idx": 75, "end_idx": 79, "close_pullback_pct": -5.0,
            }],
            "prior_breakout_bonus_score": None,
            "prior_breakout_bonus_reasons": [],
            "prior_breakout_context_tag": "",
        }

        quant.attach_strong_impulse_context(df, structure)

        self.assertEqual(structure["prior_breakout_context_tag"], "放量启动后强势整理")
        self.assertIsNone(structure["prior_breakout_bonus_score"])
        self.assertIn("不计入当前结构分或买点权限", structure["prior_breakout_bonus_reasons"][-1])

    def test_strong_impulse_context_does_not_replace_valid_vcp_bonus(self):
        df = quant.calc_indicators(make_frame([100.0] * 80))
        structure = {
            "state": "VCP_EARLY",
            "structure_valid": True,
            "volume_pattern": "drying",
            "contraction_group": [{"start_idx": 75, "end_idx": 79, "close_pullback_pct": -5.0}],
            "prior_breakout_bonus_score": 72,
            "prior_breakout_bonus_reasons": ["有效前序VCP"],
            "prior_breakout_context_tag": "之前已有突破并强势整理",
        }

        quant.attach_strong_impulse_context(df, structure)

        self.assertEqual(structure["prior_breakout_context_tag"], "之前已有突破并强势整理")
        self.assertEqual(structure["prior_breakout_bonus_score"], 72)

    def test_new_vcp_keeps_prior_retest_lifecycle_available(self):
        breakout = {"idx": 60, "date": "2026-03-30", "level": 110.0}
        old_group = [{"start_idx": 50}, {"start_idx": 56}]
        structure = {
            "state": "VCP_EARLY", "structure_valid": True,
            "post_breakout_state": "PRE_BREAKOUT", "contraction_group": [{"start_idx": 65}],
            "prior_breakout_context": {
                "post_breakout_state": "POST_BREAKOUT_RETEST", "structure_valid": True,
                "breakout": breakout, "group": old_group, "volume_pattern": "decreasing",
            },
            "setup_score_context": {"RETEST_BUY": {"structure_stage": "VCP_FORMING"}},
        }

        retest_structure = quant.retest_structure_for_detection(structure)

        self.assertEqual(retest_structure["post_breakout_state"], "POST_BREAKOUT_RETEST")
        self.assertEqual(retest_structure["breakout"], breakout)
        self.assertEqual(retest_structure["contraction_group"], old_group)
        self.assertEqual(retest_structure["state"], "VCP_FORMING")
        self.assertEqual(structure["post_breakout_state"], "PRE_BREAKOUT")

    def test_strong_post_breakout_early_vcp_can_use_pullback_gate(self):
        structure = {
            "state": "VCP_EARLY", "volume_pattern": "drying",
            "last_contraction_low": 18.32,
            "contraction_group": [{"start_idx": 65}],
            "prior_breakout_bonus_score": 47,
            "prior_breakout_context_tag": "之前已有突破并强势整理",
            "prior_breakout_context": {"structure_pivot": 16.76},
        }

        self.assertTrue(quant.post_breakout_early_pullback_allowed(structure))
        structure["prior_breakout_context_tag"] = ""
        self.assertFalse(quant.post_breakout_early_pullback_allowed(structure))

    def test_early_vcp_without_valid_prior_context_cannot_use_pullback_gate(self):
        structure = {
            "state": "VCP_EARLY", "volume_pattern": "drying",
            "last_contraction_low": 18.32,
            "contraction_group": [{"start_idx": 65}],
            "prior_breakout_bonus_score": None,
            "prior_breakout_context_tag": "",
            "prior_breakout_context": None,
        }

        self.assertFalse(quant.post_breakout_early_pullback_allowed(structure))
        structure["prior_breakout_context_tag"] = "放量启动后强势整理"
        self.assertFalse(quant.post_breakout_early_pullback_allowed(structure))

    def test_top_level_prices_follow_the_selected_setup(self):
        pullback = {"support_price": 22.31, "invalid_price": 14.94}
        breakout = {"support_price": 25.20, "invalid_price": 24.44, "breakout_level": 25.20}
        retest = {"support_price": 26.00, "invalid_price": 25.00, "breakout_level": 26.00}

        detail = quant.choose_setup_detail("PULLBACK_BUY", pullback, breakout, retest)

        self.assertEqual(quant.setup_reference_prices(detail), (22.31, 14.94, None))

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

    def test_right_edge_standard_pullback_is_provisional_without_three_future_days(self):
        closes = [18.0] * 74 + [18.4, 18.7, 19.16, 18.64, 18.27, 18.94]
        df = make_frame(closes)
        confirmed = [{"end_idx": 73}]

        result = quant.detect_right_edge_provisional_contraction(df, confirmed)

        self.assertIsNotNone(result)
        self.assertEqual(result["start_close"], 19.16)
        self.assertEqual(result["end_close"], 18.27)
        self.assertEqual(result["close_pullback_pct"], -4.65)
        self.assertEqual(result["confirmation_status"], "PROVISIONAL")
        self.assertEqual(result["right_confirm_days"], 1)
        self.assertEqual(result["required_right_confirm_days"], 3)

    def test_right_edge_provisional_low_extends_when_latest_close_is_lower(self):
        base = [100.0] * 72 + [101.0, 102.0, 104.0, 102.0, 99.0, 98.0]
        confirmed = [{"end_idx": 70}]
        first = quant.detect_right_edge_provisional_contraction(make_frame(base), confirmed)
        extended = quant.detect_right_edge_provisional_contraction(
            make_frame([*base, 97.0]), confirmed
        )

        self.assertEqual(first["end_idx"], 77)
        self.assertEqual(first["end_close"], 98.0)
        self.assertEqual(extended["end_idx"], 78)
        self.assertEqual(extended["end_close"], 97.0)
        self.assertEqual(extended["right_confirm_days"], 0)
        self.assertLess(extended["close_pullback_pct"], first["close_pullback_pct"])

    def test_right_edge_provisional_becomes_confirmed_after_three_future_days(self):
        closes = [100.0] * 72 + [101.0, 102.0, 104.0, 102.0, 99.0, 97.0, 99.0, 100.0, 101.0]
        df = make_frame(closes)

        contractions = quant.detect_contractions(df)
        result = contractions[-1]

        self.assertEqual(result["start_close"], 104.0)
        self.assertEqual(result["end_close"], 97.0)
        self.assertEqual(result["confirmation_status"], "CONFIRMED")
        self.assertEqual(result["right_confirm_days"], 3)
        self.assertIsNone(
            quant.detect_right_edge_provisional_contraction(df, contractions)
        )

    def test_provisional_standard_contraction_advances_live_structure_stage(self):
        trend = [10.0 + index * 8.0 / 69 for index in range(70)]
        closes = trend + [
            18.4, 18.8, 19.2, 20.0, 19.5, 19.0, 18.0,
            18.4, 18.8, 19.2, 19.5, 19.0, 18.6, 18.9,
        ]

        result = quant.detect_vcp_structure(quant.calc_indicators(make_frame(closes)))

        self.assertEqual(result["state"], "VCP_FORMING")
        self.assertEqual(result["contraction_count"], 2)
        self.assertEqual(result["confirmed_contraction_count"], 1)
        self.assertEqual(result["provisional_contraction_count"], 1)
        self.assertEqual(result["effective_contraction_count"], 2)
        self.assertEqual(result["contraction_confirmation_status"], "PROVISIONAL")
        self.assertEqual(
            [item["confirmation_status"] for item in result["contraction_group"]],
            ["CONFIRMED", "PROVISIONAL"],
        )

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

    def test_destructive_reset_uses_major_peak_and_isolates_overlapping_segments(self):
        closes = [100.0] * 80 + [120.0, 115.0, 110.0, 100.0, 90.0, 80.0, 82.0, 85.0, 88.0, 90.0]
        df = quant.calc_indicators(make_frame(closes))

        event = quant.detect_destructive_reset(df)

        self.assertEqual(event["peak_idx"], 80)
        self.assertEqual(event["peak_close"], 120.0)
        self.assertEqual(event["low_idx"], 85)
        self.assertEqual(event["close_drawdown_pct"], -33.33)
        rebuild = quant.destructive_reset_rebuild_status(df, event)
        self.assertFalse(rebuild["ready"])

        contractions = [
            {"start_idx": 60, "end_idx": 65},
            {"start_idx": 80, "end_idx": 85},
            {"start_idx": 87, "end_idx": 89},
        ]
        annotated, post_reset = quant.annotate_contractions_for_destructive_reset(
            contractions, event, rebuild_ready=False
        )
        self.assertEqual(
            [item["vcp_segment_status"] for item in annotated],
            ["PRE_RESET", "OVERLAPS_DESTRUCTIVE_RESET", "POST_RESET_REBUILD"],
        )
        self.assertEqual(post_reset, [annotated[-1]])
        self.assertEqual(annotated[1]["excluded_reason"], "DESTRUCTIVE_RESET")
        self.assertFalse(any(item["eligible_for_vcp"] for item in annotated))

        current = {
            "group": [
                {"start_idx": 87, "end_idx": 89, "close_pullback_pct": -12.0},
                {"start_idx": 90, "end_idx": 92, "close_pullback_pct": -10.0},
            ],
            "post_breakout_state": "PRE_BREAKOUT",
        }
        self.assertTrue(
            quant.destructive_reset_relevant_to_current(event, contractions, current)
        )
        self.assertFalse(
            quant.destructive_reset_relevant_to_current(
                event, contractions, dict(current, post_breakout_state="POST_BREAKOUT_HOT")
            )
        )

    def test_destructive_reset_rebuild_gate_can_reopen_post_reset_segments(self):
        closes = [100.0] * 80 + [120.0, 115.0, 110.0, 100.0, 90.0, 80.0]
        closes += [82.0 + step * 2.0 for step in range(21)]
        df = quant.calc_indicators(make_frame(closes))
        event = quant.detect_destructive_reset(df)

        rebuild = quant.destructive_reset_rebuild_status(df, event)

        self.assertTrue(rebuild["ready"])
        contraction = {"start_idx": 90, "end_idx": 94}
        annotated, post_reset = quant.annotate_contractions_for_destructive_reset(
            [contraction], event, rebuild_ready=True
        )
        self.assertEqual(annotated[0]["vcp_segment_status"], "POST_RESET_ELIGIBLE")
        self.assertTrue(annotated[0]["eligible_for_vcp"])
        self.assertEqual(post_reset, annotated)

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
        old_group = [
            {
                "start_idx": 0, "end_idx": 0, "start_date": "2026-01-01", "end_date": "2026-01-01",
                "high_price": 100.0, "low_price": 88.0, "close_pullback_pct": -12.0,
                "avg_volume": 110.0,
            },
            {
                "start_idx": 1, "end_idx": 2, "start_date": "2026-01-02", "end_date": "2026-01-05",
                "high_price": 100.0, "low_price": 90.0, "close_pullback_pct": -10.0,
                "avg_volume": 100.0,
            },
        ]
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

        result = quant.detect_confirmed_reset_contraction(
            df, old_group + [reset, confirming], [confirming]
        )

        self.assertEqual(result["type"], "CONFIRMED_RESET_CONTRACTION")
        self.assertEqual(result["score"], 6)
        self.assertEqual(result["failure_date"], str(df.iloc[4]["date"]))
        self.assertEqual(result["prior_contraction_count"], 2)
        self.assertEqual(result["prior_structure_pivot"], 100.0)
        self.assertAlmostEqual(result["confirm_volume_ratio"], 0.45)

        noisy_follow = dict(confirming, avg_volume=180.0)
        self.assertIsNone(
            quant.detect_confirmed_reset_contraction(
                df, old_group + [reset, noisy_follow], [noisy_follow]
            )
        )

    def test_confirmed_reset_rejects_single_swing_and_deep_trend_break(self):
        df = make_frame([99, 100, 98, 102, 95, 90, 80, 85, 90, 95, 100, 92, 85, 88, 90, 92])
        old = {
            "start_idx": 0, "end_idx": 2, "start_date": "2026-01-01", "end_date": "2026-01-05",
            "high_price": 100.0, "low_price": 90.0, "close_pullback_pct": -10.0,
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

        self.assertIsNone(
            quant.detect_confirmed_reset_contraction(df, [old, reset, confirming], [confirming])
        )

        old_group = [
            dict(old, start_idx=0, end_idx=0, close_pullback_pct=-12.0),
            dict(old, start_idx=1, end_idx=2),
        ]
        deep_reset = dict(reset, close_pullback_pct=-25.1)
        self.assertIsNone(
            quant.detect_confirmed_reset_contraction(
                df, old_group + [deep_reset, confirming], [confirming]
            )
        )

        expanding_old_group = [
            dict(old, start_idx=0, end_idx=0, close_pullback_pct=-8.0),
            dict(old, start_idx=1, end_idx=2, close_pullback_pct=-10.0),
        ]
        self.assertIsNone(
            quant.detect_confirmed_reset_contraction(
                df, expanding_old_group + [reset, confirming], [confirming]
            )
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

    def test_provisional_low_day_cannot_self_confirm_pullback_from_intraday_low(self):
        df = pd.DataFrame([{
            "date": "2026-08-21", "close": 100.0, "low": 95.0, "volume": 70.0,
            "distance_ma20": 0.0, "distance_ma60": 0.0, "volume_dry_up": 0.7,
            "MA20_slope": 1.0, "MA20": 100.0, "MA60": 100.0,
            "vol_ma20": 100.0, "low_20": 95.0,
        }])
        structure = {
            "state": "VCP_FORMING", "post_breakout_state": "PRE_BREAKOUT",
            "volume_pattern": "decreasing", "last_contraction_low": 95.0,
            "contraction_group": [{
                "low_price": 95.0, "end_close": 100.0, "avg_volume": 100.0,
                "confirmation_status": "PROVISIONAL", "right_confirm_days": 0,
            }],
            "setup_score_context": {"PULLBACK_BUY": {"structure_score": 71.0}},
        }

        result = quant.detect_pullback_buy(
            df, structure, {"risk_flags": [], "risk_score": 0}
        )

        self.assertGreater(df.iloc[-1]["close"], structure["last_contraction_low"] * 1.02)
        self.assertFalse(result["hit"])
        self.assertIn("最近收缩低点未守住", result["setup_misses"])
        self.assertEqual(result["plan_inputs"]["last_low_confirmation_anchor"], "end_close")
        self.assertEqual(result["plan_inputs"]["last_low_required_price"], 102.0)

    def test_later_close_can_confirm_pullback_above_end_close(self):
        df = pd.DataFrame([{
            "date": "2026-08-24", "close": 102.1, "low": 99.0, "volume": 70.0,
            "distance_ma20": 2.1, "distance_ma60": 2.1, "volume_dry_up": 0.7,
            "MA20_slope": 1.0, "MA20": 100.0, "MA60": 100.0,
            "vol_ma20": 100.0, "low_20": 95.0,
        }])
        structure = {
            "state": "VCP_FORMING", "post_breakout_state": "PRE_BREAKOUT",
            "volume_pattern": "decreasing", "last_contraction_low": 95.0,
            "contraction_group": [{
                "low_price": 95.0, "end_close": 100.0, "avg_volume": 100.0,
                "confirmation_status": "PROVISIONAL", "right_confirm_days": 1,
            }],
            "setup_score_context": {"PULLBACK_BUY": {"structure_score": 71.0}},
        }

        result = quant.detect_pullback_buy(
            df, structure, {"risk_flags": [], "risk_score": 0}
        )

        self.assertTrue(result["hit"])
        self.assertEqual(result["reason"], "缩量回踩MA20")

    def test_pullback_does_not_fall_back_to_intraday_low_without_end_close(self):
        structure = {
            "contraction_group": [{"low_price": 95.0}],
            "last_contraction_low": 95.0,
        }

        self.assertIsNone(quant.pullback_confirmation_close(structure))

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

    def test_post_breakout_score_reuses_frozen_pre_breakout_anchor(self):
        structure = {
            "post_breakout_state": "POST_BREAKOUT_HOT",
            "setup_score_context": {
                "RETEST_BUY": {"structure_score": 89, "anchor_date": "2026-08-07"},
            },
        }
        self.assertEqual(quant.frozen_breakout_structure_score(structure), 89)
        structure["post_breakout_state"] = "PRE_BREAKOUT"
        self.assertIsNone(quant.frozen_breakout_structure_score(structure))

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
