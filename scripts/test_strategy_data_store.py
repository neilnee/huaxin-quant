import tempfile
import unittest
from pathlib import Path

from scripts.data.strategy_data_store import (
    connect,
    lifecycle_latest,
    load_document,
    load_vcp_selection_events,
    replace_lifecycle_rows,
    save_bloom,
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
