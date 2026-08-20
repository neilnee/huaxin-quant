#!/usr/bin/env python3
"""Regression tests for Bloom status mapping."""

import unittest

from scripts.bloom import apply_exit_rules, base_status, is_watching_row, should_call_llm_insight, state_row


def sample_row(**overrides):
    row = {
        "code": "000001",
        "model2_include": True,
        "structure_stage": "NONE",
        "structure_valid": True,
        "setup_signal": "NONE",
        "action_hint": "AVOID_CHASE",
        "post_breakout_state": "POST_BREAKOUT_HOT",
        "structure_risk_score": 0,
        "structure_risk_flags": [],
    }
    row.update(overrides)
    return row


class BloomStatusMappingTests(unittest.TestCase):
    def test_post_breakout_lifecycle_does_not_fall_back_to_forming(self):
        for state in (
            "POST_BREAKOUT_RETEST",
            "POST_BREAKOUT_HOT",
            "POST_BREAKOUT_CONSOLIDATING",
            "POST_BREAKOUT_FAILED",
            "POST_BREAKOUT_EXPIRED",
        ):
            with self.subTest(state=state):
                self.assertEqual(base_status(sample_row(post_breakout_state=state)), "COOLDOWN")

    def test_triggered_retest_takes_priority_over_post_breakout_cooldown(self):
        self.assertEqual(
            base_status(sample_row(post_breakout_state="POST_BREAKOUT_RETEST", setup_signal="RETEST_BUY")),
            "TRIGGERED",
        )

    def test_pre_breakout_stage_keeps_normal_stage_mapping(self):
        self.assertEqual(
            base_status(sample_row(post_breakout_state="PRE_BREAKOUT", structure_stage="VCP_FORMING")),
            "FORMING",
        )

    def test_live_post_breakout_state_ignores_generic_cooldown_limit(self):
        previous = {"consecutive_reject": "99"}
        row = sample_row(post_breakout_state="POST_BREAKOUT_HOT")
        self.assertEqual(apply_exit_rules(previous, "COOLDOWN", row), "COOLDOWN")

    def test_terminal_post_breakout_state_exits_immediately(self):
        row = sample_row(post_breakout_state="POST_BREAKOUT_EXPIRED")
        self.assertEqual(apply_exit_rules({}, "COOLDOWN", row), "EXIT")

    def test_state_row_keeps_model2_post_breakout_facts(self):
        row = sample_row(
            structure_breakout_date="2026-07-30", breakout_days=15,
            structure_breakout_level=27.33,
        )
        result = state_row({}, row, "2026-08-20", base_status(row))
        self.assertEqual(result["post_breakout_state"], "POST_BREAKOUT_HOT")
        self.assertEqual(result["structure_breakout_date"], "2026-07-30")
        self.assertEqual(result["breakout_days"], "15")
        self.assertEqual(result["consecutive_reject"], "0")

    def test_post_breakout_does_not_reenter_pre_breakout_focus_by_rescanned_stage(self):
        self.assertFalse(is_watching_row({
            "post_breakout_state": "POST_BREAKOUT_HOT",
            "model2_stage": "VCP_MATURE",
            "structure_score": "90",
        }))

    def test_llm_insight_requires_structure_score_of_at_least_70(self):
        self.assertFalse(should_call_llm_insight({"structure_score": 69.99}))
        self.assertTrue(should_call_llm_insight({"structure_score": 70}))
        self.assertTrue(should_call_llm_insight({"structure_score": "75"}))


if __name__ == "__main__":
    unittest.main()
