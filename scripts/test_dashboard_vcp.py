#!/usr/bin/env python3
"""Regression tests for historical VCP dashboard publishing."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dashboard_vcp


class DashboardVcpIndustryContextTests(unittest.TestCase):
    def test_plan_hit_does_not_add_a_vcp_display_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bloom_dir = root / "bloom"; quant_dir = root / "quant"
            bloom_dir.mkdir(); quant_dir.mkdir()
            bloom = {
                "summary": {"date": "2026-08-10", "status_dist": {"COOLDOWN": 1}},
                "sections": {
                    "active": [],
                    "cooldown": [{
                        "code": "603882", "name": "金域医学", "bloom_status": "COOLDOWN",
                        "model2_setup_signal": "NONE", "structure_score": "62",
                    }],
                },
            }
            quant = {"results": [{
                "code": "603882", "name": "金域医学", "structure_stage": "VCP_FORMING",
                "setup_signal": "NONE", "structure_score": 62,
            }]}
            (bloom_dir / "bloom_input_260810.json").write_text(json.dumps(bloom, ensure_ascii=False), encoding="utf-8")
            (quant_dir / "quant_260810.json").write_text(json.dumps(quant, ensure_ascii=False), encoding="utf-8")
            event = {
                "code": "603882", "setup_type": "BREAKOUT_BUY", "entry_grade": "REGULAR",
                "plan_date": "2026-08-07",
            }
            with patch.object(dashboard_vcp, "BLOOM_INPUT_DIR", bloom_dir), patch.object(
                dashboard_vcp, "QUANT_RUN_DIR", quant_dir
            ), patch.object(dashboard_vcp, "realized_events_for_date", return_value=[event]), patch.object(
                dashboard_vcp, "load_industry_context", return_value={}
            ):
                result = dashboard_vcp.build_context("260810")

        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["summary"]["plan_hit_total"], 0)
        self.assertEqual(result["summary"]["status_dist"]["TRIGGERED"], 0)

    def test_native_trigger_can_be_annotated_with_previous_plan_hit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bloom_dir = root / "bloom"; quant_dir = root / "quant"
            bloom_dir.mkdir(); quant_dir.mkdir()
            row = {
                "code": "603882", "name": "金域医学", "bloom_status": "TRIGGERED",
                "model2_setup_signal": "BREAKOUT_BUY", "structure_score": "62",
            }
            bloom = {
                "summary": {"date": "2026-08-10", "status_dist": {"TRIGGERED": 1}},
                "sections": {"active": [row]},
            }
            quant = {"results": [{
                "code": "603882", "name": "金域医学", "structure_stage": "VCP_FORMING",
                "setup_signal": "BREAKOUT_BUY", "structure_score": 62,
            }]}
            (bloom_dir / "bloom_input_260810.json").write_text(json.dumps(bloom, ensure_ascii=False), encoding="utf-8")
            (quant_dir / "quant_260810.json").write_text(json.dumps(quant, ensure_ascii=False), encoding="utf-8")
            event = {"code": "603882", "setup_type": "BREAKOUT_BUY", "plan_date": "2026-08-07"}
            with patch.object(dashboard_vcp, "BLOOM_INPUT_DIR", bloom_dir), patch.object(
                dashboard_vcp, "QUANT_RUN_DIR", quant_dir
            ), patch.object(dashboard_vcp, "realized_events_for_date", return_value=[event]), patch.object(
                dashboard_vcp, "load_industry_context", return_value={}
            ):
                result = dashboard_vcp.build_context("260810")

        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["candidates"][0]["previous_plan_hit"])
        self.assertEqual(result["candidates"][0]["bloom_status"], "TRIGGERED")
        self.assertEqual(result["summary"]["plan_hit_total"], 1)

    def test_same_day_market_csv_enriches_historical_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (root / "stock_strength_260506.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["code", "sw_l2_name"])
                writer.writeheader()
                writer.writerow({"code": "000001", "sw_l2_name": "银行"})
            with (root / "sector_heat_260506.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["block_type", "block_name", "sector_state", "history_basis"])
                writer.writeheader()
                writer.writerow({
                    "block_type": "industry_sw_l2", "block_name": "银行",
                    "sector_state": "持续主线", "history_basis": "current_snapshot_backfill",
                })
            with patch.object(dashboard_vcp, "MARKET_OUTPUT_DIR", root):
                result = dashboard_vcp.load_industry_context(["000001"], "2026-05-06")

        self.assertEqual(result["000001"]["sw_l2_name"], "银行")
        self.assertEqual(result["000001"]["sector"]["sector_state"], "持续主线")
        self.assertEqual(result["000001"]["sector"]["history_basis"], "current_snapshot_backfill")

    def test_market_csv_does_not_fall_back_to_another_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stock_strength_260507.csv").write_text("code,sw_l2_name\n000001,银行\n", encoding="utf-8")
            (root / "sector_heat_260507.csv").write_text(
                "block_type,block_name,sector_state\nindustry_sw_l2,银行,持续主线\n", encoding="utf-8"
            )
            with patch.object(dashboard_vcp, "MARKET_OUTPUT_DIR", root), patch.object(
                dashboard_vcp, "MARKET_DATA_DB", root / "missing.sqlite"
            ), patch.object(dashboard_vcp, "MARKET_REGIME_DB", root / "missing_state.sqlite"):
                result = dashboard_vcp.load_industry_context(["000001"], "2026-05-06")

        self.assertEqual(result, {})


if __name__ == "__main__":
    unittest.main()
