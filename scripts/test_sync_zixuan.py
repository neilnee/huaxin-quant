#!/usr/bin/env python3
"""Regression tests for Bloom-managed watchlist selection."""

import unittest

from scripts.sync_zixuan import target_row


def sample_row(**overrides):
    row = {
        "code": "600547", "name": "山东黄金", "last_seen": "2026-08-20",
        "model2_stage": "VCP_FORMING", "structure_score": "10",
        "model2_setup_signal": "NONE", "bloom_status": "COOLDOWN",
        "post_breakout_state": "",
    }
    row.update(overrides)
    return row


class SyncZixuanTargetTests(unittest.TestCase):
    def test_active_post_breakout_state_is_selected_without_stage_score(self):
        result = target_row(sample_row(post_breakout_state="POST_BREAKOUT_HOT"), "2026-08-20")
        self.assertEqual(result["selection"], "POST_BREAKOUT_TRACKING")

    def test_terminal_post_breakout_state_is_not_selected_by_rescanned_stage(self):
        result = target_row(sample_row(
            post_breakout_state="POST_BREAKOUT_EXPIRED", structure_score="80",
        ), "2026-08-20")
        self.assertIsNone(result)


if __name__ == "__main__":
    unittest.main()
