#!/usr/bin/env python3
"""Regression tests for the deterministic daily AI report publisher."""

import json
import tempfile
import unittest
from pathlib import Path

from scripts import daily_ai_report


def contexts():
    return {
        "market": {
            "meta": {"run_date": "2026-08-17", "strategy_version": "market_v1", "data_status": "VALID"},
            "market_state": {
                "label": "弱势震荡",
                "analysis": "宽基指数全部站上MA20。继续观察更多宽基能否重回 MA20 上方。",
                "analysis_source": "llm",
            },
            "market_state_explainer": {"current": {"metrics": []}},
            "indexes": {"csi300": {"return_1": 1.2, "above_ma20": True, "above_ma60": False}},
            "breadth": {"all_a": {"advance_ratio": 60}},
            "daily_mainline": {"status": "clear", "name": "测试主线", "provider": "deepseek"},
            "sector_rankings": {
                "industry_sw_l1": [{"block_type": "industry_sw_l1", "block_name": "电子", "rank_20": 1}],
                "industry_sw_l2": [], "gn": [], "fg": [],
            },
            "sector_history": {"industry_sw_l1": [{"date": "2026-08-16"}]},
            "sector_rank_matrix": {"industry_sw_l1": [{"date": "2026-08-16"}]},
            "sector_leaders": {
                "industry_sw_l1:电子": [{"code": "000001", "name": "代表股"}],
                "industry_sw_l1:未排名": [{"code": "000002", "name": "不应输出"}],
            },
        },
        "capital": {
            "meta": {"trade_date": "2026-08-17", "status": "complete", "errors": []},
            "summary": {"candidate_count": 1},
            "thresholds": {"entry_rules": {}},
            "sectors": [{"sw_code": "X1", "sw_name": "电子", "top_stocks": [{"code": "000001"}]}],
        },
        "vcp": {
            "meta": {"run_date": "2026-08-17"},
            "summary": {
                "quant_stats": {"pull_fail": 0},
                "strategy_file": "/Users/example/huaxin_quant/strategies/04-bloom.json",
                "source_status_dist": {"FORMING": 2, "EXIT": 3},
                "status_dist": {"FORMING": 1},
            },
            "candidates": [{
                "code": "000001",
                "bloom_status": "FORMING",
                "structure_score": 70,
                "days_tracked": "3",
                "valuation_candidate": "true",
                "contractions": [{"start_date": "2026-07-01", "end_date": "2026-07-10"}],
                "sector": {
                    "block_name": "电子",
                    "member_count": "120",
                    "return_20": "17.196",
                    "rank_20": "2",
                    "sector_health_level": "2",
                    "short_pulse": "False",
                },
            }],
        },
        "signals": {
            "meta": {"run_date": "2026-08-17", "capital_errors": []},
            "market_notice": {"label": "弱势震荡"},
            "summary": {"total": 1},
            "signals": [{
                "code": "000001",
                "signal_kind": "TRIGGERED",
                "setup_signal": "PULLBACK_BUY",
                "pivot_price": 10.0,
                "support_price": 10.0,
                "invalid_price": 9.7,
                "setup_plan_inputs": {
                    "breakout": {"trigger_price": 10.1, "invalid_price": 9.7},
                    "pullback": {"support_price": 8.5, "invalid_price": 7.0},
                    "retest": {},
                },
            }],
        },
    }


