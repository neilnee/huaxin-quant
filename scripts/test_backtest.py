#!/usr/bin/env python3
"""Regression tests for the rolling strategy backtest."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import backtest


def quant_payload(run_date, rows):
    return {"meta": {"run_date": run_date, "strategy_version": "test"}, "results": rows}


def row(code="000001", anchor="2026-01-01", stage="VCP_TIGHT", setup="NONE", close=10, volume=100):
    return {
        "code": code, "name": "测试", "structure_stage": stage, "setup_signal": setup,
        "close": close, "volume": volume, "contraction_group": [{"start_date": anchor}],
        "structure_type": "VCP", "structure_valid": True, "post_breakout_state": "PRE_BREAKOUT",
    }


def plan(action="NEW", family="BREAKOUT", anchor="2026-01-01", quality="A"):
    result = {
        "code": "000001", "name": "测试", "structure_anchor": anchor,
        "setup_family": family, "plan_action": action, "target_quality": quality,
        "model2_stage": "VCP_TIGHT", "structure_score": 85, "structure_risk_score": 5,
        "trigger_price_low": 10, "trigger_price_high": 12,
        "ideal_price_low": 10.5, "ideal_price_high": 11.5,
        "invalid_price": 9, "strategy_version": "test-plan", "formula_ref": {"pivot": 10},
    }
    if family == "BREAKOUT":
        result.update({"volume_min": 90, "ideal_volume_min": 120})
    else:
        result.update({"volume_max": 110, "ideal_volume_max": 80})
    return result


class BacktestEventTests(unittest.TestCase):
    def test_plan_grade_is_mutually_exclusive(self):
        self.assertEqual(backtest.plan_hit_grade(plan(), row(close=11, volume=130)), "A")
        self.assertEqual(backtest.plan_hit_grade(plan(), row(close=11, volume=100)), "REGULAR")
        self.assertIsNone(backtest.plan_hit_grade(plan(), row(close=9.5, volume=130)))

    def test_new_and_follow_use_realized_plan_conditions(self):
        actual = row(setup="BREAKOUT_BUY", close=11, volume=130)
        actual["model2_include"] = True
        new_event = backtest.realized_plan_event(plan(), actual, None, "2026-07-01", "2026-07-02", "test")
        self.assertEqual(new_event["entry_grade"], "A")
        self.assertEqual(new_event["signal_date"], "2026-07-02")
        no_repeat_signal = row(close=11, volume=130)
        no_repeat_signal["model2_include"] = True
        self.assertIsNotNone(backtest.realized_plan_event(plan(), no_repeat_signal, None, "2026-07-01", "2026-07-02", "test"))
        follow_event = backtest.realized_plan_event(plan(action="FOLLOW"), no_repeat_signal, None, "2026-07-01", "2026-07-02", "test")
        self.assertEqual(follow_event["entry_action"], "FOLLOW")

    def test_pullback_plan_cannot_cross_into_post_breakout_lifecycle(self):
        actual = row(close=11, volume=70)
        actual.update({"model2_include": True, "post_breakout_state": "POST_BREAKOUT_HOT"})
        self.assertIsNone(backtest.realized_plan_event(plan(family="PULLBACK"), actual, None, "2026-07-01", "2026-07-02", "test"))

    def test_breakout_plan_can_realize_on_first_post_breakout_hot_day(self):
        actual = row(close=11, volume=130)
        actual.update({
            "model2_include": True,
            "post_breakout_state": "POST_BREAKOUT_HOT",
            "setup_signal": "NONE",
        })
        event = backtest.realized_plan_event(
            plan(family="BREAKOUT"), actual, None, "2026-07-01", "2026-07-02", "test"
        )

        self.assertIsNotNone(event)
        self.assertEqual(event["setup_type"], "BREAKOUT_BUY")
        self.assertEqual(event["entry_model2_setup_signal"], "NONE")
        self.assertEqual(event["post_breakout_state"], "POST_BREAKOUT_HOT")

    def test_first_realized_event_only_and_new_structure_can_reenter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            quant_dir = root / "quant"; plan_dir = root / "plan"
            quant_dir.mkdir(); plan_dir.mkdir()
            dates = ["260701", "260702", "260703", "260706"]
            rows = [
                row(),
                row(setup="BREAKOUT_BUY", close=11, volume=130),
                row(setup="BREAKOUT_BUY", close=11, volume=130),
                row(anchor="2026-06-20", setup="BREAKOUT_BUY", close=11, volume=130),
            ]
            for actual in rows:
                actual["model2_include"] = True
            for date, actual in zip(dates, rows):
                (quant_dir / f"quant_{date}.json").write_text(json.dumps(quant_payload(f"20{date[:2]}-{date[2:4]}-{date[4:]}", [actual])), encoding="utf-8")
            for date, anchor in (("260701", "2026-01-01"), ("260702", "2026-01-01"), ("260703", "2026-06-20")):
                (plan_dir / f"signal_plan_{date}.json").write_text(json.dumps({"plans": [plan(anchor=anchor)]}), encoding="utf-8")
            calendar = ["2026-07-01", "2026-07-02", "2026-07-03", "2026-07-06"]
            with patch.object(backtest, "QUANT_DIR", quant_dir), patch.object(backtest, "PLAN_DIR", plan_dir):
                events = backtest.discover_events("260706", calendar)

        self.assertEqual(len(events), 2)
        self.assertEqual([event["structure_anchor"] for event in events], ["2026-01-01", "2026-06-20"])
        self.assertEqual([event["entry_date_yy"] for event in events], ["260702", "260706"])

    def test_unfinished_horizons_stay_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE daily_bars(code TEXT,trade_date TEXT,close REAL)")
            calendar = [f"2026-07-{day:02d}" for day in range(1, 13)]
            conn.executemany("INSERT INTO daily_bars VALUES(?,?,?)", [("000001", date, 10 + index) for index, date in enumerate(calendar)])
            conn.commit(); conn.close()
            event = {
                "signal_date": calendar[0], "signal_date_yy": "260701", "code": "000001", "name": "测试",
                "event_type": "MATURE_ENTRY", "setup_type": "", "signal_close_snapshot": 10,
                "structure_pivot": 12,
            }
            environment = {"market_state": "SELECTIVE", "market_state_label": "结构分化", "sector_name": "测试行业", "sector_state": "持续主线"}
            with patch.object(backtest, "MARKET_DB", db_path), patch.object(backtest, "trading_calendar", return_value=calendar), patch.object(backtest, "load_signal_environment", return_value=environment), patch.object(backtest, "load_corporate_actions", return_value=[]):
                result = backtest.add_performance([event], "260708")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["age_days"], 7)
        self.assertEqual(result[0]["return_5d"], 50.0)
        self.assertIsNone(result[0]["return_10d"])
        self.assertIsNone(result[0]["return_20d"])
        self.assertEqual(result[0]["breakout_time"], "2026-07-04 · T+3")
        self.assertEqual(result[0]["breakout_return"], 8.333)

    def test_structure_selection_returns_include_pending_and_mature_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE daily_bars(code TEXT,trade_date TEXT,close REAL)")
            calendar = [f"2026-07-{day:02d}" for day in range(1, 13)]
            rows = []
            for code, start in (("000001", 10), ("000002", 20)):
                rows.extend((code, date, start + index) for index, date in enumerate(calendar))
            conn.executemany("INSERT INTO daily_bars VALUES(?,?,?)", rows)
            conn.commit(); conn.close()
            events = [
                {"event_type": "VCP_SELECTION", "selection_date": calendar[0], "signal_date": calendar[0],
                 "code": "000001", "name": "成熟", "structure_anchor": "A",
                 "initial_stage": "VCP_FORMING", "initial_bloom_status": "FORMING"},
                {"event_type": "VCP_SELECTION", "selection_date": calendar[5], "signal_date": calendar[5],
                 "code": "000002", "name": "待观察", "structure_anchor": "B",
                 "initial_stage": "VCP_EARLY", "initial_bloom_status": "EARLY"},
            ]
            with patch.object(backtest, "MARKET_DB", db_path), \
                 patch.object(backtest, "trading_calendar", return_value=calendar), \
                 patch.object(backtest, "load_corporate_actions", return_value=[]):
                result = backtest.add_structure_performance(events, "260708")

        by_code = {row["code"]: row for row in result}
        self.assertEqual(by_code["000001"]["return_5d"], 50.0)
        self.assertIsNone(by_code["000001"]["return_20d"])
        self.assertIsNone(by_code["000002"]["return_5d"])
        window = backtest.build_structure_sample_window(result, {"id": "ALL", "label": "全部", "max_age_trade_days": None})
        self.assertEqual(window["summary"]["events"], 2)
        self.assertEqual(window["summary"]["mature_events"], 1)
        self.assertEqual(window["summary"]["pending_events"], 1)

    def test_structure_sample_windows_use_independent_30_90_180_day_ranges(self):
        rows = [
            {
                "selection_date": f"2026-0{index + 1}-01",
                "age_days": age,
                "initial_stage": "VCP_FORMING" if index % 2 else "VCP_MATURE",
                "initial_bloom_status": "FORMING",
            }
            for index, age in enumerate([5, 30, 31, 90, 91, 180, 181])
        ]

        windows = {
            window["id"]: backtest.build_structure_sample_window(rows, window)
            for window in backtest.STRUCTURE_SAMPLE_WINDOWS
        }

        self.assertEqual(backtest.STRUCTURE_DEFAULT_SAMPLE_WINDOW, "90D")
        self.assertEqual(set(windows), {"30D", "90D", "180D", "ALL"})
        self.assertEqual(windows["30D"]["summary"]["events"], 2)
        self.assertEqual(windows["90D"]["summary"]["events"], 4)
        self.assertEqual(windows["180D"]["summary"]["events"], 6)
        self.assertEqual(windows["ALL"]["summary"]["events"], 7)

    def test_completed_event_keeps_frozen_returns_after_twenty_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "market.sqlite"
            missing_capital = Path(tmp) / "missing-capital.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE daily_bars(code TEXT,trade_date TEXT,close REAL)")
            calendar = [f"2026-07-{day:02d}" for day in range(1, 27)]
            conn.executemany("INSERT INTO daily_bars VALUES(?,?,?)", [("000001", date, 10 + index) for index, date in enumerate(calendar)])
            conn.commit(); conn.close()
            event = {
                "plan_date": "2026-06-30", "signal_date": calendar[0], "signal_date_yy": "260701",
                "code": "000001", "name": "测试", "event_type": "BUY_POINT",
                "setup_family": "BREAKOUT", "entry_grade": "A", "maturity_stage": "VCP_TIGHT",
                "signal_close_snapshot": 10, "structure_pivot": 12,
            }
            environment = {"market_state": "SELECTIVE", "market_state_label": "结构分化", "sector_name": "测试行业", "sector_state": "持续主线"}
            with patch.object(backtest, "MARKET_DB", db_path), patch.object(backtest, "CAPITAL_DB", missing_capital), patch.object(backtest, "trading_calendar", return_value=calendar), patch.object(backtest, "load_signal_environment", return_value=environment), patch.object(backtest, "load_corporate_actions", return_value=[]):
                result = backtest.add_performance([event], "260726")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["age_days"], 25)
        self.assertEqual(result[0]["observation_age_days"], 20)
        self.assertEqual(result[0]["observation_end_date"], "2026-07-21")
        self.assertEqual(result[0]["return_20d"], 200.0)
        self.assertEqual(result[0]["report_close"], 30.0)

    def test_sector_membership_backfill_uses_earliest_snapshot_but_same_day_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context_dir = root / "context"
            report_dir = root / "report"
            context_dir.mkdir(); report_dir.mkdir()
            db_path = root / "market.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE stock_industries(snapshot_date TEXT,code TEXT,sw_industry_code TEXT)")
            conn.execute("CREATE TABLE industry_definitions(snapshot_date TEXT,industry_system TEXT,industry_code TEXT,industry_name TEXT)")
            conn.executemany("INSERT INTO stock_industries VALUES(?,?,?)", [
                ("2026-07-01", "000001", "X400103"),
                ("2026-08-07", "000001", "X500101"),
            ])
            conn.executemany("INSERT INTO industry_definitions VALUES(?,?,?,?)", [
                ("2026-07-01", "sw", "X4001", "半导体"),
                ("2026-08-07", "sw", "X5001", "软件开发"),
            ])
            conn.commit()
            (context_dir / "market_context_260520.json").write_text(json.dumps({
                "market_state": {"label": "结构性强势"},
                "sector_rankings": {"industry_sw_l2": [
                    {"block_name": "半导体", "sector_state": "持续主线"},
                    {"block_name": "软件开发", "sector_state": "弱势退潮"},
                ]},
            }), encoding="utf-8")

            with patch.object(backtest, "MARKET_CONTEXT_DIR", context_dir), patch.object(backtest, "MARKET_REPORT_DIR", report_dir):
                result = backtest.load_signal_environment(conn, {
                    "signal_date": "2026-05-20", "signal_date_yy": "260520", "code": "000001",
                })
            conn.close()

        self.assertEqual(result["sector_name"], "半导体")
        self.assertEqual(result["sector_state"], "持续主线")
        self.assertEqual(result["sector_membership_date"], "2026-07-01")
        self.assertEqual(result["sector_membership_basis"], "earliest_available_backfill")

    def test_sector_membership_prefers_latest_prior_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context_dir = root / "context"
            report_dir = root / "report"
            context_dir.mkdir(); report_dir.mkdir()
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE stock_industries(snapshot_date TEXT,code TEXT,sw_industry_code TEXT)")
            conn.execute("CREATE TABLE industry_definitions(snapshot_date TEXT,industry_system TEXT,industry_code TEXT,industry_name TEXT)")
            conn.executemany("INSERT INTO stock_industries VALUES(?,?,?)", [
                ("2026-07-01", "000001", "X400103"),
                ("2026-07-03", "000001", "X500101"),
            ])
            conn.executemany("INSERT INTO industry_definitions VALUES(?,?,?,?)", [
                ("2026-07-01", "sw", "X4001", "半导体"),
                ("2026-07-03", "sw", "X5001", "软件开发"),
            ])
            conn.commit()
            (context_dir / "market_context_260702.json").write_text(json.dumps({
                "market_state": {"label": "结构分化"},
                "sector_rankings": {"industry_sw_l2": [{"block_name": "半导体", "sector_state": "强势初现"}]},
            }), encoding="utf-8")

            with patch.object(backtest, "MARKET_CONTEXT_DIR", context_dir), patch.object(backtest, "MARKET_REPORT_DIR", report_dir):
                result = backtest.load_signal_environment(conn, {
                    "signal_date": "2026-07-02", "signal_date_yy": "260702", "code": "000001",
                })
            conn.close()

        self.assertEqual(result["sector_name"], "半导体")
        self.assertEqual(result["sector_membership_date"], "2026-07-01")
        self.assertEqual(result["sector_membership_basis"], "prior_snapshot")

    def test_sector_state_uses_full_daily_state_database_not_dashboard_top20(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            context_dir = root / "context"
            report_dir = root / "report"
            context_dir.mkdir(); report_dir.mkdir()
            conn = sqlite3.connect(":memory:")
            conn.execute("CREATE TABLE stock_industries(snapshot_date TEXT,code TEXT,sw_industry_code TEXT)")
            conn.execute("CREATE TABLE industry_definitions(snapshot_date TEXT,industry_system TEXT,industry_code TEXT,industry_name TEXT)")
            conn.execute("INSERT INTO stock_industries VALUES('2026-07-01','000001','X400103')")
            conn.execute("INSERT INTO industry_definitions VALUES('2026-07-01','sw','X4001','半导体')")
            state_conn = sqlite3.connect(":memory:")
            state_conn.execute("CREATE TABLE sector_daily_metrics(trade_date TEXT,block_kind TEXT,block_name TEXT,sector_state TEXT,history_basis TEXT)")
            state_conn.execute("INSERT INTO sector_daily_metrics VALUES('2026-05-20','industry_sw_l2','半导体','观察中','current_snapshot_backfill')")
            state_conn.commit()
            (context_dir / "market_context_260520.json").write_text(json.dumps({
                "market_state": {"label": "结构性强势"},
                "sector_rankings": {"industry_sw_l2": []},
            }), encoding="utf-8")

            with patch.object(backtest, "MARKET_CONTEXT_DIR", context_dir), patch.object(backtest, "MARKET_REPORT_DIR", report_dir):
                result = backtest.load_signal_environment(conn, {
                    "signal_date": "2026-05-20", "signal_date_yy": "260520", "code": "000001",
                }, state_conn)
            state_conn.close(); conn.close()

        self.assertEqual(result["sector_state"], "观察中")
        self.assertEqual(result["sector_state_source"], "market_regime.sqlite")
        self.assertEqual(result["sector_history_basis"], "current_snapshot_backfill")

    def test_holding_period_value_adjusts_dividend_bonus_and_rights(self):
        value, applied = backtest.holding_period_value("2026-05-22", "2026-06-04", 10, [
            {"date": "2026-05-22", "cash_dividend_per_10": 9, "bonus_shares_per_10": 9},
            {"date": "2026-05-29", "cash_dividend_per_10": 2, "bonus_shares_per_10": 3},
            {"date": "2026-06-04", "rights_shares_per_10": 2, "rights_price": 4},
        ])

        self.assertAlmostEqual(value, 14.76)
        self.assertEqual([item["date"] for item in applied], ["2026-05-29", "2026-06-04"])

    def test_breakout_is_empty_when_frozen_pivot_is_not_crossed(self):
        result = backtest.breakout_performance(
            {"structure_pivot": 20},
            {"2026-07-01": 10, "2026-07-02": 11},
            ["2026-07-01", "2026-07-02"],
            0,
            1,
        )

        self.assertIsNone(result["breakout_time"])
        self.assertIsNone(result["breakout_return"])

    def test_plan_capital_uses_only_rows_through_plan_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "capital.sqlite"
            conn = sqlite3.connect(db_path)
            conn.execute("CREATE TABLE stock_capital(code TEXT,trade_date TEXT,metrics_json TEXT,fetched_at TEXT,PRIMARY KEY(code,trade_date))")
            rows = []
            for index, date in enumerate(("2026-08-03", "2026-08-04", "2026-08-05", "2026-08-06", "2026-08-07")):
                metrics = {
                    "amount": 1000,
                    "main_net_inflow": 10,
                    "financing_balance": 100 + index * 2,
                    "financing_buy": 20,
                    "financing_repay": 10,
                }
                rows.append(("000001", date, json.dumps(metrics), "2026-08-09T18:00:00"))
            rows.append(("000001", "2026-08-10", json.dumps({
                "amount": 1000, "main_net_inflow": -500, "financing_balance": 50,
                "financing_buy": 1, "financing_repay": 30,
            }), "2026-08-10T18:00:00"))
            conn.executemany("INSERT INTO stock_capital VALUES(?,?,?,?)", rows)
            conn.commit()

            result = backtest.load_plan_capital(conn, {"code": "000001", "plan_date": "2026-08-07"})
            conn.close()

        self.assertEqual(result["main_order_state"], "INFLOW")
        self.assertEqual(result["margin_state"], "LEVERAGING")
        self.assertEqual(result["main_data_date"], "2026-08-07")
        self.assertEqual(result["margin_data_date"], "2026-08-07")
        self.assertEqual(result["main_positive_days_3d"], 3)

    def test_capital_before_observation_start_is_not_a_historical_sample(self):
        result = backtest.load_plan_capital(None, {"code": "000001", "plan_date": "2026-08-06"})
        self.assertEqual(result["status"], "before_observation_start")
        self.assertEqual(result["main_order_state"], "INSUFFICIENT")

    def test_condition_value_reports_lift_and_missed_opportunities(self):
        condition = next(item for item in backtest.CONDITIONS if item["id"] == "strong_main_capital_confirmation")
        rows = [
            {"main_order_state": "INFLOW", "return_5d": 10},
            {"main_order_state": "BALANCED", "return_5d": -2},
            {"main_order_state": "OUTFLOW", "return_5d": -5},
            {"main_order_state": "INSUFFICIENT", "return_5d": 7},
        ]

        result = backtest.condition_horizon(rows, condition, 5)

        self.assertEqual(result["baseline_samples"], 2)
        self.assertEqual(result["retained_samples"], 1)
        self.assertEqual(result["excluded_samples"], 1)
        self.assertEqual(result["weak_samples"], 1)
        self.assertEqual(result["unavailable_samples"], 1)
        self.assertEqual(result["retention_rate"], 50.0)
        self.assertEqual(result["win_rate_lift"], 50.0)
        self.assertEqual(result["avg_return_lift"], 6.0)
        self.assertEqual(result["weak_avg_return"], -5.0)
        self.assertEqual(result["missed_winners"], 0)

    def test_market_tiers_include_rotation_and_divergence_in_tradable_baseline(self):
        condition = next(item for item in backtest.CONDITIONS if item["id"] == "strong_market_confirmation")
        rows = [
            {"market_state_label": "趋势扩散", "return_5d": 6},
            {"market_state_label": "结构性强势", "return_5d": 4},
            {"market_state_label": "震荡轮动", "return_5d": 2},
            {"market_state_label": "结构分化", "return_5d": -1},
            {"market_state_label": "修复观察", "return_5d": 1},
            {"market_state_label": "弱势下行", "return_5d": -4},
        ]

        result = backtest.condition_horizon(rows, condition, 5)

        self.assertEqual(result["baseline_samples"], 5)
        self.assertEqual(result["retained_samples"], 2)
        self.assertEqual(result["excluded_samples"], 3)
        self.assertEqual(result["weak_samples"], 1)

    def test_setup_profile_keeps_grade_and_maturity_inside_family(self):
        rows = [
            {"setup_family": "BREAKOUT", "entry_grade": "A", "maturity_stage": "VCP_TIGHT", "return_5d": 5},
            {"setup_family": "BREAKOUT", "entry_grade": "REGULAR", "maturity_stage": "VCP_MATURE", "return_5d": -1},
            {"setup_family": "PULLBACK", "entry_grade": "A", "maturity_stage": "VCP_TIGHT", "return_5d": 3},
        ]

        profile = backtest.build_analysis_profile(rows, "BREAKOUT")

        self.assertEqual(profile["events"], 2)
        self.assertEqual({row["group"] for row in profile["grade_groups"]}, {"A", "REGULAR"})
        self.assertEqual({row["group"] for row in profile["maturity_groups"]}, {"VCP_TIGHT", "VCP_MATURE"})

    def test_sample_window_filters_every_profile_by_trade_day_age(self):
        rows = [
            {"entry_date": "2026-08-03", "age_days": 5, "setup_family": "BREAKOUT", "entry_grade": "A", "maturity_stage": "VCP_TIGHT", "return_5d": 5},
            {"entry_date": "2026-07-13", "age_days": 20, "setup_family": "PULLBACK", "entry_grade": "REGULAR", "maturity_stage": "VCP_MATURE", "return_5d": -1, "return_10d": 2, "return_20d": 3},
            {"entry_date": "2026-07-10", "age_days": 21, "setup_family": "RETEST", "entry_grade": "A", "maturity_stage": "VCP_TIGHT", "return_5d": 4, "return_10d": 5, "return_20d": 6},
        ]

        window = backtest.build_sample_window(rows, {"id": "20D", "label": "近20个交易日", "max_age_trade_days": 20})

        self.assertEqual(window["summary"]["events"], 2)
        self.assertEqual([row["age_days"] for row in window["events"]], [5, 20])
        self.assertEqual(window["summary"]["sample_start_date"], "2026-07-13")
        self.assertEqual(next(row for row in window["setup_profiles"] if row["setup_family"] == "ALL")["events"], 2)
        self.assertEqual({row["group"] for row in window["setup_groups"]}, {"BREAKOUT", "PULLBACK"})


if __name__ == "__main__":
    unittest.main()
