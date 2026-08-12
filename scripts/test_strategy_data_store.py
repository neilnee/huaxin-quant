import tempfile
import unittest
from pathlib import Path

from scripts.data.strategy_data_store import (
    connect,
    lifecycle_latest,
    load_document,
    replace_lifecycle_rows,
    save_buy_point_events,
    save_quant,
    save_signal_plan,
)


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


if __name__ == "__main__":
    unittest.main()
