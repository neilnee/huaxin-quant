import json
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path
from unittest.mock import patch

from scripts import quant_filter, strategy_publish, valuate
from scripts.data.strategy_data_store import (
    connect,
    lifecycle_latest,
    load_document,
    load_latest_pool_tracking_before,
    load_pool_tracking_rows,
    load_vcp_selection_events,
    replace_lifecycle_rows,
    save_bloom,
    save_pool_tracking_rows,
    save_buy_point_events,
    save_quant,
    save_signal_plan,
)
from scripts.strategy_publish import ordered_bloom_state


class StrategyDataStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.conn = connect(Path(self.temp.name) / "strategy.sqlite")

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_quant_round_trip_and_idempotency(self):
        payload = {
            "meta": {"run_date": "2026-08-12", "schema": "q", "strategy_version": "v1"},
            "results": [{"code": "1", "name": "A", "structure_type": "VCP",
                         "structure_stage": "VCP_FORMING", "structure_valid": True,
                         "structure_score": 80, "strategy_version": "v1"}],
        }
        first = save_quant(self.conn, payload)
        second = save_quant(self.conn, payload)
        self.assertEqual(first, second)
        self.assertEqual(load_document(self.conn, "quant", "2026-08-12"), payload)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM vcp_structure_snapshots").fetchone()[0], 1)

    def test_quant_manual_names_do_not_need_exports(self):
        directory = Path(self.temp.name)
        index = directory / "valuation_index.csv"
        index.write_text("code,name\n5,索引名称\n", encoding="utf-8")
        (directory / "pool_260812.csv").write_text(
            "股票代码,股票名称\n2,股票池名称\n4,股票池旧名称\n", encoding="utf-8"
        )
        (directory / "pool_260814.csv").write_text("code,name\n4,未来名称\n", encoding="utf-8")
        save_quant(self.conn, {"meta": {"run_date": "2026-08-11"}, "results": [
            {"code": "1", "name": "前日名称"},
        ]})
        for name in ("旧修订", "当前修订"):
            save_quant(self.conn, {"meta": {"run_date": "2026-08-12"}, "results": [
                {"code": "1", "name": name}, {"code": "5", "name": "量价名称"},
            ]})
        save_quant(self.conn, {"meta": {"run_date": "2026-08-14"}, "results": [
            {"code": "1", "name": "未来名称"},
        ]})

        args = Namespace(code=None, name=None, codes="1,2,3,4,5", test=False)
        with patch.multiple(
            quant_filter, STRATEGY_DB_PATH=directory / "strategy.sqlite",
            VALUATION_INDEX_PATH=index, POOL_DIR=directory,
        ):
            self.assertEqual(quant_filter.resolve_input_codes(args, "260812"), ([
                ("000001", "当前修订"), ("000002", "股票池名称"), ("000003", "000003"),
                ("000004", "股票池旧名称"), ("000005", "索引名称"),
            ], "多股"))
            args.code, args.name = "1", "手工输入"
            self.assertEqual(quant_filter.resolve_input_codes(args, "260812"), (
                [("000001", "手工输入")], "单股"
            ))

    def test_valuation_pool_uses_sorted_selected_quant_rows_without_csv(self):
        save_quant(self.conn, {"meta": {"run_date": "2026-08-12"}, "results": [
            {"code": "1", "name": "低分", "model2_include": True, "structure_score": 50},
            {"code": "2", "name": "未入选", "model2_include": False, "structure_score": 90},
            {"code": "3", "name": "高分", "model2_include": True, "structure_score": 70},
        ]})
        save_quant(self.conn, {"meta": {"run_date": "2026-08-13"}, "results": []})
        save_quant(self.conn, {"meta": {"run_date": "2026-08-14"}, "results": [
            {"code": "4", "name": "未来名单", "model2_include": True, "structure_score": 80},
        ]})
        with patch.object(valuate, "STRATEGY_DB_PATH", Path(self.temp.name) / "strategy.sqlite"):
            self.assertEqual(valuate.read_quant_pool("260813"), [
                {"code": "000003", "name": "高分"}, {"code": "000001", "name": "低分"},
            ])
            self.assertEqual(valuate.read_quant_pool("260811"), [])

    def test_quant_run_writes_compatibility_files_only_when_requested(self):
        directory = Path(self.temp.name)
        (directory / "pool_260812.csv").write_text("股票代码,股票名称\n1,测试\n", encoding="utf-8")
        rows = [
            {"code": "000001", "name": "测试", "model2_include": True, "structure_score": 50},
            {"code": "000002", "name": "排除", "model2_include": False, "structure_score": 60},
        ]
        stats = dict(pull_ok=2, pull_fail=0, data_insufficient=0, cache_hits=2, api_calls=0, tdx_calls=0)
        json_path = directory / "runs" / "quant_260812.json"
        argv = ["quant_filter.py", "--date", "260812"]
        with (
            patch.multiple(quant_filter, POOL_DIR=directory, QUANT_DIR=directory, QUANT_RUNS_DIR=json_path.parent),
            patch.object(quant_filter, "load_local_env"),
            patch.object(quant_filter, "connect_strategy_db", return_value=self.conn),
            patch.object(quant_filter, "process_codes", return_value=(rows, stats)),
            patch.object(quant_filter, "print_summary"),
            patch.object(quant_filter, "write_csv") as write_csv,
            patch("builtins.print"),
        ):
            with patch("sys.argv", argv):
                quant_filter.main()
            write_csv.assert_not_called()
            self.assertFalse(json_path.exists())
            saved_payload = load_document(self.conn, "quant", "2026-08-12")
            self.assertIsNone(saved_payload["meta"]["csv_path"])
            self.assertEqual(saved_payload["results"], rows)

            with patch("sys.argv", [*argv, "--quant", "selected.csv", "--output-json", str(json_path)]):
                quant_filter.main()
            write_csv.assert_called_once_with([rows[0]], "selected.csv")
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), load_document(self.conn, "quant", "2026-08-12"))
            write_csv.reset_mock()

            with patch("sys.argv", [*argv, "--csv"]):
                quant_filter.main()
            write_csv.assert_called_once_with([rows[0]], str(directory / "quant_260812.csv"))
            write_csv.reset_mock()

            with patch("sys.argv", [*argv, "--include-reject"]):
                quant_filter.main()
            write_csv.assert_called_once_with(list(reversed(rows)), str(directory / "quant_260812.csv"))

    def test_strategy_quant_publisher_separates_json_rebuild_and_csv_export(self):
        directory = Path(self.temp.name)
        trade_date = "2026-08-12"
        row = {"code": "000001", "name": "测试", "model2_include": True, "structure_score": 50}
        payload = {"meta": {"run_date": trade_date}, "results": [row]}
        save_quant(self.conn, payload)
        with patch.object(strategy_publish, "ROOT", directory), patch.object(quant_filter, "write_csv") as write_csv:
            json_path = directory / "cache" / "quant_runs" / "quant_260812.json"
            self.assertEqual(strategy_publish.publish_quant(self.conn, trade_date, "260812"), [json_path])
            self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), payload)
            write_csv.assert_not_called()
            json_path.unlink()
            expected_csv = directory / "quant" / "quant_260812.csv"
            self.assertEqual(
                strategy_publish.publish_quant(self.conn, trade_date, "260812", csv_only=True), [expected_csv]
            )
            write_csv.assert_called_once_with([row], str(expected_csv))
            self.assertFalse(json_path.exists())

    def test_pool_tracking_replace_and_strict_prior_load(self):
        def row(date, status):
            return {
                "trade_date": date, "code": "000001", "name": "测试",
                "tracking_status": status, "resolution_status": "FINAL",
                "first_seen_date": "2026-08-10", "last_rs_eligible_date": "2026-08-10",
                "rs_current_eligible": status == "RS_ACTIVE", "grace_trade_days": 0,
                "grace_remaining_days": 20, "strategy_version": "p1",
            }

        save_pool_tracking_rows(self.conn, "2026-08-10", [row("2026-08-10", "RS_ACTIVE")])
        save_pool_tracking_rows(self.conn, "2026-08-10", [row("2026-08-10", "STRUCTURE_TRACKED")])
        self.assertEqual(len(load_pool_tracking_rows(self.conn, "2026-08-10")), 1)
        self.assertEqual(load_latest_pool_tracking_before(self.conn, "2026-08-10"), {})
        prior = load_latest_pool_tracking_before(self.conn, "2026-08-11")
        self.assertEqual(prior["000001"]["tracking_status"], "STRUCTURE_TRACKED")

    def test_pool_liquidity_audit_survives_pending_and_final_storage(self):
        row = {
            "trade_date": "2026-09-14", "code": "301108", "name": "洁雅股份",
            "tracking_status": "STRUCTURE_TRACKED", "resolution_status": "PENDING",
            "first_seen_date": "2026-08-31", "rs_current_eligible": False,
            "liquidity_filter_exempt": True, "liquidity_structure_active": True,
            "quant_stage": "VCP_FORMING", "bloom_status": "COOLDOWN",
            "post_breakout_state": "POST_BREAKOUT_HOT",
        }
        save_pool_tracking_rows(self.conn, row["trade_date"], [row])
        self.assertTrue(load_pool_tracking_rows(self.conn, row["trade_date"])["301108"]["liquidity_filter_exempt"])
        self.assertEqual(load_latest_pool_tracking_before(self.conn, "2026-09-15"), {})
        row["resolution_status"] = "FINAL"
        save_pool_tracking_rows(self.conn, row["trade_date"], [row])
        prior = load_latest_pool_tracking_before(self.conn, "2026-09-15")["301108"]
        self.assertTrue(prior["liquidity_filter_exempt"])
        self.assertTrue(prior["liquidity_structure_active"])

    def test_plan_and_buy_events_are_stable(self):
        plan = {"code": "000001", "structure_anchor": "2026-07-01",
                "setup_family": "BREAKOUT", "plan_action": "NEW"}
        payload = {"meta": {"date": "2026-08-11", "strategy_version": "p1"}, "plans": [plan]}
        save_signal_plan(self.conn, payload)
        event = {**plan, "plan_date": "2026-08-11", "entry_date": "2026-08-12",
                 "signal_close_snapshot": 10, "invalid_price": 9}
        save_buy_point_events(self.conn, [event])
        save_buy_point_events(self.conn, [event])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM signal_plans").fetchone()[0], 1)
        self.assertEqual(self.conn.execute("SELECT count(*) FROM buy_point_events").fetchone()[0], 1)

        event_id = self.conn.execute("SELECT event_id FROM buy_point_events").fetchone()[0]
        replace_lifecycle_rows(self.conn, event_id, [
            {"trade_date": "2026-08-13", "age_trade_days": 1, "close": 10.5,
             "hit_1r": False, "lifecycle_status": "OPEN"},
            {"trade_date": "2026-08-14", "age_trade_days": 2, "close": 11.0,
             "hit_1r": True, "lifecycle_status": "OPEN"},
        ])
        self.assertEqual(lifecycle_latest(self.conn, "2026-08-13")[0]["trade_date"], "2026-08-13")
        self.assertEqual(lifecycle_latest(self.conn)[0]["trade_date"], "2026-08-14")

    def test_bloom_publisher_uses_runtime_state_order(self):
        state = {
            "cooldown": {"code": "000003", "bloom_status": "COOLDOWN", "structure_score": 99},
            "early": {"code": "000002", "bloom_status": "EARLY", "structure_score": 20},
            "forming": {"code": "000001", "bloom_status": "FORMING", "structure_score": 30},
            "exit": {"code": "000004", "bloom_status": "EXIT", "structure_score": 100},
        }
        self.assertEqual(
            [row["code"] for row in ordered_bloom_state(state)],
            ["000001", "000002", "000003"],
        )

    def test_vcp_selection_uses_first_display_day_per_structure_round(self):
        def quant(date, anchor, close):
            payload = {
                "meta": {"run_date": date, "strategy_version": "q1"},
                "results": [{"code": "000001", "name": "测试", "structure_type": "VCP",
                             "structure_stage": "VCP_FORMING", "structure_valid": True,
                             "contraction_group": [{"start_date": anchor}], "close": close}],
            }
            save_quant(self.conn, payload)

        def bloom(date, first_seen):
            payload = {"summary": {"date": date, "strategy_version": "b1"}, "sections": {}}
            save_bloom(self.conn, payload, [{"code": "000001", "name": "测试",
                       "bloom_status": "FORMING", "model2_stage": "VCP_FORMING",
                       "first_seen": first_seen}])

        quant("2026-08-10", "2026-07-01", 10)
        bloom("2026-08-10", "2026-08-10")
        quant("2026-08-11", "2026-07-01", 11)
        bloom("2026-08-11", "2026-08-10")
        quant("2026-08-12", "2026-08-12", 12)
        bloom("2026-08-12", "2026-08-12")

        events = load_vcp_selection_events(self.conn, "2026-08-12")
        self.assertEqual([row["selection_date"] for row in events], ["2026-08-10", "2026-08-12"])
        self.assertEqual([row["structure_anchor"] for row in events], ["2026-07-01", "2026-08-12"])
        valid_events = load_vcp_selection_events(
            self.conn, "2026-08-12", {"2026-08-11", "2026-08-12"}
        )
        self.assertEqual(
            [row["selection_date"] for row in valid_events],
            ["2026-08-11", "2026-08-12"],
        )


if __name__ == "__main__":
    unittest.main()
