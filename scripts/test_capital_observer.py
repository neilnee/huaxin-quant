import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.capital_observer import (
    CONFIG,
    calculate_turnover_candidates,
    classify_main_order,
    classify_stock_capital,
    build_threshold_reference,
    select_stock_check_candidates,
)


class CapitalObserverTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "market.sqlite"
        conn = sqlite3.connect(self.db_path)
        conn.executescript(
            """
            CREATE TABLE securities(code TEXT PRIMARY KEY, name TEXT);
            CREATE TABLE daily_bars(code TEXT, trade_date TEXT, amount REAL);
            CREATE TABLE stock_industries(snapshot_date TEXT, code TEXT, sw_industry_code TEXT);
            CREATE TABLE industry_definitions(
                snapshot_date TEXT, industry_system TEXT, industry_code TEXT, industry_name TEXT
            );
            """
        )
        dates = [f"2026-07-{day:02d}" for day in range(1, 16)]
        for industry, name in (("X1001", "迁入行业"), ("X1002", "对照行业")):
            conn.execute("INSERT INTO industry_definitions VALUES(?,?,?,?)", (dates[-1], "sw", industry, name))
        for index in range(1, 7):
            code = f"{index:06d}"
            industry = "X1001" if index <= 3 else "X1002"
            conn.execute("INSERT INTO securities VALUES(?,?)", (code, f"N{code}"))
            conn.execute("INSERT INTO stock_industries VALUES(?,?,?)", (dates[-1], code, industry + "01"))
            for offset, date in enumerate(dates):
                recent = offset >= len(dates) - 3
                amount = 200.0 if index <= 3 and recent else 100.0 if index <= 3 else 800.0 if recent else 900.0
                conn.execute("INSERT INTO daily_bars VALUES(?,?,?)", (code, date, amount))
        conn.commit()
        conn.close()
        self.config = copy.deepcopy(CONFIG)
        self.config["turnover"]["minimum_market_share"] = 0.0

    def tearDown(self):
        self.tempdir.cleanup()

    def test_turnover_scan_selects_relative_share_migration(self):
        result = calculate_turnover_candidates("2026-07-15", self.db_path, self.config)
        self.assertEqual(result["trade_date"], "2026-07-15")
        self.assertEqual([row["sw_code"] for row in result["candidates"]], ["X1001"])
        self.assertGreater(result["candidates"][0]["share_lift_3d"], 0.9)
        self.assertEqual(result["candidates"][0]["breadth"], 1.0)

    def test_main_order_state_uses_order_flow_only(self):
        rows = [
            {"trade_date": f"2026-07-{day:02d}", "main_net_inflow_ratio": ratio}
            for day, ratio in enumerate((0.001, 0.002, 0.006, 0.011, 0.018), 1)
        ]
        result = classify_main_order(rows, self.config)
        self.assertEqual(result["state"], "INFLOW_ACCELERATING")
        self.assertEqual(result["positive_days_5d"], 5)

    def test_main_order_marks_recent_inflow_as_weakening_when_latest_turns_negative(self):
        rows = [
            {"trade_date": f"2026-07-{day:02d}", "main_net_inflow_ratio": ratio}
            for day, ratio in enumerate((0.02, 0.03, 0.04, 0.02, -0.001), 1)
        ]
        self.assertEqual(classify_main_order(rows, self.config)["state"], "INFLOW_WEAKENING")

    def test_stock_margin_is_independent_from_main_order(self):
        stock = {"amount": 1_000_000.0, "amount_ratio_20d": 1.8}
        rows = [
            {"trade_date": "2026-07-01", "main_net_inflow": -20_000.0, "amount": 1_000_000.0, "financing_balance": 1_000_000.0},
            {"trade_date": "2026-07-02", "main_net_inflow": -15_000.0, "amount": 1_000_000.0, "financing_buy": 100_000.0, "financing_repay": 80_000.0, "financing_balance": 1_020_000.0},
        ]
        result = classify_stock_capital(stock, rows, self.config)
        self.assertEqual(result["turnover_state"], "ENHANCED")
        self.assertEqual(result["main_order_state"], "OUTFLOW")
        self.assertEqual(result["margin_state"], "LEVERAGING")

    def test_threshold_reference_is_derived_from_config(self):
        reference = build_threshold_reference(self.config)
        entry = {row["metric"]: row["standard"] for row in reference["entry_rules"]}
        self.assertEqual(entry["近3日份额提升"], "≥ 15%")
        self.assertEqual(entry["放量股票占比"], "≥ 30%")
        self.assertEqual(entry["最终展示数量"], "最多 8 个")
        self.assertTrue(any(row["state"] == "融资增加" for row in reference["state_rules"]))

    def test_unmapped_sector_can_still_enter_stock_check(self):
        candidates = [
            {"sw_code": "X1", "migration_score": 90, "mapping": {"quality": "high"}, "main_order": {"state": "INFLOW_PERSISTENT"}},
            {"sw_code": "X2", "migration_score": 85, "mapping": {"quality": "unavailable"}, "main_order": {"state": "MAPPING_UNRELIABLE"}},
        ]
        main_confirmed, selected, fallback = select_stock_check_candidates(candidates, self.config)
        self.assertEqual([row["sw_code"] for row in main_confirmed], ["X1"])
        self.assertEqual([row["sw_code"] for row in selected], ["X1", "X2"])
        self.assertEqual([row["sw_code"] for row in fallback], ["X2"])


if __name__ == "__main__":
    unittest.main()
