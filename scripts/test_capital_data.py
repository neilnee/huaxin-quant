import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.data.capital_data_service import CapitalDataService, build_sector_mappings
from scripts.data.capital_data_sources import (
    MiaoxiangCapitalSource,
    RequestBudget,
    RequestBudgetExceeded,
    parse_cn_number,
)
from scripts.shared import expected_trade_date
from scripts.strategy_config import load_strategy_config


class FakeBoardSource:
    def __init__(self):
        self.catalog_calls = 0
        self.catalog_levels = []
        self.member_calls = []
        self.boards = {
            "BK1000": {"name": "示例行业", "members": ["000001", "000002", "000003", "000004", "000005"]},
            "BK2000": {"name": "其他行业", "members": ["000006"]},
            "BK1100": {"name": "示例二级", "members": ["000001", "000002", "000003", "000004", "000005"]},
        }

    def fetch_catalog(self, level=1):
        self.catalog_calls += 1
        self.catalog_levels.append(level)
        codes = ["BK1000", "BK2000"] if level == 1 else ["BK1100"]
        return [{"bk_code": code, "bk_name": self.boards[code]["name"]} for code in codes]

    def fetch_members(self, bk_code):
        self.member_calls.append(bk_code)
        return [{"stock_code": code, "stock_name": f"N{code}"} for code in self.boards[bk_code]["members"]]


class FakeCapitalSource:
    def __init__(self, drop_from_large_batch=False):
        self.sector_calls = []
        self.stock_calls = []
        self.drop_from_large_batch = drop_from_large_batch

    def fetch_sector(self, bk_code, bk_name, start_date, end_date, fortune_level=1):
        self.sector_calls.append((bk_code, start_date, end_date))
        rows = [{
            "trade_date": start_date,
            "amount": 1_000_000.0,
            "main_net_inflow": 100_000.0,
        }]
        contracts = {(bk_code, "main_net_inflow"): {
            "source_field_code": "field_main", "source_field_name": "主力净流入资金(合计)"
        }}
        return rows, contracts, "sector query", None

    def fetch_stocks(self, entities, start_date, end_date):
        codes = [item["code"] for item in entities]
        self.stock_calls.append(codes)
        returned = codes[:-1] if self.drop_from_large_batch and len(codes) > 1 else codes
        rows = {code: [{
            "trade_date": start_date,
            "main_net_inflow": 10_000.0,
            "financing_balance": 20_000.0,
        }] for code in returned}
        contracts = {(code, "main_net_inflow"): {
            "source_field_code": "field_main", "source_field_name": "主力净流入资金"
        } for code in returned}
        return rows, set(codes) - set(returned), contracts, "stock query", None


class CapitalDataTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = Path(self.tempdir.name)
        self.market_db = root / "market.sqlite"
        self.capital_db = root / "capital.sqlite"
        conn = sqlite3.connect(self.market_db)
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
        self.trade_date = expected_trade_date()
        conn.execute("INSERT INTO industry_definitions VALUES(?,?,?,?)", (self.trade_date, "sw", "X10", "示例行业"))
        conn.execute("INSERT INTO industry_definitions VALUES(?,?,?,?)", (self.trade_date, "sw", "X1001", "示例二级"))
        for index in range(1, 7):
            code = f"{index:06d}"
            conn.execute("INSERT INTO securities VALUES(?,?)", (code, f"N{code}"))
            conn.execute("INSERT INTO stock_industries VALUES(?,?,?)", (self.trade_date, code, "X100101"))
            conn.execute("INSERT INTO daily_bars VALUES(?,?,?)", (code, self.trade_date, float(700 - index * 10)))
        conn.commit()
        conn.close()
        self.config = copy.deepcopy(load_strategy_config("capital-data.json")[0])

    def tearDown(self):
        self.tempdir.cleanup()

    def service(self, board_source=None, capital_source=None):
        return CapitalDataService(
            config=self.config,
            market_db_path=self.market_db,
            capital_db_path=self.capital_db,
            board_source=board_source or FakeBoardSource(),
            capital_source=capital_source or FakeCapitalSource(),
        )

    def test_parse_chinese_number_units(self):
        self.assertEqual(parse_cn_number("1.5亿元"), 150_000_000.0)
        self.assertEqual(parse_cn_number("320万元"), 3_200_000.0)
        self.assertEqual(parse_cn_number("2.5万亿"), 2_500_000_000_000.0)
        self.assertIsNone(parse_cn_number("-"))

    def test_miaoxiang_budget_stops_before_request(self):
        source = MiaoxiangCapitalSource(request_budget=RequestBudget(0))
        with self.assertRaises(RequestBudgetExceeded):
            source.fetch_stocks([{"code": "000001", "name": "示例"}], self.trade_date, self.trade_date)

    def test_mapping_can_select_multiple_boards_for_combined_coverage(self):
        sw = {"X10": {"name": "测试", "members": {"000001", "000002", "000003", "000004"}}}
        boards = {
            "BK1": {"name": "前半", "members": {"000001", "000002"}},
            "BK2": {"name": "后半", "members": {"000003", "000004"}},
        }
        amounts = {code: 100.0 for code in sw["X10"]["members"]}
        rows = build_sector_mappings(sw, boards, amounts, self.config["mapping"])
        self.assertEqual([row["bk_code"] for row in rows], ["BK2", "BK1"])
        self.assertEqual(rows[0]["combined_coverage"], 1.0)
        self.assertEqual(sum(row["mapping_weight"] for row in rows), 1.0)

    def test_mapping_rejects_broad_low_purity_board(self):
        sw = {"X10": {"name": "测试", "members": {"000001", "000002"}}}
        broad_members = {f"{index:06d}" for index in range(1, 11)}
        boards = {"BK1": {"name": "宽泛板块", "members": broad_members}}
        amounts = {code: 100.0 for code in broad_members}
        rows = build_sector_mappings(sw, boards, amounts, self.config["mapping"])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["bk_code"])
        self.assertEqual(rows[0]["mapping_quality"], "unavailable")

    def test_mapping_is_cached_until_force_rebuild(self):
        board_source = FakeBoardSource()
        service = self.service(board_source=board_source)
        first = service.ensure_sector_mapping(self.trade_date)
        second = service.ensure_sector_mapping(self.trade_date)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(board_source.catalog_calls, 1)
        third = service.ensure_sector_mapping(self.trade_date, force_rebuild=True)
        self.assertFalse(third["reused"])
        self.assertEqual(board_source.catalog_calls, 2)

    def test_mapping_cache_is_isolated_by_industry_level(self):
        board_source = FakeBoardSource()
        service = self.service(board_source=board_source)
        level_one = service.ensure_sector_mapping(self.trade_date, sw_level=1)
        level_two = service.ensure_sector_mapping(self.trade_date, sw_level=2)
        self.assertEqual(level_one["sw_level"], 1)
        self.assertEqual(level_two["sw_level"], 2)
        self.assertEqual(board_source.catalog_levels, [1, 2])

    def test_sector_fetch_resolves_sw_mapping_and_uses_cache(self):
        board_source, capital_source = FakeBoardSource(), FakeCapitalSource()
        service = self.service(board_source, capital_source)
        first = service.fetch_sector_capital("X10", self.trade_date, self.trade_date)
        second = service.fetch_sector_capital("X10", self.trade_date, self.trade_date)
        self.assertEqual(first["status"], "complete")
        self.assertEqual(first["mapped_blocks"][0]["bk_code"], "BK1000")
        self.assertAlmostEqual(first["rows"][0]["main_net_inflow_ratio"], 0.1)
        self.assertEqual(second["status"], "complete")
        self.assertEqual(len(capital_source.sector_calls), 1)

    def test_stock_fetch_batches_five_and_retries_silent_omission(self):
        capital_source = FakeCapitalSource(drop_from_large_batch=True)
        service = self.service(capital_source=capital_source)
        result = service.fetch_stock_capital(
            [f"{index:06d}" for index in range(1, 7)], self.trade_date, self.trade_date
        )
        self.assertEqual(capital_source.stock_calls[0], ["000001", "000002", "000003", "000004", "000005"])
        self.assertEqual(capital_source.stock_calls[1], ["000005"])
        self.assertEqual(capital_source.stock_calls[2], ["000006"])
        self.assertEqual(result["status"], "complete")
        self.assertTrue(all(item["rows"] for item in result["stocks"].values()))

    def test_stock_cache_avoids_second_source_call(self):
        capital_source = FakeCapitalSource()
        service = self.service(capital_source=capital_source)
        service.fetch_stock_capital(["000001", "000002"], self.trade_date, self.trade_date)
        service.fetch_stock_capital(["000001", "000002"], self.trade_date, self.trade_date)
        self.assertEqual(len(capital_source.stock_calls), 1)
        conn = sqlite3.connect(self.capital_db)
        payload = json.loads(conn.execute("SELECT metrics_json FROM stock_capital LIMIT 1").fetchone()[0])
        conn.close()
        self.assertEqual(payload["financing_balance"], 20_000.0)

    def test_contract_accepts_equivalent_label_when_source_id_changes(self):
        service = self.service()
        service._save_contracts("stock", "000001", {
            ("000001", "main_net_inflow"): {
                "source_field_code": "old_interval_id",
                "source_field_name": "(区间)主力净流入资金",
            }
        })
        error = service._contract_error("stock", "000001", {
            ("000001", "main_net_inflow"): {
                "source_field_code": "new_daily_id",
                "source_field_name": "主力净流入资金",
            }
        })
        self.assertIsNone(error)

    def test_contract_rejects_id_change_with_different_semantics(self):
        service = self.service()
        service._save_contracts("stock", "000001", {
            ("000001", "main_net_inflow"): {
                "source_field_code": "old_main_id",
                "source_field_name": "主力净流入资金",
            }
        })
        error = service._contract_error("stock", "000001", {
            ("000001", "main_net_inflow"): {
                "source_field_code": "wrong_margin_id",
                "source_field_name": "融资余额",
            }
        })
        self.assertIn("字段语义契约变化", error)


if __name__ == "__main__":
    unittest.main()
