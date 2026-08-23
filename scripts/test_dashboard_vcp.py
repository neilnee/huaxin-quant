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
    def test_compact_candidate_keeps_prior_breakout_reference(self):
        quant = {
            "prior_breakout_bonus_score": 47,
            "prior_breakout_bonus_reasons": ["前序突破", "强势整理形成新VCP"],
            "prior_breakout_context_tag": "之前已有突破并强势整理",
        }

        result = dashboard_vcp.compact_candidate({}, quant, {})

        self.assertEqual(result["prior_breakout_bonus_score"], 47)
        self.assertEqual(result["prior_breakout_bonus_reasons"], ["前序突破", "强势整理形成新VCP"])
        self.assertEqual(result["prior_breakout_context_tag"], "之前已有突破并强势整理")

    def test_compact_candidate_keeps_standard_and_extension_contractions_separate(self):
        quant = {
            "structure_stage": "VCP_EARLY",
            "contraction_count": 1,
            "contraction_group": [{"start_date": "2026-07-27", "end_date": "2026-07-30"}],
            "contraction_extension_tags": [
                "CONFIRMED_RESET_CONTRACTION", "TERMINAL_MICRO_CONTRACTION",
            ],
            "contraction_extension_score": 12,
            "contraction_extensions": [
                {"type": "CONFIRMED_RESET_CONTRACTION", "start_date": "2026-07-16", "end_date": "2026-07-20"},
                {"type": "TERMINAL_MICRO_CONTRACTION", "start_date": "2026-07-31", "end_date": "2026-08-03"},
            ],
        }

        result = dashboard_vcp.compact_candidate({}, quant, {})

        self.assertEqual(result["contraction_count"], 1)
        self.assertEqual(len(result["contractions"]), 1)
        self.assertEqual(len(result["contraction_extensions"]), 2)
        self.assertEqual(result["contraction_extension_score"], 12)

    def test_compact_candidate_defaults_missing_extension_fields_for_old_history(self):
        result = dashboard_vcp.compact_candidate(
            {"contraction_count": "2"},
            {"contraction_group": [{"start_date": "2026-05-01"}]},
            {},
        )

        self.assertEqual(result["contraction_count"], "2")
        self.assertEqual(result["contraction_extension_tags"], [])
        self.assertEqual(result["contraction_extension_score"], 0)
        self.assertEqual(result["contraction_extensions"], [])

    def test_compact_candidate_keeps_destructive_reset_rebuild_audit_separate(self):
        quant = {
            "structure_stage": "TREND_REBUILD",
            "contraction_count": 0,
            "contraction_group": [],
            "destructive_reset": {"peak_date": "2026-07-01", "low_date": "2026-07-20"},
            "destructive_reset_rebuild_ready": False,
            "rebuild_contraction_count": 1,
            "rebuild_contraction_group": [{"start_date": "2026-08-13"}],
        }

        result = dashboard_vcp.compact_candidate({}, quant, {})

        self.assertEqual(result["contractions"], [])
        self.assertEqual(result["rebuild_contraction_count"], 1)
        self.assertEqual(len(result["rebuild_contractions"]), 1)
        self.assertFalse(result["destructive_reset_rebuild_ready"])
        self.assertEqual(result["bloom_status"], "TREND_REBUILD")
        self.assertEqual(result["bloom_signal"], "NONE")
        self.assertIn("尚不计入正式 VCP", result["watch_reason"])
        self.assertIn("MA20 站上 MA60", result["next_watch_point"])
        self.assertEqual(result["llm_insight"], "")

    def test_same_day_pool_source_is_exposed_for_vcp_detail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (root / "pool_260811.csv").open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["股票代码", "pool_channel"])
                writer.writeheader()
                writer.writerow({"股票代码": '="002517"', "pool_channel": "BOTH"})
            with patch.object(dashboard_vcp, "POOL_DIR", root):
                result = dashboard_vcp.load_pool_sources("260811")

        self.assertEqual(result["002517"]["source_label"], "双通道")
        self.assertEqual(result["002517"]["source_tone"], "both")

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

    def test_post_breakout_section_is_published_as_separate_tracking_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bloom_dir = root / "bloom"; quant_dir = root / "quant"
            bloom_dir.mkdir(); quant_dir.mkdir()
            row = {
                "code": "600547", "name": "山东黄金", "bloom_status": "COOLDOWN",
                "post_breakout_state": "POST_BREAKOUT_HOT", "structure_breakout_date": "2026-07-30",
                "structure_breakout_score": "81", "breakout_days": "15", "structure_score": "62",
            }
            bloom = {
                "summary": {"date": "2026-08-20", "status_dist": {"COOLDOWN": 1}},
                "sections": {"active": [], "post_breakout": [row]},
            }
            quant = {"results": [{
                "code": "600547", "name": "山东黄金", "structure_stage": "VCP_FORMING",
                "post_breakout_state": "POST_BREAKOUT_HOT", "structure_breakout_date": "2026-07-30",
                "structure_breakout_score": 81, "breakout_days": 15,
                "structure_breakout_level": 27.33, "structure_score": 62,
            }]}
            (bloom_dir / "bloom_input_260820.json").write_text(json.dumps(bloom, ensure_ascii=False), encoding="utf-8")
            (quant_dir / "quant_260820.json").write_text(json.dumps(quant, ensure_ascii=False), encoding="utf-8")
            with patch.object(dashboard_vcp, "BLOOM_INPUT_DIR", bloom_dir), patch.object(
                dashboard_vcp, "QUANT_RUN_DIR", quant_dir
            ), patch.object(dashboard_vcp, "realized_events_for_date", return_value=[]), patch.object(
                dashboard_vcp, "load_industry_context", return_value={}
            ):
                result = dashboard_vcp.build_context("260820")

        self.assertEqual(result["summary"]["pre_breakout_total"], 0)
        self.assertEqual(result["summary"]["post_breakout_total"], 1)
        self.assertEqual(result["candidates"][0]["tracking_scope"], "POST_BREAKOUT")
        self.assertEqual(result["candidates"][0]["post_breakout_state"], "POST_BREAKOUT_HOT")
        self.assertEqual(result["candidates"][0]["structure_breakout_score"], 81)

    def test_pre_breakout_focus_reuses_bloom_watching_section(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bloom_dir = root / "bloom"; quant_dir = root / "quant"
            bloom_dir.mkdir(); quant_dir.mkdir()
            focus = {
                "code": "600000", "name": "浦发银行", "bloom_status": "FORMING",
                "model2_stage": "VCP_FORMING", "structure_score": "60",
            }
            all_only = {
                "code": "600001", "name": "邯郸钢铁", "bloom_status": "EARLY",
                "model2_stage": "VCP_EARLY", "structure_score": "59",
            }
            bloom = {
                "summary": {"date": "2026-08-21"},
                "sections": {"active": [focus, all_only], "watching": [focus]},
            }
            (bloom_dir / "bloom_input_260821.json").write_text(
                json.dumps(bloom, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(dashboard_vcp, "BLOOM_INPUT_DIR", bloom_dir), patch.object(
                dashboard_vcp, "QUANT_RUN_DIR", quant_dir
            ), patch.object(dashboard_vcp, "realized_events_for_date", return_value=[]), patch.object(
                dashboard_vcp, "load_industry_context", return_value={}
            ), patch.object(dashboard_vcp, "load_pool_sources", return_value={}):
                result = dashboard_vcp.build_context("260821")

        by_code = {row["code"]: row for row in result["candidates"]}
        self.assertTrue(by_code["600000"]["pre_breakout_focus"])
        self.assertFalse(by_code["600001"]["pre_breakout_focus"])
        self.assertEqual(result["summary"]["pre_breakout_focus_total"], 1)
        self.assertEqual(result["summary"]["pre_breakout_total"], 2)

    def test_vcp_page_has_focus_all_tabs_and_independent_scroll_containers(self):
        dashboard = Path(__file__).resolve().parents[1] / "dashboard"
        app = (dashboard / "app.js").read_text(encoding="utf-8")
        page = (dashboard / "index.html").read_text(encoding="utf-8")
        css = (dashboard / "vcp.css").read_text(encoding="utf-8")

        self.assertIn('["PRE_BREAKOUT_FOCUS","突破前跟踪"]', app)
        self.assertIn('["PRE_BREAKOUT_ALL","全部"]', app)
        self.assertIn('["TREND_REBUILD","趋势重建"]', app)
        self.assertIn("vcpDestructiveResetPanel", app)
        self.assertIn("rebuild_contractions", app)
        self.assertNotIn('$("vcp-detail").scrollTop=0', app)
        self.assertIn('id="vcp-list-scroll"', page)
        self.assertNotIn("vcp-detail-scroll", page)
        self.assertIn("max-height:1236px", css)
        self.assertIn("#vcp-table tbody tr{height:60px}", css)

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