class DailyAiReportTests(unittest.TestCase):
    def test_build_report_keeps_current_rankings_and_excludes_history(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )

        rankings = report["sector_strength_rankings"]
        row = rankings["categories"]["industry_sw_l1"][0]
        self.assertEqual(row["representative_stocks"], [{"code": "000001", "name": "代表股"}])
        self.assertNotIn("sector_history", rankings)
        self.assertNotIn("sector_rank_matrix", rankings)
        self.assertNotIn("backtest", report)
        self.assertNotIn("valuation", report)

    def test_build_report_normalizes_machine_values_and_counts(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )

        self.assertEqual(report["content_index"]["sector_ranking_items"], 1)
        self.assertEqual(report["content_index"]["capital_sectors"], 1)
        self.assertEqual(report["content_index"]["vcp_structures"], 1)
        self.assertEqual(report["content_index"]["trading_signals"], 1)
        self.assertEqual(report["vcp_structures"]["items"][0]["days_tracked"], 3)
        self.assertIs(report["vcp_structures"]["items"][0]["valuation_candidate"], True)
        self.assertEqual(report["vcp_structures"]["summary"]["strategy_file"], "04-bloom.json")
        daily_ai_report.validate_report(report)

    def test_vcp_sector_types_and_status_count_basis_are_explicit(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )
        vcp = report["vcp_structures"]
        sector = vcp["items"][0]["sector"]

        self.assertEqual(sector["member_count"], 120)
        self.assertIsInstance(sector["member_count"], int)
        self.assertEqual(sector["return_20"], 17.196)
        self.assertIsInstance(sector["return_20"], float)
        self.assertIs(sector["short_pulse"], False)
        counts = vcp["summary"]["status_counts"]
        self.assertNotIn("source_status_dist", vcp["summary"])
        self.assertEqual(counts["source_lifecycle_snapshot"]["total"], 5)
        self.assertIs(counts["source_lifecycle_snapshot"]["mutually_exclusive"], True)
        self.assertEqual(counts["display_vcp_items"]["total"], 1)

    def test_legacy_vcp_status_dist_is_source_scope_and_display_is_recounted(self):
        payloads = contexts()
        summary = payloads["vcp"]["summary"]
        summary.pop("source_status_dist")
        summary["status_dist"] = {"FORMING": 7, "COOLDOWN": 10, "EXIT": 3}
        report = daily_ai_report.build_report(
            payloads, {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )
        counts = report["vcp_structures"]["summary"]["status_counts"]

        self.assertEqual(counts["source_lifecycle_snapshot"]["total"], 20)
        self.assertEqual(counts["display_vcp_items"]["total"], 1)
        self.assertEqual(counts["display_vcp_items"]["counts"]["FORMING"], 1)

    def test_invalid_declared_vcp_sector_type_fails(self):
        payloads = contexts()
        payloads["vcp"]["candidates"][0]["sector"]["short_pulse"] = "maybe"

        with self.assertRaisesRegex(ValueError, "invalid boolean machine value"):
            daily_ai_report.build_report(
                payloads, {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
            )

    def test_signal_prices_are_scoped_to_trade_plan(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )
        signal = report["trading_signals"]["items"][0]

        self.assertNotIn("support_price", signal)
        self.assertNotIn("invalid_price", signal)
        semantics = signal["trade_price_semantics"]
        self.assertEqual(semantics["matched_plan"], "pullback")
        self.assertEqual(semantics["structure"]["pivot_price"], 10.0)
        self.assertEqual(semantics["plans"]["breakout"]["invalid_price"], 9.7)
        self.assertEqual(semantics["plans"]["pullback"]["support_price"], 8.5)
        self.assertEqual(semantics["plans"]["pullback"]["invalid_price"], 7.0)
        self.assertIs(semantics["canonical_for_ai_and_downstream"], True)

    def test_signal_and_structure_identifiers_are_stable_and_linked(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )
        structure = report["vcp_structures"]["items"][0]
        signal = report["trading_signals"]["items"][0]

        self.assertEqual(structure["structure_anchor"], "2026-07-01")
        self.assertEqual(structure["structure_id"], "000001:2026-07-01")
        self.assertEqual(signal["record_role"], "TODAY_TRIGGER")
        self.assertEqual(signal["parent_structure_id"], structure["structure_id"])
        self.assertEqual(
            signal["signal_id"],
            "000001:2026-08-17:TODAY_TRIGGER:PULLBACK_BUY",
        )
        self.assertIn("OBSERVE_QUALITY", report["dictionaries"]["enums"]["position_status"])

    def test_market_llm_fact_conflict_is_reported_without_rewriting_analysis(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )

        market = report["market_trend"]
        validation = market["llm_validation"]
        self.assertEqual(validation["status"], "WARNING")
        self.assertIs(validation["analysis_safe_to_use"], False)
        self.assertEqual(validation["structured_facts"]["above_ma20_count"], 1)
        self.assertEqual(
            validation["fact_conflicts"][0]["code"],
            "ALL_ABOVE_MA20_BUT_MORE_RECOVERY_REQUESTED",
        )
        self.assertIn("更多宽基", market["state"]["analysis"])
        self.assertTrue(any(row["module"] == "market_llm_validation" for row in report["data_quality"]["warnings"]))

    def test_market_llm_validator_does_not_cross_match_ma20_and_ma60_clauses(self):
        payloads = contexts()
        payloads["market"]["market_state"]["analysis"] = (
            "宽基指数全部站上MA20（1/1），但无一站上MA60。"
            "继续观察更多宽基能否重回 MA20 上方。"
        )
        validation = daily_ai_report.market_llm_validation(payloads["market"])

        self.assertEqual(validation["status"], "WARNING")
        self.assertIs(validation["analysis_safe_to_use"], False)
        self.assertEqual(len(validation["fact_conflicts"]), 1)
        self.assertEqual(
            validation["fact_conflicts"][0]["code"],
            "ALL_ABOVE_MA20_BUT_MORE_RECOVERY_REQUESTED",
        )

    def test_extract_context_and_publish_are_date_scoped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_dir = root / "data"
            month = data_dir / "202608"
            month.mkdir(parents=True)
            payloads = contexts()
            for module, (variable, _) in daily_ai_report.MODULES.items():
                path = month / f"{module}_context_260817.js"
                path.write_text(
                    f"window.{variable} = window.{variable} || {{}};\n"
                    f"window.{variable}[\"260817\"] = {json.dumps(payloads[module], ensure_ascii=False)};\n",
                    encoding="utf-8",
                )
            output = daily_ai_report.publish("2026-08-17", data_dir, root / "reports")
            report = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(report["schema_version"], "huaxin_ai_daily_v1.1")
        self.assertEqual(report["report_date"], "2026-08-17")
        self.assertEqual(output.parent.name, "202608")
        self.assertEqual(output.name, "huaxin_quant_ai_report_260817.json")

    def test_volume_units_are_path_scoped_and_use_lots(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )
        dictionaries = report["dictionaries"]
        units = dictionaries["units_by_path"]

        self.assertNotIn("volume", dictionaries["units"])
        self.assertEqual(units["trading_signals.items[]"]["unit"], "lot")
        self.assertEqual(units["trading_signals.items[]"]["lot_size_shares"], 100)
        self.assertIn(
            "volume_floor_threshold",
            units["trading_signals.items[].plan_inputs"]["fields"],
        )
        self.assertIn(
            "vcp_structures.items[].contractions[]",
            units,
        )
        daily_ai_report.validate_report(report)

    def test_undocumented_absolute_volume_field_fails_validation(self):
        report = daily_ai_report.build_report(
            contexts(), {key: f"{key}.js" for key in daily_ai_report.MODULES}, "260817", "2026-08-17"
        )
        report["trading_signals"]["items"][0]["plan_inputs"] = {
            "mystery_volume_limit": 12345,
        }

        with self.assertRaisesRegex(ValueError, "undocumented nested signal volume fields"):
            daily_ai_report.validate_report(report)

    def test_volume_vs_fields_are_ratios_not_absolute_volume(self):
        fields = daily_ai_report.numeric_absolute_volume_fields({
            "volume_vs_breakout": 0.75,
            "volume_vs_previous_avg": 1.2,
            "breakout_volume": 50000,
        })

        self.assertEqual(fields, {"breakout_volume"})

    def test_missing_required_context_fails_without_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(daily_ai_report.ReportInputError):
                daily_ai_report.publish("260817", root / "data", root / "reports")
            self.assertFalse((root / "reports").exists())


if __name__ == "__main__":
    unittest.main()
