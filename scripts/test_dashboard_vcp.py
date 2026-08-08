#!/usr/bin/env python3
"""Regression tests for historical VCP dashboard publishing."""

import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dashboard_vcp


class DashboardVcpIndustryContextTests(unittest.TestCase):
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
