#!/usr/bin/env python3
"""Regression tests for Signal Plan lifecycle gating."""

import unittest

from scripts.signal_plan import lifecycle_plan_permission, plans_for_row


def sample_row(state="PRE_BREAKOUT", signal="NONE"):
    return {
        "code": "000001",
        "name": "测试标的",
        "post_breakout_state": state,
        "structure_stage": "VCP_TIGHT",
        "setup_signal": signal,
        "structure_pivot": 100.0,
        "close": 99.0,
        "volume": 100000.0,
        "vol_ma5": 90000.0,
        "vol_ma20": 80000.0,
        "MA20": 98.0,
        "MA60": 96.0,
        "last_contraction_low": 95.0,
        "structure_score": 85.0,
        "structure_risk_score": 0.0,
        "structure_risk_flags": [],
        "setup_plan_inputs": {
            "retest": {
                "recent_breakout_level": 100.0,
                "price_low": 99.0,
                "price_high": 100.5,
                "ideal_price_low": 99.5,
                "ideal_price_high": 100.2,
                "volume_threshold": 70000.0,
                "ideal_volume_max": 55000.0,
                "invalid_price": 97.0,
            },
        },
    }


class SignalPlanLifecycleTests(unittest.TestCase):
    def test_pre_breakout_keeps_pullback_and_breakout_plans(self):
        plans = plans_for_row(sample_row())
        self.assertEqual([p["setup_family"] for p in plans], ["PULLBACK", "BREAKOUT"])

    def test_retest_state_only_generates_new_retest_plan(self):
        plans = plans_for_row(sample_row("POST_BREAKOUT_RETEST"))
        self.assertEqual([(p["setup_family"], p["plan_action"]) for p in plans], [("RETEST", "NEW")])

    def test_triggered_retest_generates_follow_plan(self):
        plans = plans_for_row(sample_row("POST_BREAKOUT_RETEST", "RETEST_BUY"))
        self.assertEqual([(p["setup_family"], p["plan_action"]) for p in plans], [("RETEST", "FOLLOW")])

    def test_non_retest_post_breakout_states_are_excluded(self):
        for state in (
            "POST_BREAKOUT_HOT",
            "POST_BREAKOUT_CONSOLIDATING",
            "POST_BREAKOUT_FAILED",
            "POST_BREAKOUT_EXPIRED",
        ):
            with self.subTest(state=state):
                allowed, reason = lifecycle_plan_permission(sample_row(state))
                self.assertEqual(allowed, set())
                self.assertTrue(reason)


if __name__ == "__main__":
    unittest.main()
