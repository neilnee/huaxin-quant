#!/usr/bin/env python3
"""Regression tests for Bloom-managed watchlist selection."""

import unittest

from scripts.sync_zixuan import target_row


def sample_row(**overrides):
    row = {
        "code": "600547", "name": "山东黄金", "last_seen": "2026-08-20",
        "model2_stage": "VCP_FORMING", "structure_score": "10",
        "model2_setup_signal": "NONE", "bloom_status": "COOLDOWN",
        "post_breakout_state": "", "structure_breakout_score": "",
    }
    row.update(overrides)
    return row


class SyncZixuanTargetTests(unittest.TestCase):
    def test_active_post_breakout_state_is_selected_without_stage_score(self):
        result = target_row(sample_row(
            post_breakout_state="POST_BREAKOUT_HOT", structure_breakout_score="81",
        ), "2026-08-20")
        self.assertEqual(result["selection"], "POST_BREAKOUT_TRACKING")
        self.assertEqual(result["structure_breakout_score"], "81")

    def test_terminal_post_breakout_state_is_not_selected_by_rescanned_stage(self):
        result = target_row(sample_row(
            post_breakout_state="POST_BREAKOUT_EXPIRED", structure_score="80",
        ), "2026-08-20")
        self.assertIsNone(result)

    def test_low_frozen_score_post_breakout_is_not_selected_by_rescanned_stage(self):
        result = target_row(sample_row(
            post_breakout_state="POST_BREAKOUT_RETEST", structure_breakout_score="54",
            structure_score="90",
        ), "2026-08-20")
        self.assertIsNone(result)

    def test_triggered_is_selected_without_score_gate(self):
        result = target_row(sample_row(
            bloom_status="TRIGGERED", model2_setup_signal="PULLBACK_BUY",
        ), "2026-08-20")
        self.assertEqual(result["selection"], "SETUP_TRIGGER")

    def test_excluded_bloom_statuses_override_stage_and_trigger(self):
        for status in ("RISK_BLOCKED", "DATA_ISSUE", "INVALID", "EXIT"):
            with self.subTest(status=status):
                result = target_row(sample_row(
                    bloom_status=status, model2_stage="VCP_TIGHT",
                    structure_score="99", model2_setup_signal="BREAKOUT_BUY",
                ), "2026-08-20")
                self.assertIsNone(result)

    def test_mature_bloom_status_is_selected_without_score_gate(self):
        result = target_row(sample_row(
            bloom_status="MATURE", model2_stage="VCP_TIGHT",
        ), "2026-08-20")
        self.assertEqual(result["selection"], "FOCUS")

    def test_forming_requires_score_70(self):
        self.assertIsNone(target_row(sample_row(
            bloom_status="FORMING", structure_score="69.99",
        ), "2026-08-20"))
        result = target_row(sample_row(
            bloom_status="FORMING", structure_score="70",
        ), "2026-08-20")
        self.assertEqual(result["selection"], "FOCUS")

    def test_early_requires_score_75(self):
        self.assertIsNone(target_row(sample_row(
            bloom_status="EARLY", model2_stage="VCP_EARLY", structure_score="74.99",
        ), "2026-08-20"))
        result = target_row(sample_row(
            bloom_status="EARLY", model2_stage="VCP_EARLY", structure_score="75",
        ), "2026-08-20")
        self.assertEqual(result["selection"], "FOCUS")

    def test_post_breakout_requires_cooldown_and_score_60(self):
        self.assertIsNone(target_row(sample_row(
            bloom_status="COOLDOWN", post_breakout_state="POST_BREAKOUT_HOT",
            structure_breakout_score="59.99",
        ), "2026-08-20"))
        result = target_row(sample_row(
            bloom_status="COOLDOWN", post_breakout_state="POST_BREAKOUT_HOT",
            structure_breakout_score="60",
        ), "2026-08-20")
        self.assertEqual(result["selection"], "POST_BREAKOUT_TRACKING")


if __name__ == "__main__":
    unittest.main()
