#!/usr/bin/env python3
"""Regression tests for signal Dashboard market context."""

import csv
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dashboard_signals


class DashboardSignalsMarketNoticeTests(unittest.TestCase):
    def test_plan_row_keeps_optional_prior_breakout_context(self):
        quant = {
            "code": "603132", "name": "金徽股份", "structure_score": 58,
            "prior_breakout_bonus_score": 47,
            "prior_breakout_bonus_reasons": ["前序VCP突破", "强势整理形成新VCP"],
            "prior_breakout_context_tag": "之前已有突破并强势整理",
        }
        plan = {"setup_signal": "PULLBACK_BUY", "target_quality": "A"}

        result = dashboard_signals.plan_row(plan, quant)

        self.assertEqual(result["prior_breakout_bonus_score"], 47)
        self.assertEqual(result["prior_breakout_bonus_reasons"], ["前序VCP突破", "强势整理形成新VCP"])
        self.assertEqual(result["prior_breakout_context_tag"], "之前已有突破并强势整理")

    def test_stale_plan_is_not_published_after_quant_enters_trend_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"; plans = root / "plans"
            runs.mkdir(); plans.mkdir()
            quant = {
                "meta": {"run_date": "2026-08-21"},
                "results": [{
                    "code": "003004", "name": "声迅股份", "model2_include": True,
                    "structure_type": "VCP", "structure_stage": "TREND_REBUILD",
                    "structure_valid": False, "setup_signal": "NONE",
                }],
            }
            plan = {"plans": [{
                "code": "003004", "name": "声迅股份", "setup_signal": "BREAKOUT_BUY",
                "target_quality": "A", "plan_reason": "旧结构突破计划",
            }]}
            (runs / "quant_260821.json").write_text(json.dumps(quant), encoding="utf-8")
            (plans / "signal_plan_260821.json").write_text(json.dumps(plan), encoding="utf-8")
            with patch.object(dashboard_signals, "RUNS", runs), patch.object(
                dashboard_signals, "PLAN_RUNS", plans
            ), patch.object(dashboard_signals, "realized_events_for_date", return_value=[]), patch.object(
                dashboard_signals, "pool_notices", return_value={}
            ), patch.object(dashboard_signals, "market_notice", return_value={"state": "UNKNOWN"}), patch.object(
                dashboard_signals, "sector_notices", return_value={}
            ), patch.object(dashboard_signals, "capital_notices", return_value=({}, 0, [])):
                result = dashboard_signals.build("260821")

        self.assertEqual(result["signals"], [])
        self.assertEqual(result["summary"]["planned"], 0)

    def test_previous_plan_hit_does_not_create_an_independent_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"; plans = root / "plans"
            runs.mkdir(); plans.mkdir()
            quant = {
                "meta": {"run_date": "2026-08-10", "strategy_version": "test-quant"},
                "results": [{
                    "code": "603882", "name": "金域医学", "structure_stage": "VCP_FORMING",
                    "structure_type": "VCP", "structure_valid": True, "model2_include": True,
                    "setup_signal": "NONE", "setup_quality": "D", "setup_score": 0,
                    "post_breakout_state": "POST_BREAKOUT_HOT", "close": 30.95, "volume": 285020,
                    "structure_risk_flags": ["EXTENDED_FROM_MA20"],
                }],
            }
            (runs / "quant_260810.json").write_text(json.dumps(quant, ensure_ascii=False), encoding="utf-8")
            event = {
                "code": "603882", "name": "金域医学", "setup_family": "BREAKOUT",
                "setup_type": "BREAKOUT_BUY", "entry_action": "NEW", "entry_grade": "REGULAR",
                "plan_date": "2026-08-07", "plan_target_quality": "A",
                "entry_model2_setup_signal": "NONE", "model2_setup_quality": "D",
                "post_breakout_state": "POST_BREAKOUT_HOT", "trigger_price_low": 29.58,
                "trigger_price_high": 31.63, "volume_min": 75131, "invalid_price": 28.41,
            }
            with patch.object(dashboard_signals, "RUNS", runs), patch.object(
                dashboard_signals, "PLAN_RUNS", plans
            ), patch.object(dashboard_signals, "realized_events_for_date", return_value=[event]), patch.object(
                dashboard_signals, "pool_notices", return_value={}
            ), patch.object(dashboard_signals, "market_notice", return_value={"state": "UNKNOWN"}), patch.object(
                dashboard_signals, "sector_notices", return_value={}
            ), patch.object(dashboard_signals, "capital_notices", return_value=({}, 0, [])):
                result = dashboard_signals.build("260810")

        self.assertEqual(result["summary"]["plan_hits"], 0)
        self.assertEqual(result["summary"]["triggered"], 0)
        self.assertEqual(result["signals"], [])

    def test_model2_and_plan_same_trigger_are_deduplicated(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs = root / "runs"; plans = root / "plans"
            runs.mkdir(); plans.mkdir()
            quant = {
                "meta": {"run_date": "2026-08-10", "strategy_version": "test-quant"},
                "results": [{
                    "code": "000001", "name": "测试", "setup_signal": "BREAKOUT_BUY",
                    "setup_quality": "A", "setup_score": 80, "setup_plan_inputs": {},
                }],
            }
            (runs / "quant_260810.json").write_text(json.dumps(quant), encoding="utf-8")
            event = {
                "code": "000001", "setup_type": "BREAKOUT_BUY", "entry_action": "NEW",
                "entry_grade": "A", "plan_date": "2026-08-07", "plan_target_quality": "A",
            }
            with patch.object(dashboard_signals, "RUNS", runs), patch.object(
                dashboard_signals, "PLAN_RUNS", plans
            ), patch.object(dashboard_signals, "realized_events_for_date", return_value=[event]), patch.object(
                dashboard_signals, "pool_notices", return_value={}
            ), patch.object(dashboard_signals, "market_notice", return_value={"state": "UNKNOWN"}), patch.object(
                dashboard_signals, "sector_notices", return_value={}
            ), patch.object(dashboard_signals, "capital_notices", return_value=({}, 0, [])):
                result = dashboard_signals.build("260810")

        triggered = [row for row in result["signals"] if row["signal_kind"] == "TRIGGERED"]
        self.assertEqual(len(triggered), 1)
        self.assertEqual(triggered[0]["signal_source"], "MODEL2_AND_PLAN")
        self.assertTrue(triggered[0]["previous_plan_hit"])
        self.assertEqual(triggered[0]["previous_plan_source_date"], "2026-08-07")
        self.assertNotIn("realized_plan_grade", triggered[0])
        self.assertEqual(result["summary"]["plan_hits"], 1)

    def test_capital_notice_keeps_main_and_margin_independent(self):
        rows = [
            {"trade_date": "2026-08-05", "main_net_inflow": -10.0, "amount": 1000.0, "financing_balance": 100.0},
            {"trade_date": "2026-08-06", "main_net_inflow": 20.0, "amount": 1000.0, "financing_balance": 101.0},
            {"trade_date": "2026-08-07", "main_net_inflow": 30.0, "amount": 1000.0, "financing_buy": 12.0, "financing_repay": 7.0, "financing_balance": 103.0},
        ]
        notice = dashboard_signals.capital_notice(rows)
        self.assertEqual(notice["status"], "complete")
        self.assertEqual(notice["main_order_state"], "INFLOW")
        self.assertEqual(notice["main_positive_days_3d"], 2)
        self.assertEqual(notice["margin_state"], "LEVERAGING")
        self.assertEqual(notice["financing_net_buy"], 5.0)

    def test_actionable_position_uses_market_range_without_setup_multiplier(self):
        row = {"signal_kind": "TRIGGERED", "setup_signal": "BREAKOUT_BUY", "setup_quality": "A"}
        market = {"state": "RECOVERY_WATCH"}
        sector = {"sector_phase": "主线", "sector_data_status": "READY", "sector_health_level": 0}
        result = dashboard_signals.position_guidance(row, market, sector)

        self.assertEqual(result["position_status"], "ACTIONABLE")
        self.assertNotIn("base_position", result)
        self.assertNotIn("environment_factor", result)
        self.assertEqual(result["adjusted_position"], [20, 40])
        self.assertEqual(result["allocation_amount_range"], [20000, 40000])
        self.assertEqual(result["position_denominator"], "single_stock_calculation_amount")
        self.assertEqual(result["position_advice"], "20%-40%（2—4万元）")

    def test_c_or_d_setup_is_observation_only(self):
        row = {"signal_kind": "TRIGGERED", "setup_signal": "RETEST_BUY", "setup_quality": "C"}
        result = dashboard_signals.position_guidance(
            row, {"state": "OFFENSIVE"}, {"sector_phase": "主线", "sector_data_status": "READY"}
        )
        self.assertEqual(result["position_status"], "OBSERVE_QUALITY")
        self.assertEqual(result["position_advice"], "观察（C级）")

    def test_weak_market_allows_small_position_but_sector_still_gates(self):
        row = {"signal_kind": "TRIGGERED", "setup_signal": "PULLBACK_BUY", "setup_quality": "A"}
        weak = dashboard_signals.position_guidance(
            row, {"state": "CONSOLIDATING"}, {"sector_phase": "主线", "sector_data_status": "READY", "sector_health_level": 0}
        )
        blocked = dashboard_signals.position_guidance(
            row, {"state": "OFFENSIVE"}, {"sector_phase": "退潮", "sector_data_status": "READY", "sector_health_level": 2}
        )
        excluded = dashboard_signals.position_guidance(
            row, {"state": "SELECTIVE"}, {"sector_phase": "NONE", "sector_data_status": "READY", "sector_health_level": 0}
        )

        self.assertEqual(weak["position_status"], "ACTIONABLE")
        self.assertEqual(weak["position_advice"], "10%-20%（1—2万元）")
        self.assertEqual(blocked["position_advice"], "观察（退潮）")
        self.assertEqual(excluded["position_advice"], "观察（观察）")
        self.assertIsNone(blocked["allocation_amount_range"])

    def test_plan_shows_same_range_for_a_and_b_without_treating_target_as_actual(self):
        row = {"signal_kind": "PLAN", "setup_signal": "PULLBACK_BUY", "setup_quality": "A"}
        result = dashboard_signals.position_guidance(
            row, {"state": "SELECTIVE"}, {"sector_phase": "转强", "sector_data_status": "READY", "sector_health_level": 0}
        )
        self.assertEqual(result["position_status"], "PLAN_CONDITIONAL")
        self.assertEqual(result["plan_position_a"], [40, 60])
        self.assertEqual(result["plan_position_b"], [40, 60])
        self.assertIsNone(result["adjusted_position"])
        self.assertEqual(result["position_advice"], "触发后 40%-60%（4—6万元）")

    def test_all_twenty_sector_combinations_in_each_market(self):
        markets = {"OFFENSIVE": [0, 80], "SELECTIVE": [40, 60], "RECOVERY_WATCH": [20, 40],
                   "CONSOLIDATING": [10, 20], "DEFENSIVE": [10, 20]}
        allowed = {"NONE": {1, 2}, "转强": {0, 1, 2}, "主线": {-1, 0, 1, 2}, "退潮": set()}
        row = {"signal_kind": "TRIGGERED", "setup_signal": "BREAKOUT_BUY", "setup_quality": "A"}
        for market, expected_range in markets.items():
            for phase, levels in allowed.items():
                for level in range(-2, 3):
                    with self.subTest(market=market, phase=phase, level=level):
                        result = dashboard_signals.position_guidance(row, {"state": market}, {
                            "sector_phase": phase, "sector_data_status": "READY", "sector_health_level": level})
                        self.assertEqual(result["market_position_range"], expected_range)
                        self.assertEqual(result["sector_eligible"], level in levels)
                        self.assertEqual(result["position_status"], "ACTIONABLE" if level in levels else "OBSERVE_SECTOR")
                        self.assertEqual(result["allocation_amount_range"], [v * 1000 for v in expected_range] if level in levels else None)

    def test_signal_type_and_grade_do_not_change_allowed_amount(self):
        for signal in ("PULLBACK_BUY", "BREAKOUT_BUY", "RETEST_BUY"):
            for grade in ("A", "B"):
                with self.subTest(signal=signal, grade=grade):
                    result = dashboard_signals.position_guidance(
                        {"setup_signal": signal, "setup_quality": grade}, {"state": "SELECTIVE"},
                        {"sector_phase": "NONE", "sector_data_status": "READY", "sector_health_level": 1})
                    self.assertEqual(result["allocation_amount_range"], [40000, 60000])

    def test_invalid_or_missing_sector_evidence_does_not_default_to_stability(self):
        row = {"setup_signal": "PULLBACK_BUY", "setup_quality": "A"}
        base = {"sector_phase": "主线", "sector_data_status": "READY", "sector_health_level": 0}
        cases = [{"sector_health_level": value} for value in (None, "", 0.5, 3, -3, float("inf"), float("nan"))]
        cases += [{"sector_data_status": value} for value in (None, "BUILDING", "BACKFILL")]
        cases += [{"sector_phase": "UNKNOWN"}, {"sector_health": "数据不足"}, {"sector_history_basis": "current_snapshot_backfill"}]
        for change in cases:
            with self.subTest(change=change):
                result = dashboard_signals.position_guidance(row, {"state": "OFFENSIVE"}, {**base, **change})
                self.assertEqual(result["position_status"], "OBSERVE_SECTOR")
                self.assertIsNone(result["allocation_amount_range"])

    def test_calculation_amount_contract_and_offensive_upper_bound(self):
        row = {"setup_signal": "BREAKOUT_BUY", "setup_quality": "B"}
        sector = {"sector_phase": "主线", "sector_data_status": "READY", "sector_health_level": -1}
        cfg = copy.deepcopy(dashboard_signals.POSITION_CFG)
        cfg["calculation_amount"] = 50000
        with patch.object(dashboard_signals, "POSITION_CFG", cfg):
            result = dashboard_signals.position_guidance(row, {"state": "OFFENSIVE"}, sector)
        self.assertEqual(result["position_advice"], "≤80%（≤4万元）")
        self.assertEqual(result["allocation_amount_range"], [0, 40000])
        for amount in (0, -1, 100001, float("inf"), None):
            cfg["calculation_amount"] = amount
            with self.subTest(amount=amount), patch.object(dashboard_signals, "POSITION_CFG", cfg):
                result = dashboard_signals.position_guidance(row, {"state": "OFFENSIVE"}, sector)
                self.assertEqual(result["position_status"], "OBSERVE_BUDGET")
                self.assertIsNone(result["allocation_amount_range"])

    def test_guidance_does_not_mutate_signal_or_promote_unknown_market(self):
        row = {"signal_kind": "PLAN", "setup_signal": "PULLBACK_BUY", "setup_quality": "C"}
        before = dict(row)
        result = dashboard_signals.position_guidance(row, {"state": "UNKNOWN", "candidate_state": "OFFENSIVE"},
                    {"sector_phase": "主线", "sector_data_status": "READY", "sector_health_level": 0})
        self.assertEqual(row, before)
        self.assertEqual(result["position_status"], "OBSERVE_MARKET")
        self.assertIsNone(result["allocation_amount_range"])

    def test_position_refresh_preserves_signals_and_backs_up_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root / "data" / "202610"; folder.mkdir(parents=True)
            path = folder / "signals_context_261008.js"
            payload = {"meta": {"run_date": "2026-10-08", "capital_requests_used": 4},
                       "summary": {"total": 1}, "signals": [{
                           "code": "000001", "signal_kind": "TRIGGERED", "setup_signal": "BREAKOUT_BUY",
                           "setup_quality": "A", "close": 10.0, "setup_score": 85, "trigger_price_low": 9.9,
                           "capital_support": {"margin_data_date": "2026-10-08"},
                           "environment_factor": 0, "base_position": [40, 50]}]}
            source = 'window.QUANT_DASHBOARD_SIGNALS_CONTEXTS["261008"] = ' + json.dumps(payload) + ';\n'
            path.write_text(source, encoding="utf-8")
            sector = {"sector_phase": "NONE", "sector_data_status": "READY", "sector_health_level": 1}
            with patch.object(dashboard_signals, "ROOT", root), patch.object(dashboard_signals, "OUT", root / "data"), \
                 patch.object(dashboard_signals, "market_notice", return_value={"state": "DEFENSIVE"}), \
                 patch.object(dashboard_signals, "sector_notices", return_value={"000001": sector}):
                result = dashboard_signals.refresh_position("261008")
            self.assertEqual(Path(result["backup"]).read_text(encoding="utf-8"), source)
            updated = json.loads(path.read_text(encoding="utf-8").split(" = ", 1)[1].rstrip(";\n"))
            self.assertEqual(updated["summary"], payload["summary"])
            self.assertEqual(updated["meta"]["capital_requests_used"], 4)
            after = updated["signals"][0]
            for field in ("code", "signal_kind", "setup_signal", "setup_quality", "close", "setup_score", "trigger_price_low", "capital_support"):
                self.assertEqual(after[field], payload["signals"][0][field])
            self.assertNotIn("environment_factor", after)
            self.assertNotIn("base_position", after)
            self.assertEqual(after["position_advice"], "10%-20%（1—2万元）")

    def test_position_refresh_rejects_date_mismatch_before_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); folder = root / "202610"; folder.mkdir()
            path = folder / "signals_context_261008.js"
            source = 'window.X["261008"] = {"meta": {"run_date": "2026-10-09"}};\n'
            path.write_text(source, encoding="utf-8")
            with patch.object(dashboard_signals, "OUT", root):
                with self.assertRaisesRegex(ValueError, "date mismatch"):
                    dashboard_signals.refresh_position("261008")
            self.assertEqual(path.read_text(encoding="utf-8"), source)

    def test_sector_notice_carries_phase_and_health_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stock_path = root / "stock_strength_260811.csv"
            sector_path = root / "sector_heat_260811.csv"
            with stock_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["code", "sw_l2_name"])
                writer.writeheader(); writer.writerow({"code": "601233", "sw_l2_name": "化纤"})
            with sector_path.open("w", encoding="utf-8", newline="") as handle:
                fields = ["block_type", "block_name", "sector_state", "sector_phase", "sector_health", "sector_health_level", "sector_health_score", "sector_policy_tier", "data_status", "rank_20", "history_basis"]
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader(); writer.writerow({"block_type": "industry_sw_l2", "block_name": "化纤", "sector_state": "观察中", "sector_phase": "NONE", "sector_health": "温和改善", "sector_health_level": "1", "sector_health_score": "0.3", "sector_policy_tier": "D", "data_status": "READY", "rank_20": "36", "history_basis": "point_in_time"})
            with patch.object(dashboard_signals, "MARKET_DIR", root):
                notice = dashboard_signals.sector_notices("260811")["601233"]

        self.assertEqual(notice["sector_phase"], "NONE")
        self.assertEqual(notice["sector_health"], "温和改善")
        self.assertEqual(notice["sector_health_level"], 1.0)
        self.assertEqual(notice["sector_data_status"], "READY")

    def test_defensive_notice_uses_same_day_market_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {
                "market_state": {
                    "label": "弱势下行",
                    "confirmed_state": "DEFENSIVE",
                    "candidate_state": "SELECTIVE",
                    "risk_tags": ["高波动", "市场广度偏弱"],
                }
            }
            (root / "market_context_260730.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
            with patch.object(dashboard_signals, "MARKET_CONTEXT_DIR", root):
                notice = dashboard_signals.market_notice("260730")

        self.assertEqual(notice["state"], "DEFENSIVE")
        self.assertEqual(notice["candidate_state"], "SELECTIVE")
        self.assertEqual(notice["label"], "弱势下行")
        self.assertEqual(notice["tag"], "防御试探")
        self.assertEqual(notice["tone"], "caution")
        self.assertEqual(notice["risk_tags"], ["高波动", "市场广度偏弱"])

    def test_market_notice_uses_confirmed_label_for_legacy_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "market_context_260730.json").write_text(
                json.dumps({"market_state": {"label": "修复期", "raw_label": "SELECTIVE"}}),
                encoding="utf-8",
            )
            with patch.object(dashboard_signals, "MARKET_CONTEXT_DIR", root):
                notice = dashboard_signals.market_notice("260730")

        self.assertEqual(notice["state"], "RECOVERY_WATCH")
        self.assertEqual(notice["candidate_state"], "SELECTIVE")
        self.assertEqual(notice["tag"], "修复参与")

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

    def test_pool_notice_labels_source_and_verified_financial_risks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "pool_260807.csv"
            fields = [
                "股票代码", "pool_channel", "fundamental_status", "归母净利润_元",
                "资产负债率_pct", "经营现金流_元", "营收同比增速_pct",
                "净利润同比增速_pct", "毛利率_pct", "风险标签",
            ]
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fields)
                writer.writeheader()
                writer.writerow({
                    "股票代码": '="000001"', "pool_channel": "CORE_QUALITY",
                    "fundamental_status": "CORE_VERIFIED", "归母净利润_元": "10",
                    "资产负债率_pct": "71", "经营现金流_元": "-1",
                    "营收同比增速_pct": "-25", "净利润同比增速_pct": "-40",
                    "毛利率_pct": "8", "风险标签": "LOW_ROE;HIGH_VALUATION",
                })
            with patch.object(dashboard_signals, "POOL_DIR", root):
                notice = dashboard_signals.pool_notices("260807")["000001"]

        self.assertEqual(notice["source_label"], "核心质量池")
        self.assertEqual(notice["financial_tone"], "risk")
        self.assertEqual(
            notice["financial_tags"],
            ["高负债", "经营现金流为负", "营收明显下滑", "利润明显下滑", "毛利率偏低", "低 ROE", "估值偏高"],
        )

    def test_expansion_pool_is_unverified_not_negative(self):
        notice = dashboard_signals.financial_notice({
            "fundamental_status": "FUNDAMENTAL_UNVERIFIED",
            "风险标签": "FUNDAMENTAL_UNVERIFIED",
        })
        self.assertEqual(notice["financial_tone"], "unverified")
        self.assertEqual(notice["financial_tags"], ["财务未查询"])

    def test_signal_financial_cache_replaces_unverified_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "600123.json").write_text(json.dumps({
                "snapshots": [{
                    "label": "2026一季报", "report_end": "2026-03-31",
                    "available_from": "2026-04-30", "source": "东方财富妙想",
                    "risk_tags": ["最新期亏损", "利润明显下滑"],
                }]
            }, ensure_ascii=False), encoding="utf-8")
            with patch.object(dashboard_signals, "SIGNAL_FIN_DIR", root):
                notice = dashboard_signals.signal_financial_notice("260807", "600123")

        self.assertEqual(notice["financial_tone"], "risk")
        self.assertEqual(notice["financial_report_period"], "2026一季报")
        self.assertEqual(notice["financial_tags"], ["最新期亏损", "利润明显下滑"])


if __name__ == "__main__":
    unittest.main()
