#!/usr/bin/env python3
"""Regression tests for market-regime sector lifecycle labels."""

import unittest

import pandas as pd

from scripts.market_regime import classify_sector_state


def prior(ranks, states=None):
    return pd.DataFrame({
        "rank_20": ranks,
        "sector_state": states or ["观察中"] * len(ranks),
    })


def row(rank_20=7, rank_5=12, relative_strength_5=4.827, up_breadth=96.88):
    return {
        "rank_20": rank_20,
        "rank_5": rank_5,
        "relative_strength_5": relative_strength_5,
        "up_breadth": up_breadth,
    }


class SectorStateTests(unittest.TestCase):
    def test_strong_but_not_persistent_is_emerging(self):
        self.assertEqual(classify_sector_state(row(), prior([8, 12, 22, 25, 30])), "强势初现")

    def test_complete_but_directionless_is_watch(self):
        value = row(rank_5=35, relative_strength_5=-0.2, up_breadth=48)
        self.assertEqual(classify_sector_state(value, prior([25, 24, 23, 22, 21])), "观察中")

    def test_fading_requires_prior_lifecycle_evidence(self):
        value = row(rank_5=30, relative_strength_5=-1, up_breadth=40)
        history = prior([8, 9, 10, 11, 12], ["高位分歧", "持续主线", "持续主线", "持续主线", "持续主线"])
        self.assertEqual(classify_sector_state(value, history), "弱势退潮")

    def test_first_weak_day_after_mainline_is_divergence(self):
        value = row(rank_5=30, relative_strength_5=-1, up_breadth=40)
        history = prior([8, 9, 10, 11, 12], ["持续主线"] * 5)
        self.assertEqual(classify_sector_state(value, history), "高位分歧")

    def test_insufficient_history_is_building(self):
        self.assertEqual(classify_sector_state(row(), prior([8, 12, 22, 25])), "历史积累中")


if __name__ == "__main__":
    unittest.main()
