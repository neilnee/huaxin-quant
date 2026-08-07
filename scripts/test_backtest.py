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
            with patch.object(backtest, "MARKET_DB", db_path), patch.object(backtest, "trading_calendar", return_value=calendar), patch.object(backtest, "load_signal_environment", return_value=environment):
                result = backtest.add_performance([event], "260708")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["age_days"], 7)
        self.assertEqual(result[0]["return_5d"], 50.0)
        self.assertIsNone(result[0]["return_10d"])
        self.assertIsNone(result[0]["return_20d"])
        self.assertEqual(result[0]["breakout_time"], "2026-07-04 · T+3")
        self.assertEqual(result[0]["breakout_return"], 8.333)

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


if __name__ == "__main__":
    unittest.main()
