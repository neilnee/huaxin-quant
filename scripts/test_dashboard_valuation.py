#!/usr/bin/env python3
"""Regression tests for the Model 3 Dashboard publishing contract."""

import unittest

from scripts.dashboard_valuation import (
    business_map_with_coverage,
    catalog_row,
    company_summary_for_display,
    institution_profit_analysis,
    legacy_stage_two_baseline,
    merge_priced_independent_items,
    primary_pillar,
    scenario_per_share,
)


class DashboardValuationTest(unittest.TestCase):
    def test_company_summary_flattens_sourced_fields_for_display(self):
        result = company_summary_for_display({"company_summary": {
            "company_profile": {"text": "主营数据中心服务", "source_ids": ["s1"]},
            "earnings_consensus": {"text": "2026E中位利润10亿元", "source_ids": ["s2"]},
        }})
        self.assertEqual(result["company_profile"], "主营数据中心服务")
        self.assertEqual(result["earnings_consensus"], "2026E中位利润10亿元")
        self.assertEqual(result["tracking_focus"], "")

    def test_legacy_profit_analysis_is_explicitly_endpoint_only(self):
        result = institution_profit_analysis({"institution_forecasts": [{
            "institution": "示例证券", "report_date": "2026-07-01",
            "net_profit_2026e": 10, "net_profit_2027e": 12,
            "profit_scope": "recurring", "source_ids": ["s1"],
        }]})
        self.assertEqual(result[0]["profit_logic"]["status"], "endpoint_only")
        self.assertEqual(
            [step["value"] for step in result[0]["profit_logic"]["steps"]], [10, 12],
        )

    def test_business_map_joins_stage_two_coverage_by_stable_pillar_id(self):
        stage_one = {"business_pillars": [{
            "pillar_id": "p1", "name": "在建产能", "source_ids": ["stage1"],
        }]}
        stage_two = {"business_item_coverage": [{
            "source_pillar_id": "p1", "valuation_role": "merged_component",
            "overlap_status": "inside_target", "reason": "多数机构已纳入核心经营预测",
            "source_ids": ["institution"],
        }]}
        result = business_map_with_coverage(stage_one, stage_two)
        self.assertEqual(result[0]["institution_coverage"], "多数机构已纳入核心经营预测")
        self.assertEqual(result[0]["institution_coverage_status"], "included")
        self.assertEqual(result[0]["source_ids"], ["stage1", "institution"])
        self.assertNotIn("institution_coverage", stage_one["business_pillars"][0])

    def test_business_map_joins_value_profile_by_stage_one_pillar_id(self):
        stage_one = {"business_pillars": [{"pillar_id": "p1", "name": "成熟机房"}]}
        card = {"business_pillar_analysis": [{
            "source_pillar_id": "p1",
            "role_in_company": {"text": "公司当前经常性利润基础"},
        }]}
        result = business_map_with_coverage(stage_one, {}, card)
        self.assertEqual(
            result[0]["business_value_profile"]["role_in_company"]["text"],
            "公司当前经常性利润基础",
        )
        self.assertNotIn("business_value_profile", stage_one["business_pillars"][0])

    def test_business_map_keeps_fallback_only_when_historical_coverage_is_missing(self):
        result = business_map_with_coverage(
            {"business_pillars": [{"pillar_id": "legacy", "name": "旧业务"}]}, {},
        )
        self.assertNotIn("institution_coverage", result[0])

    def test_business_map_marks_realized_history_as_excluded_for_legacy_roles(self):
        result = business_map_with_coverage(
            {"business_pillars": [{
                "pillar_id": "history", "name": "历史处置收益",
                "economic_nature": "historical_realized",
            }]},
            {"business_item_coverage": [{
                "source_pillar_id": "history", "valuation_role": "valued_event",
                "overlap_status": "not_applicable", "reason": "未来年度无价值贡献",
            }]},
        )
        self.assertEqual(result[0]["institution_coverage_status"], "historical_excluded")

    def test_scenario_per_share_builds_dual_year_summary_values(self):
        scenario = scenario_per_share(
            {"pessimistic": 420, "base": 500, "optimistic": 620},
            shares=5,
            current_price=80,
        )
        self.assertEqual(scenario["pessimistic"], 84)
        self.assertEqual(scenario["base"], 100)
        self.assertEqual(scenario["optimistic"], 124)
        self.assertEqual(scenario["base_upside_pct"], 25)

    def test_primary_pillar_uses_largest_base_market_value(self):
        result = {
            "matrix_2026e": {
                "matrix_rows": [
                    {"pillar_name": "成熟业务", "pillar_total": {"base": 120}},
                    {"pillar_name": "增长业务", "pillar_total": {"base": 260}},
                ]
            }
        }
        self.assertEqual(primary_pillar(result), {
            "name": "增长业务", "base_value": 260.0, "unit": "亿元市值",
        })

    def test_catalog_row_contains_routes_but_not_full_research(self):
        report = {
            "code": "688676", "name": "金盘科技", "run_id": "688676_20260723_101826",
            "run_at": "2026-07-23T10:18:26", "analysis_date": "260723",
            "valuation": {"base": 105.41}, "status": {"run": "done"},
            "research_stats": {"evidence_count": 195}, "decision_summary": {},
            "facts": [{"statement": "不应进入目录"}],
        }
        row = catalog_row(report)
        self.assertEqual(row["report_path"], "data/valuation/reports/688676_20260723_101826.js")
        self.assertNotIn("evidence_path", row)
        self.assertNotIn("facts", row)

    def test_legacy_baseline_uses_layer_one_without_later_adjustments(self):
        card = {"business_pillars": [{
            "name": "主营", "research_id": "p1", "source_ids": ["s1"],
            "profit_bridge": {"profit_metric": "net_profit", "items_2026e": [], "items_2027e": []},
        }]}
        params = {"pillars": [{
            "name": "主营", "route": "A1", "consensus_np_2026e": 10,
            "consensus_np_lower_2026e": 9, "consensus_np_upper_2026e": 11,
            "consensus_np_2027e": 12, "consensus_np_lower_2027e": 11, "consensus_np_upper_2027e": 13,
        }]}
        result = {
            "matrix_2026e": {"matrix_rows": [{"pillar_name": "主营", "layer1": {"pessimistic": 270, "base": 350, "optimistic": 440}, "layer2": {"base": 999}}]},
            "matrix_2027e": {"matrix_rows": [{"pillar_name": "主营", "layer1": {"pessimistic": 330, "base": 420, "optimistic": 520}}]},
        }
        baseline = legacy_stage_two_baseline(card, params, result)
        self.assertEqual(baseline["totals"]["2026e"]["base"], 350)
        self.assertEqual(baseline["pillars"][0]["years"]["2026e"]["base"]["multiple"], 35)

    def test_legacy_assumptions_distinguish_priced_drivers_from_unpriced_inputs(self):
        card = {"business_pillars": [{
            "name": "主营", "research_id": "p1", "source_ids": ["s1"],
            "profit_bridge": {
                "items_2026e": [
                    {"item": "已计入项目", "profit_impact": 2.5, "source_ids": ["s1"]},
                    {"item": "待量化项目", "profit_impact": 0, "source_ids": ["s1"]},
                ],
                "items_2027e": [],
            },
            "valuation_drivers": ["行业地位"],
        }]}
        params = {"pillars": [{"name": "主营"}]}
        baseline = legacy_stage_two_baseline(card, params, {})
        assumptions = baseline["assumption_ledger"]
        self.assertEqual(
            [(item["included_in_baseline"], item["pricing_channel"], item["quantification_status"])
             for item in assumptions],
            [(True, "profit", "quantitative"), (False, "none", "missing_input"),
             (True, "multiple", "qualitative")],
        )

    def test_priced_independent_item_is_merged_as_its_own_pillar(self):
        baseline = {
            "pillars": [], "assumption_ledger": [],
            "totals": {year: {scenario: 0 for scenario in ("pessimistic", "base", "optimistic")}
                       for year in ("2026e", "2027e")},
        }
        card = {"narrative_options": [{
            "research_id": "reit_followon", "name": "REIT扩募", "included_in_valuation": True,
            "business_essence": "扩募注入资产", "source_ids": ["s1"],
        }]}
        params = {"pillars": [{"type_b_pipeline": [{
            "name": "REIT扩募", "project_profit": 65, "pe": 1, "probability": 0.6,
        }]}]}
        model = merge_priced_independent_items(baseline, card, params)
        self.assertEqual(model["pillars"][0]["pillar_type"], "independent_event")
        self.assertEqual(model["totals"]["2026e"]["base"], 39)
        self.assertEqual(model["pillars"][0]["years"]["2026e"]["pessimistic"]["probability"], 0.45)
        self.assertTrue(model["assumption_ledger"][1]["included_in_model"])

    def test_final_company_pe_replaces_unreliable_combined_pillar_multiple(self):
        cells = {
            scenario: {"profit": profit, "multiple": 40, "probability": 1, "valuation": profit * 40}
            for scenario, profit in (("pessimistic", 27), ("base", 31), ("optimistic", 33))
        }
        baseline = {
            "pillars": [{"pillar_id": "core", "pillar_type": "operating", "profit_basis": "company_consensus",
                         "pricing_stage": "institution_baseline", "years": {"2026e": cells, "2027e": cells}}],
            "assumption_ledger": [],
        }
        model = merge_priced_independent_items(baseline, {}, {}, {
            "2026e": {"pessimistic_pe": 25, "final_pe": 30, "optimistic_pe": 35},
            "2027e": {"pessimistic_pe": 22, "final_pe": 27, "optimistic_pe": 32},
        })
        core = model["pillars"][0]
        self.assertEqual(core["years"]["2026e"]["base"]["multiple"], 30)
        self.assertEqual(core["years"]["2026e"]["base"]["valuation"], 930)
        self.assertEqual(core["pricing_stage"], "final_research")


if __name__ == "__main__":
    unittest.main()
