#!/usr/bin/env python3
"""Focused regression tests for the valuation stage-five contract."""

import unittest

from scripts.calc_valuation import calc_divergence, parse_params
from scripts.valuation_pipeline import (
    PipelineError,
    _canonical_billion,
    assemble_stage_five,
    merge_manifest_stages,
    validate_stage_five_mapping,
)


class ValuationContractTest(unittest.TestCase):
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
                "pe_selection_reason": "机构目标估值中枢", "source_ids": ["s1"],
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
