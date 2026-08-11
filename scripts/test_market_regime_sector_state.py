#!/usr/bin/env python3
"""Regression tests for stage-first sector lifecycle and phase trend labels."""

import unittest

import pandas as pd

from scripts.market_regime import classify_sector_phase


def prior(rank_percentiles, rank5=None, rs5=None, breadth5=None, phases=None, legacy=None):
    size = len(rank_percentiles)
    return pd.DataFrame({
        "rank_20": [max(1, round(value * 100)) for value in rank_percentiles],
        "rank_5": [max(1, round(value * 100)) for value in (rank5 or [0.10] * size)],
        "rank_pct_20": rank_percentiles,
        "rank_pct_5": rank5 or [0.10] * size,
        "relative_strength_5": rs5 or [0.0] * size,
        "up_breadth_5": breadth5 or [50.0] * size,
        "sector_phase": phases or ["NONE"] * size,
        "sector_state": legacy or ["观察中"] * size,
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
    def test_new_entry_is_turning_before_health_is_described(self):
        result = classify_sector_phase(row(), prior([0.08, 0.12, 0.22, 0.25, 0.30]))
        self.assertEqual(result["sector_phase"], "转强")
        self.assertEqual(result["sector_health"], "转强加速")
        self.assertEqual(result["sector_health_level"], 2)
        self.assertEqual(result["sector_policy_tier"], "B")
        self.assertEqual(result["sector_state"], "强势初现")

    def test_turning_can_be_blocked_without_losing_its_phase(self):
        history = prior([0.40] * 5, rank5=[0.20] * 5, rs5=[2.0] * 5, breadth5=[60.0] * 5)
        value = row(rank_pct_20=0.12, rank_pct_5=0.40, relative_strength_5=-2, up_breadth_5=35)
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "转强")
        self.assertEqual(result["sector_health"], "转强受阻")
        self.assertEqual(result["sector_policy_tier"], "B")

    def test_mainline_health_does_not_change_tier_or_legacy_state(self):
        history = prior(
            [0.05] * 5, rank5=[0.10] * 5, rs5=[4.0] * 5, breadth5=[70.0] * 5,
            phases=["主线"] * 5,
        )
        value = row(rank_pct_20=0.14, rank_pct_5=0.50, relative_strength_5=-4, up_breadth_5=30)
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "主线")
        self.assertEqual(result["sector_health"], "高位分歧")
        self.assertEqual(result["sector_health_level"], -2)
        self.assertEqual(result["sector_policy_tier"], "A")
        self.assertEqual(result["sector_state"], "持续主线")

    def test_first_day_outside_exit_zone_remains_mainline(self):
        history = prior([0.05] * 5, phases=["主线"] * 5)
        result = classify_sector_phase(row(rank_pct_20=0.25), history)
        self.assertEqual(result["sector_phase"], "主线")

    def test_two_days_outside_exit_zone_confirm_fading(self):
        history = prior(
            [0.30, 0.05, 0.05, 0.05, 0.05], rank5=[0.50] * 5, rs5=[-5.0] * 5,
            breadth5=[20.0] * 5, phases=["主线"] * 5,
        )
        value = row(rank_pct_20=0.25, rank_pct_5=0.05, relative_strength_5=5, up_breadth_5=80)
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "退潮")
        self.assertEqual(result["sector_health"], "强力修复")
        self.assertEqual(result["sector_health_level"], 2)
        self.assertEqual(result["sector_policy_tier"], "D")
        self.assertEqual(result["sector_state"], "弱势退潮")

    def test_fading_expires_to_observation_after_five_more_weak_days(self):
        history = prior(
            [0.30] * 5, phases=["退潮"] * 5,
            rank5=[0.40] * 5, rs5=[-2.0] * 5, breadth5=[35.0] * 5,
        )
        result = classify_sector_phase(
            row(rank_pct_20=0.28, rank_pct_5=0.30, relative_strength_5=0.5, up_breadth_5=55),
            history,
        )
        self.assertEqual(result["sector_phase"], "NONE")
        self.assertEqual(result["sector_state"], "观察中")
        self.assertEqual(result["sector_health"], "蓄势增强")

    def test_recent_fading_remains_fading(self):
        history = prior(
            [0.30, 0.30, 0.30, 0.30, 0.05],
            phases=["退潮", "退潮", "退潮", "退潮", "主线"],
        )
        result = classify_sector_phase(row(rank_pct_20=0.28), history)
        self.assertEqual(result["sector_phase"], "退潮")

    def test_fading_recovery_inside_top_twenty_does_not_expire(self):
        history = prior([0.30] * 5, phases=["退潮"] * 5)
        result = classify_sector_phase(row(rank_pct_20=0.18), history)
        self.assertEqual(result["sector_phase"], "退潮")

    def test_observation_uses_improvement_language(self):
        history = prior([0.40] * 5, rank5=[0.40] * 5, rs5=[0.0] * 5, breadth5=[50.0] * 5)
        value = row(rank_pct_20=0.30, rank_pct_5=0.20, relative_strength_5=3, up_breadth_5=70)
        result = classify_sector_phase(value, history)
        self.assertEqual(result["sector_phase"], "NONE")
        self.assertEqual(result["sector_health"], "蓄势增强")
        self.assertEqual(result["sector_health_level"], 2)
        self.assertEqual(result["sector_policy_tier"], "D")

    def test_old_observation_with_scattered_strength_does_not_become_fading(self):
        history = prior(
            [0.30, 0.10, 0.12, 0.25, 0.08],
            legacy=["观察中", "强势初现", "观察中", "观察中", "轮动脉冲"],
        )
        result = classify_sector_phase(row(rank_pct_20=0.35), history)
        self.assertEqual(result["sector_phase"], "NONE")

    def test_legacy_fading_alone_is_not_migrated_as_a_lifecycle(self):
        history = prior([0.30] * 5, legacy=["弱势退潮"] * 5)
        result = classify_sector_phase(row(rank_pct_20=0.35), history)
        self.assertEqual(result["sector_phase"], "NONE")

    def test_insufficient_history_is_building(self):
        result = classify_sector_phase(row(), prior([0.08, 0.12, 0.22, 0.25]))
        self.assertEqual(result["sector_phase"], "NONE")
        self.assertEqual(result["data_status"], "BUILDING")
        self.assertEqual(result["sector_health"], "数据不足")
        self.assertEqual(result["sector_state"], "历史积累中")

    def test_backfill_is_data_quality_not_a_market_view(self):
        value = row()
        value["history_basis"] = "current_snapshot_backfill"
        result = classify_sector_phase(value, prior([0.05] * 5))
        self.assertEqual(result["data_status"], "BACKFILL")
        self.assertEqual(result["sector_policy_tier"], "D")
        self.assertEqual(result["sector_health"], "数据不足")
        self.assertEqual(result["sector_state"], "历史积累中")

    def test_short_pulse_does_not_raise_policy_tier(self):
        value = row(rank_pct_20=0.40, rank_pct_5=0.05)
        result = classify_sector_phase(value, prior([0.30] * 5))
        self.assertEqual(result["sector_phase"], "NONE")
        self.assertTrue(result["short_pulse"])
        self.assertEqual(result["sector_policy_tier"], "D")

    def test_single_day_participation_does_not_enter_phase_trend(self):
        history = prior([0.05] * 5, rs5=[3.0] * 5, breadth5=[65.0] * 5)
        strong_day = classify_sector_phase(row(up_breadth=90, relative_strength_5=3, up_breadth_5=65), history)
        weak_day = classify_sector_phase(row(up_breadth=10, relative_strength_5=3, up_breadth_5=65), history)
        self.assertEqual(strong_day["sector_health_score"], weak_day["sector_health_score"])


if __name__ == "__main__":
    unittest.main()
