#!/usr/bin/env python3
"""Focused tests for isolated historical environment refresh helpers."""

import tempfile
import unittest
from pathlib import Path

from scripts import rebuild_market_history


class RebuildMarketHistoryTests(unittest.TestCase):
    def test_normalize_stamp(self):
        self.assertEqual(rebuild_market_history.normalize_stamp("2026-08-11"), "260811")
        self.assertEqual(rebuild_market_history.normalize_stamp("260811"), "260811")

    def test_workspace_must_stay_under_project_tmp(self):
        with self.assertRaisesRegex(ValueError, "项目 .tmp"):
            rebuild_market_history.validate_workspace(Path("/tmp"))

    def test_context_javascript_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "signals_context_260811.js"
            payload = {"market_notice": {"state": "RECOVERY_WATCH"}, "signals": []}
            rebuild_market_history.write_context_js(path, "QUANT_DASHBOARD_SIGNALS_CONTEXTS", payload)
            self.assertEqual(rebuild_market_history.read_context_js(path), payload)

    def test_refresh_markdown_state_preserves_body(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "old.md"
            target = root / "new.md"
            source.write_text("# 日报\n\n**市场状态：SELECTIVE**  \n\n保留正文\n", encoding="utf-8")
            report = {"state": {"confirmed_state": "CONSOLIDATING"}}
            rebuild_market_history.refresh_markdown_state(source, target, report)
            self.assertIn("**市场状态：弱势震荡**", target.read_text(encoding="utf-8"))
            self.assertIn("保留正文", target.read_text(encoding="utf-8"))

    def test_merge_sector_fields_only_updates_environment(self):
        target = {"block_name": "医疗服务", "relative_strength_20": 3.2}
        source = {
            "rank_pct_20": 0.1, "rank_pct_5": 0.2, "advance_ratio_5": None,
            "sector_state": "持续主线", "sector_phase": "主线", "sector_health": "主线降温",
            "sector_health_level": -1, "sector_health_score": -0.45, "short_pulse": 0,
            "sector_policy_tier": "A", "data_status": "READY", "history_basis": "point_in_time",
        }
        rebuild_market_history._merge_sector_fields(target, source)
        self.assertEqual(target["sector_phase"], "主线")
        self.assertEqual(target["sector_health_level"], -1)
        self.assertEqual(target["relative_strength_20"], 3.2)

    def test_reclassify_market_state_uses_rebuilt_sector_evidence(self):
        report = {
            "state": {
                "current": "SELECTIVE", "raw_state": "SELECTIVE", "trend_score": 55,
                "volatility_score": 50, "breadth_score": 55, "rotation_score": 50,
            },
            "benchmarks": {
                str(index): {"above_ma20": True, "above_ma60": True} for index in range(6)
            },
            "breadth": {"advance_ratio": 48},
            "llm": {"analysis": "保留原结论"},
        }
        rebuild_market_history.reclassify_market_state(report, [], [])
        self.assertEqual(report["state"]["raw_state"], "CONSOLIDATING")
        self.assertFalse(report["state"]["local_opportunity"])
        self.assertEqual(report["llm"]["analysis"], "保留原结论")

        sectors = [
            {"block_type": "industry_sw_l2", "block_name": "医疗服务", "sector_phase": "转强", "data_status": "READY", "relative_strength_20": 2, "relative_strength_5": 1, "above_ma20_ratio": 70},
            {"block_type": "gn", "block_name": "创新药", "sector_phase": "主线", "data_status": "READY", "relative_strength_20": 3, "relative_strength_5": 2, "above_ma20_ratio": 75},
        ]
        rebuild_market_history.reclassify_market_state(report, sectors, [])
        self.assertEqual(report["state"]["raw_state"], "SELECTIVE")
        self.assertEqual(report["state"]["local_opportunity_basis"], "cross_level_strength")


if __name__ == "__main__":
    unittest.main()
