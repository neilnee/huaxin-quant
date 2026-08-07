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


def row(code="000001", anchor="2026-01-01", stage="NONE", setup="NONE", close=10):
    return {
        "code": code, "name": "测试", "structure_stage": stage, "setup_signal": setup,
        "close": close, "contraction_group": [{"start_date": anchor}],
    }


class BacktestEventTests(unittest.TestCase):
    def test_first_event_only_and_new_structure_can_reenter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payloads = {
                "260701": [row(stage="VCP_MATURE")],
                "260702": [row(stage="VCP_TIGHT", setup="BREAKOUT_BUY")],
                "260703": [row(stage="VCP_TIGHT", setup="BREAKOUT_BUY")],
                "260706": [row(anchor="2026-06-20", stage="VCP_MATURE")],
            }
            for date, rows in payloads.items():
                (root / f"quant_{date}.json").write_text(
                    json.dumps(quant_payload(f"20{date[:2]}-{date[2:4]}-{date[4:]}", rows)), encoding="utf-8"
                )
            with patch.object(backtest, "QUANT_DIR", root):
                events = backtest.discover_events("260706")

        self.assertEqual([event["event_type"] for event in events], ["MATURE_ENTRY", "SETUP_TRIGGER", "MATURE_ENTRY"])
        self.assertEqual(events[1]["signal_date_yy"], "260702")

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
