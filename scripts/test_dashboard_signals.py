#!/usr/bin/env python3
"""Regression tests for signal Dashboard market context."""

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import dashboard_signals


class DashboardSignalsMarketNoticeTests(unittest.TestCase):
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

    def test_sector_states_are_grouped_by_position_policy(self):
        self.assertEqual(dashboard_signals.sector_group("持续主线"), "STRONG")
        self.assertEqual(dashboard_signals.sector_group("高位分歧"), "NEUTRAL")
        self.assertEqual(dashboard_signals.sector_group("观察中"), "BLOCKED")
        self.assertEqual(dashboard_signals.sector_group("历史积累中"), "BLOCKED")
        self.assertEqual(dashboard_signals.sector_group("弱势退潮"), "BLOCKED")

    def test_actionable_position_uses_setup_market_and_sector(self):
        row = {"signal_kind": "TRIGGERED", "setup_signal": "BREAKOUT_BUY", "setup_quality": "A"}
        market = {"state": "RECOVERY_WATCH"}
        sector = {"sector_state": "强势初现", "sector_group": "STRONG"}
        result = dashboard_signals.position_guidance(row, market, sector)

        self.assertEqual(result["position_status"], "ACTIONABLE")
        self.assertEqual(result["base_position"], [40, 50])
        self.assertEqual(result["environment_factor"], 0.7)
        self.assertEqual(result["adjusted_position"], [25.0, 35.0])
        self.assertEqual(result["position_advice"], "25%-35%")

    def test_c_or_d_setup_is_observation_only(self):
        row = {"signal_kind": "TRIGGERED", "setup_signal": "RETEST_BUY", "setup_quality": "C"}
        result = dashboard_signals.position_guidance(
            row, {"state": "OFFENSIVE"}, {"sector_state": "持续主线", "sector_group": "STRONG"}
        )
        self.assertEqual(result["position_status"], "OBSERVE_QUALITY")
        self.assertEqual(result["position_advice"], "观察（C级）")

    def test_weak_market_and_blocked_sector_are_observation_only(self):
        row = {"signal_kind": "TRIGGERED", "setup_signal": "PULLBACK_BUY", "setup_quality": "A"}
        weak = dashboard_signals.position_guidance(
            row, {"state": "CONSOLIDATING"}, {"sector_state": "持续主线", "sector_group": "STRONG"}
        )
        blocked = dashboard_signals.position_guidance(
            row, {"state": "OFFENSIVE"}, {"sector_state": "高位分歧", "sector_group": "NEUTRAL"}
        )
        excluded = dashboard_signals.position_guidance(
            row, {"state": "OFFENSIVE"}, {"sector_state": "观察中", "sector_group": "BLOCKED"}
        )

        self.assertEqual(weak["position_advice"], "观察（市场弱势）")
        self.assertEqual(blocked["position_advice"], "10%-20%")
        self.assertEqual(excluded["position_advice"], "观察（观察中）")

    def test_plan_shows_conditional_a_and_b_ranges(self):
        row = {"signal_kind": "PLAN", "setup_signal": "PULLBACK_BUY", "setup_quality": "A"}
        result = dashboard_signals.position_guidance(
            row, {"state": "SELECTIVE"}, {"sector_state": "高位分歧", "sector_group": "NEUTRAL"}
        )
        self.assertEqual(result["position_status"], "PLAN_CONDITIONAL")
        self.assertEqual(result["plan_position_a"], [10.0, 15.0])
        self.assertEqual(result["plan_position_b"], [5.0, 10.0])
        self.assertEqual(result["position_advice"], "A 10%-15% / B 5%-10%")

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
