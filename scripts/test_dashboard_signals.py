#!/usr/bin/env python3
"""Regression tests for signal Dashboard market context."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dashboard_signals


class DashboardSignalsMarketNoticeTests(unittest.TestCase):
    def test_defensive_notice_uses_same_day_market_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {
                "market_state": {
                    "label": "弱势下行",
                    "raw_label": "DEFENSIVE",
                    "risk_tags": ["高波动", "市场广度偏弱"],
                }
            }
            (root / "market_context_260730.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(dashboard_signals, "MARKET_CONTEXT_DIR", root):
                notice = dashboard_signals.market_notice("260730")

        self.assertEqual(notice["state"], "DEFENSIVE")
        self.assertEqual(notice["label"], "弱势下行")
        self.assertEqual(notice["tag"], "市场仅观察")
        self.assertEqual(notice["tone"], "blocked")
        self.assertEqual(notice["risk_tags"], ["高波动", "市场广度偏弱"])

    def test_missing_same_day_context_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "market_context_260729.json").write_text(
                json.dumps({"market_state": {"raw_label": "OFFENSIVE"}}), encoding="utf-8"
            )
            with patch.object(dashboard_signals, "MARKET_CONTEXT_DIR", root):
                notice = dashboard_signals.market_notice("260730")

        self.assertEqual(notice["state"], "UNKNOWN")
        self.assertEqual(notice["tag"], "环境待确认")
        self.assertIsNone(notice["source"])


if __name__ == "__main__":
    unittest.main()
