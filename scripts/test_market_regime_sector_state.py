#!/usr/bin/env python3
"""Regression tests for the split market-regime sector lifecycle model."""

import unittest

import pandas as pd

from scripts.market_regime import classify_sector_phase


def prior(rank_percentiles, phases=None, health=None, legacy=None):
    return pd.DataFrame({
        "rank_20": [max(1, round(value * 100)) for value in rank_percentiles],
        "rank_5": [10] * len(rank_percentiles),
        "rank_pct_20": rank_percentiles,
        "rank_pct_5": [0.1] * len(rank_percentiles),
        "sector_phase": phases or ["NONE"] * len(rank_percentiles),
        "sector_health": health or ["扩散健康"] * len(rank_percentiles),
        "sector_state": legacy or ["观察中"] * len(rank_percentiles),
    })


def row(rank_pct_20=0.05, rank_pct_5=0.08, relative_strength_5=4.827, up_breadth=96.88, up_breadth_5=70):
    return {
        "rank_20": max(1, round(rank_pct_20 * 100)),
        "rank_5": max(1, round(rank_pct_5 * 100)),
        "rank_pct_20": rank_pct_20,
        "rank_pct_5": rank_pct_5,
        "relative_strength_5": relative_strength_5,
        "up_breadth": up_breadth,
        "up_breadth_5": up_breadth_5,
        "history_basis": "point_in_time",
    }


class SectorStateTests(unittest.TestCase):
    def test_strong_but_not_persistent_is_emerging(self):
        result = classify_sector_phase(row(), prior([0.08, 0.12, 0.22, 0.25, 0.30]))
        self.assertEqual(result["sector_phase"], "转强")
        self.assertEqual(result["sector_policy_tier"], "B")

    def test_cooling_does_not_remove_established_mainline(self):
        value = row(relative_strength_5=11.15, up_breadth=20, up_breadth_5=45)
        result = classify_sector_phase(value, prior([0.04, 0.05, 0.15, 0.09, 0.11]))
        self.assertEqual(result["sector_phase"], "主线")
        self.assertEqual(result["sector_health"], "扩散降温")
        self.assertEqual(result["sector_policy_tier"], "B")
        self.assertEqual(result["sector_state"], "高位分歧")

    def test_one_divergent_day_only_downgrades_health(self):
        value = row(relative_strength_5=-1, up_breadth=70, up_breadth_5=30)
        history = prior([0.05] * 5, phases=["主线"] * 5, health=["扩散健康"] * 5)
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "主线")
        self.assertEqual(result["sector_health"], "明显分歧")
        self.assertEqual(result["sector_policy_tier"], "C")

    def test_two_divergent_days_confirm_fading(self):
        value = row(relative_strength_5=-1, up_breadth=70, up_breadth_5=30)
        history = prior(
            [0.05] * 5,
            phases=["主线"] * 5,
            health=["明显分歧", "扩散健康", "扩散健康", "扩散健康", "扩散健康"],
        )
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "退潮")
        self.assertEqual(result["sector_policy_tier"], "D")

    def test_old_observation_with_scattered_strength_does_not_become_fading(self):
        value = row(rank_pct_20=0.35, relative_strength_5=-1, up_breadth=40, up_breadth_5=30)
        history = prior(
            [0.30, 0.10, 0.12, 0.25, 0.08],
            health=["明显分歧"] * 5,
            legacy=["观察中", "强势初现", "观察中", "观察中", "轮动脉冲"],
        )
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "NONE")

    def test_legacy_fading_alone_is_not_migrated_as_a_lifecycle(self):
        value = row(rank_pct_20=0.35, relative_strength_5=-1, up_breadth=40, up_breadth_5=30)
        history = prior(
            [0.30] * 5,
            health=["明显分歧"] * 5,
            legacy=["弱势退潮"] * 5,
        )
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "NONE")

    def test_insufficient_history_is_building(self):
        result = classify_sector_phase(row(), prior([0.08, 0.12, 0.22, 0.25]))
        self.assertEqual(result["sector_phase"], "NONE")
        self.assertEqual(result["data_status"], "BUILDING")
        self.assertEqual(result["sector_state"], "历史积累中")

    def test_backfill_is_data_quality_not_a_market_view(self):
        value = row()
        value["history_basis"] = "current_snapshot_backfill"
        result = classify_sector_phase(value, prior([0.05] * 5))
        self.assertEqual(result["data_status"], "BACKFILL")
        self.assertEqual(result["sector_policy_tier"], "D")
        self.assertEqual(result["sector_state"], "历史积累中")

    def test_short_pulse_is_a_flag_not_a_phase(self):
        value = row(rank_pct_20=0.40, rank_pct_5=0.05)
        result = classify_sector_phase(value, prior([0.30] * 5))
        self.assertEqual(result["sector_phase"], "NONE")
        self.assertTrue(result["short_pulse"])
        self.assertEqual(result["sector_policy_tier"], "C")

    def test_single_day_selloff_does_not_override_five_day_health(self):
        value = row(relative_strength_5=3, up_breadth=10, up_breadth_5=65)
        result = classify_sector_phase(value, prior([0.05] * 5))
        self.assertEqual(result["sector_health"], "扩散健康")

    def test_mixed_five_day_signals_are_cooling(self):
        value = row(relative_strength_5=-1, up_breadth_5=60)
        result = classify_sector_phase(value, prior([0.05] * 5))
        self.assertEqual(result["sector_health"], "扩散降温")


if __name__ == "__main__":
    unittest.main()
