#!/usr/bin/env python3
"""Regression tests for Bloom status mapping."""

import unittest

from scripts.bloom import base_status, should_call_llm_insight


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

    def test_llm_insight_requires_structure_score_of_at_least_70(self):
        self.assertFalse(should_call_llm_insight({"structure_score": 69.99}))
        self.assertTrue(should_call_llm_insight({"structure_score": 70}))
        self.assertTrue(should_call_llm_insight({"structure_score": "75"}))


if __name__ == "__main__":
    unittest.main()
