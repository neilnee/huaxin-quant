#!/usr/bin/env python3
"""Focused regression tests for the valuation stage-five contract."""

import unittest

from scripts.calc_valuation import calc_divergence, parse_params
from scripts.valuation_pipeline import (
    PipelineError,
    _canonical_billion,
    assemble_stage_five,
    build_stage_two_baseline,
    apply_unified_model_result,
    merge_manifest_stages,
    normalize_pe_scope_fallback,
    validate_stage_three_contract,
    valuation_model_matrix,
    validate_stage_five_mapping,
)


class ValuationContractTest(unittest.TestCase):
    def test_cross_check_only_pe_uses_conservative_explicit_fallback(self):
        inputs = {
            "selected_pe": 44, "pe_selection_method": "three_step",
            "excluded_anchor_names": [], "source_ids": [],
        }
        stage_two = {"institution_forecasts": [
            {"institution": name, "pe_2026e": pe, "pe_usability": "cross_check_only", "source_ids": [source]}
            for name, pe, source in (("甲", 26.5, "s1"), ("乙", 32, "s2"), ("丙", 43, "s3"),
                                     ("丁", 44, "s4"), ("戊", 45, "s5"), ("己", 53, "s6"))
        ], "comparable_companies": []}
        normalize_pe_scope_fallback(inputs, stage_two)
        self.assertEqual(inputs["selected_pe"], 32)
        self.assertEqual(inputs["comparable_pe_lower"], 28.8)
        self.assertEqual(inputs["comparable_pe_upper"], 38.4)
        self.assertEqual(inputs["pe_selection_method"], "override")
        self.assertEqual(set(inputs["excluded_anchor_names"]), {"甲", "乙", "丙", "丁", "戊", "己"})

    def test_unified_model_is_authoritative_for_matrix_and_ranking(self):
        model = {
            "pillars": [
                {"pillar_id": "core", "name": "主营", "pillar_type": "operating", "years": {
                    year: {scenario: {"valuation": value} for scenario, value in
                           (("pessimistic", 200), ("base", 300), ("optimistic", 400))}
                    for year in ("2026e", "2027e")}},
                {"pillar_id": "event", "name": "扩募", "pillar_type": "independent_event", "years": {
                    year: {scenario: {"valuation": value} for scenario, value in
                           (("pessimistic", 0), ("base", 20), ("optimistic", 50))}
                    for year in ("2026e", "2027e")}},
            ],
            "totals": {year: {"pessimistic": 200, "base": 320, "optimistic": 450}
                       for year in ("2026e", "2027e")},
        }
        matrix = valuation_model_matrix(model, "2026e")
        self.assertEqual(matrix["total"], model["totals"]["2026e"])
        self.assertEqual(matrix["layers_summary"]["layer2"]["base"], 20)
        legacy = {"matrix_2026e": {"total": {"base": 999}}, "matrix_2027e": {},
                  "pillars_2026e": [], "pillars_2027e": [], "pe_2026e": {"final_pe": 30}}
        result = apply_unified_model_result(legacy, model, {
            "meta": {"code": "000001", "name": "示例", "total_shares": 10, "current_price": 25},
            "growth_quality": "structural", "market_position": "leader",
        })
        self.assertEqual(result["matrix_2026e"]["total"]["base"], 320)
        self.assertEqual(result["ranking_row"]["基准估值_元"], "32.00")
        self.assertEqual(result["legacy_engine_audit"]["matrix_2026e"]["total"]["base"], 999)

    def test_stage_two_baseline_calculates_scenarios_and_totals(self):
        stage_two = {
            "institution_forecasts": [
                {"institution": name, "profit_scope": "recurring", "net_profit_2026e": value,
                 "net_profit_2027e": value, "source_ids": ["s1"]}
                for name, value in (("甲", 9), ("乙", 10), ("丙", 11))
            ],
            "baseline_pillars": [{
                "pillar_id": "pillar_01", "pillar_type": "operating", "profit_basis": "company_consensus",
                "name": "核心业务", "profit_metric": "net_profit",
                "valuation_method": "PE", "multiple_name": "PE", "source_ids": ["s1"],
                "years": {
                    year: {
                        "pessimistic": {"profit": 9, "multiple": 30, "probability": 1, "profit_reason": "共识", "multiple_reason": "下沿", "probability_reason": "经营业务", "source_ids": ["s1"]},
                        "base": {"profit": 10, "multiple": 35, "probability": 1, "profit_reason": "中位数", "multiple_reason": "中枢", "probability_reason": "经营业务", "source_ids": ["s1"]},
                        "optimistic": {"profit": 11, "multiple": 40, "probability": 1, "profit_reason": "上沿", "multiple_reason": "上沿", "probability_reason": "经营业务", "source_ids": ["s1"]},
                    } for year in ("2026e", "2027e")
                },
            }, {
                "pillar_id": "historical", "pillar_type": "independent_event", "profit_basis": "actual_historical",
                "name": "历史处置收益", "profit_metric": "net_profit", "valuation_method": "单列",
                "multiple_name": "倍数", "data_confidence": "high", "source_ids": ["s1"],
                "years": {year: {
                    scenario: {"profit": 31, "multiple": 1, "probability": 1,
                               "profit_reason": "往年已实现", "multiple_reason": "账面", "probability_reason": "已发生",
                               "source_ids": ["s1"]}
                    for scenario in ("pessimistic", "base", "optimistic")
                } for year in ("2026e", "2027e")},
            }],
            "assumption_ledger": [{
                "assumption_id": "a1", "pillar_id": "pillar_01", "year": "2026e",
                "metric": "交付量", "baseline_value": 100, "unit": "台", "condition": "按期交付",
                "information_cutoff": "2026-07-24", "source_ids": ["s1"],
                "included_in_baseline": True, "pricing_channel": "profit",
                "pricing_reason": "已经进入基准利润", "quantification_status": "quantitative",
            }],
            "business_item_coverage": [
                {"source_pillar_id": "pillar_01", "valuation_role": "valued_operating",
                 "valuation_pillar_id": "pillar_01", "quantification_status": "quantitative",
                 "overlap_status": "inside_target", "reason": "公司共识经营利润", "source_ids": ["s1"]},
                {"source_pillar_id": "historical", "valuation_role": "historical_excluded",
                 "valuation_pillar_id": None, "quantification_status": "quantitative",
                 "overlap_status": "not_applicable", "reason": "往年已经实现", "source_ids": ["s1"]},
            ],
        }
        stage_one = {"business_pillars": [{"pillar_id": "pillar_01"}, {"pillar_id": "historical"}]}
        baseline = build_stage_two_baseline(stage_two, [{"source_id": "s1"}], stage_one)
        self.assertEqual(baseline["pillars"][0]["years"]["2026e"]["base"]["valuation"], 350)
        self.assertEqual(baseline["totals"]["2027e"]["optimistic"], 440)
        self.assertEqual(baseline["operating_profit_reconciliation"]["2026e"]["base"]["difference_after"], 0)
        self.assertEqual(baseline["pillars"][1]["profit_basis"], "historical_realized")
        self.assertEqual(baseline["pillars"][1]["years"]["2026e"]["base"]["valuation"], 0)
        self.assertTrue(baseline["assumption_ledger"][0]["included_in_baseline"])
        self.assertEqual(baseline["assumption_ledger"][0]["pricing_channel"], "profit")
        self.assertEqual(baseline["business_item_coverage"][1]["valuation_role"], "historical_excluded")

    def test_stage_two_requires_complete_business_map_coverage(self):
        stage_two = {
            "institution_forecasts": [
                {"institution": name, "profit_scope": "recurring", "net_profit_2026e": value,
                 "net_profit_2027e": value, "source_ids": ["s1"]}
                for name, value in (("甲", 9), ("乙", 10), ("丙", 11))
            ],
            "baseline_pillars": [{
                "pillar_id": "core", "pillar_type": "operating", "profit_basis": "company_consensus",
                "name": "核心", "profit_metric": "net_profit", "valuation_method": "PE", "multiple_name": "PE",
                "source_ids": ["s1"], "years": {year: {scenario: {
                    "profit": 10, "multiple": 20, "probability": 1, "profit_reason": "共识",
                    "multiple_reason": "可比", "probability_reason": "经营", "source_ids": ["s1"],
                } for scenario in ("pessimistic", "base", "optimistic")} for year in ("2026e", "2027e")},
            }],
            "assumption_ledger": [],
            "business_item_coverage": [{
                "source_pillar_id": "core", "valuation_role": "valued_operating", "valuation_pillar_id": "core",
                "quantification_status": "quantitative", "overlap_status": "inside_target",
                "reason": "共识利润", "source_ids": ["s1"],
            }],
        }
        stage_one = {"business_pillars": [{"pillar_id": "core"}, {"pillar_id": "new_event"}]}
        with self.assertRaisesRegex(PipelineError, "覆盖不完整"):
            build_stage_two_baseline(stage_two, [{"source_id": "s1"}], stage_one)

    def test_stage_three_probability_event_requires_quantified_scenarios(self):
        stage_three = {"projects": [{
            "project_id": "event_01", "name": "扩产", "business_essence": "新增独立产能",
            "valuation_role": "probabilistic_event", "quantification_status": "quantitative",
            "included_in_valuation": True, "incremental_to_baseline": True,
            "source_ids": ["s1"], "invalidation_conditions": "审批失败",
            "years": {year: {scenario: {
                "profit": 0 if scenario == "pessimistic" else 1,
                "multiple": 15, "probability": {"pessimistic": 0, "base": 0.5, "optimistic": 0.8}[scenario],
                "profit_reason": "新增收益", "multiple_reason": "独立可比", "probability_reason": "审批进度",
                "source_ids": ["s1"],
            } for scenario in ("pessimistic", "base", "optimistic")} for year in ("2026e", "2027e")},
        }]}
        validate_stage_three_contract(stage_three, [{"source_id": "s1"}])
        stage_three["projects"][0]["years"]["2026e"]["base"]["profit"] = 0
        with self.assertRaisesRegex(PipelineError, "缺少可估值收益"):
            validate_stage_three_contract(stage_three, [{"source_id": "s1"}])

    def test_stage_two_rejects_forced_segment_split_without_direct_profit_evidence(self):
        scenarios = {
            scenario: {"profit": profit, "multiple": 20, "probability": 1,
                       "profit_reason": "分配", "multiple_reason": "可比", "probability_reason": "经营业务",
                       "source_ids": ["s1"]}
            for scenario, profit in (("pessimistic", 4), ("base", 5), ("optimistic", 6))
        }
        stage_two = {
            "institution_forecasts": [
                {"institution": name, "profit_scope": "unknown", "net_profit_2026e": value,
                 "net_profit_2027e": value, "source_ids": ["s1"]}
                for name, value in (("甲", 9), ("乙", 10), ("丙", 11))
            ],
            "baseline_pillars": [{
                "pillar_id": f"p{index}", "pillar_type": "operating",
                "profit_basis": "company_consensus", "name": f"业务{index}",
                "profit_metric": "net_profit", "valuation_method": "PE", "multiple_name": "PE",
                "data_confidence": "low", "source_ids": ["s1"],
                "years": {"2026e": scenarios, "2027e": scenarios},
            } for index in (1, 2)],
            "assumption_ledger": [],
        }
        with self.assertRaisesRegex(PipelineError, "不得强拆"):
            build_stage_two_baseline(stage_two, [{"source_id": "s1"}])

    def test_stage_five_joins_only_by_research_id(self):
        research = {
            "status": "ready",
            "business_pillars": [{"research_id": "pillar_01", "name": "主营"}],
            "market_divergences": [],
            "narrative_options": [],
        }
        mapping = {
            "valuation_inputs": {"selected_pe": 50},
            "pillar_mappings": [{
                "research_id": "pillar_01",
                "classification": "core_operating",
                "profit_timing": "realized",
                "accounting_treatment": "recurring",
                "calculation_mapping": {"target": "exclude"},
            }],
            "divergence_mappings": [],
            "option_mappings": [],
        }
        card = assemble_stage_five(research, mapping)
        self.assertEqual(card["business_pillars"][0]["calculation_mapping"]["target"], "exclude")

    def test_stage_five_rejects_missing_mapping(self):
        research = {"status": "ready", "business_pillars": [{"research_id": "pillar_01"}],
                    "market_divergences": [], "narrative_options": []}
        mapping = {"valuation_inputs": {}, "pillar_mappings": [], "divergence_mappings": [], "option_mappings": []}
        with self.assertRaises(PipelineError):
            assemble_stage_five(research, mapping)

    def test_yuan_value_is_rejected_as_billion(self):
        with self.assertRaises(PipelineError):
            _canonical_billion(1_767_000_000, "profit")
        self.assertEqual(_canonical_billion(17.67, "profit"), 17.67)

    def test_divergence_uses_institution_forecasts(self):
        result = calc_divergence([9.16, 9.56, 9.24])
        self.assertEqual(result["count"], 3)
        self.assertLess(result["divergence_pct"], 5)

    def test_parse_params_preserves_institution_forecasts(self):
        raw = {
            "meta": {"code": "688676", "name": "金盘科技", "total_shares": 4.6, "current_price": 70.16},
            "pillars": [{"name": "主营", "route": "B"}],
            "institution_consensus_2026e": [9.16, 9.56, 9.24],
            "institution_consensus_2027e": [12.0, 12.5, 12.2],
        }
        params = parse_params(raw)
        self.assertEqual(params["institution_consensus_2026e"], [9.16, 9.56, 9.24])

    def test_manifest_equal_rank_prefers_newer_stage(self):
        disk = {"stages": {"market_quote": {"status": "done", "current_price": 63.26,
                                             "updated_at": "2026-07-22T10:00:00"}}}
        local = {"stages": {"market_quote": {"status": "done", "current_price": 70.16,
                                              "updated_at": "2026-07-23T10:00:00"}}}
        merged = merge_manifest_stages(disk, local)
        self.assertEqual(merged["stages"]["market_quote"]["current_price"], 70.16)

    def test_pe_multiple_divergence_cannot_be_counted_twice(self):
        research = {
            "business_pillars": [{"research_id": "p1", "name": "主营"}],
            "market_divergences": [{"research_id": "d1"}],
            "narrative_options": [{"research_id": "o1", "included_in_valuation": False}],
            "consensus": [{"np_2026e": 9.0}, {"np_2026e": 9.3}, {"np_2026e": 9.6}],
        }
        mapping = {
            "valuation_inputs": {
                "growth_quality": "structural", "market_position": "leader",
                "comparable_pe_lower": 48, "comparable_pe_median": 52, "comparable_pe_upper": 60,
                "company_growth_rate": 40, "comp_growth_rate": 35, "qualitative_adjustments": [],
                "stage2_recommended_pe": 52, "selected_pe": 52, "pe_selection_method": "override",
                "pe_selection_reason": "机构目标估值中枢", "pe_scope_basis": "持续利润与核心业务PE同口径",
                "excluded_anchor_names": [], "source_ids": ["s1"],
            },
            "pillar_mappings": [{
                "research_id": "p1", "classification": "core_operating", "profit_timing": "realized",
                "accounting_treatment": "recurring", "calculation_mapping": {
                    "target": "pillar", "pillar_id": "p1", "route": "A1", "engine_values": {
                        "consensus_np_2026e": 9.3, "consensus_np_2027e": 12,
                        "consensus_np_lower_2026e": 9, "consensus_np_upper_2026e": 9.6,
                        "consensus_np_lower_2027e": 11, "consensus_np_upper_2027e": 13,
                    },
                },
            }],
            "divergence_mappings": [{
                "research_id": "d1", "driver_type": "multiple", "pillar_name": "主营",
                "classification": "core_operating", "profit_timing": "realized", "accounting_treatment": "recurring",
                "calculation_mapping": {"target": "layer2", "pillar_id": "p1",
                                        "engine_values": {"pessimistic": -10, "base": 0, "optimistic": 10, "description": "PE分歧"}},
            }],
            "option_mappings": [{
                "research_id": "o1", "classification": "narrative_option", "profit_timing": "long_term",
                "accounting_treatment": "non_recurring", "calculation_mapping": {"target": "exclude"},
            }],
        }
        with self.assertRaises(PipelineError):
            validate_stage_five_mapping(mapping, research)
        mapping["divergence_mappings"][0]["calculation_mapping"] = {"target": "exclude"}
        validate_stage_five_mapping(mapping, research)


if __name__ == "__main__":
    unittest.main()
