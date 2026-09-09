import unittest

from scripts.data.pool_tracking import (
    ACTIVE,
    EXITED,
    GRACE,
    STRUCTURE,
    finalize_tracking_rows,
    prepare_tracking_rows,
)


class PoolTrackingTests(unittest.TestCase):
    def candidate(self, code="000001", current=False, reason=""):
        return {
            "code": code,
            "name": "测试",
            "rs_current_eligible": current,
            "hard_filter_reason": reason,
        }

    def test_current_rs_starts_or_resets_cycle(self):
        prior = {
            "000001": {
                "trade_date": "2026-01-01",
                "code": "000001",
                "tracking_status": EXITED,
                "first_seen_date": "2025-12-01",
            }
        }
        row = prepare_tracking_rows(
            "2026-01-02", [self.candidate(current=True)], prior,
            ["2026-01-01", "2026-01-02"], 20,
        )[0]
        self.assertEqual(row["tracking_status"], ACTIVE)
        self.assertEqual(row["first_seen_date"], "2026-01-02")
        self.assertEqual(row["last_rs_eligible_date"], "2026-01-02")
        self.assertEqual(row["grace_trade_days"], 0)

    def test_grace_keeps_day_20_and_exits_day_21(self):
        dates = [f"2026-01-{day:02d}" for day in range(1, 23)]
        prior = {
            "000001": {
                "trade_date": dates[0], "code": "000001", "name": "测试",
                "tracking_status": ACTIVE, "first_seen_date": dates[0],
                "last_rs_eligible_date": dates[0],
            }
        }
        day20 = prepare_tracking_rows(
            dates[20], [self.candidate()], prior, dates[:21], 20
        )[0]
        day21 = prepare_tracking_rows(
            dates[21], [self.candidate()], prior, dates, 20
        )[0]
        self.assertEqual((day20["tracking_status"], day20["grace_trade_days"]), (GRACE, 20))
        self.assertEqual((day21["tracking_status"], day21["exit_reason"]), (EXITED, "GRACE_EXPIRED"))

    def test_structure_owns_lifecycle_and_terminal_resets_grace(self):
        pending = prepare_tracking_rows(
            "2026-01-02", [self.candidate()], {
                "000001": {
                    "trade_date": "2026-01-01", "code": "000001", "name": "测试",
                    "tracking_status": STRUCTURE, "first_seen_date": "2025-12-01",
                    "last_rs_eligible_date": "2025-12-15",
                }
            }, ["2026-01-01", "2026-01-02"], 20,
        )
        self.assertEqual(pending[0]["tracking_status"], STRUCTURE)
        final = finalize_tracking_rows(
            "2026-01-02", pending, {}, {
                "000001": {"bloom_status": "EXIT", "post_breakout_state": "POST_BREAKOUT_FAILED"}
            }, 20,
        )[0]
        self.assertEqual(final["tracking_status"], GRACE)
        self.assertEqual(final["grace_start_date"], "2026-01-02")
        self.assertEqual(final["grace_trade_days"], 0)

    def test_nonterminal_bloom_takes_over_and_data_issue_freezes_grace(self):
        pending = prepare_tracking_rows(
            "2026-01-03", [self.candidate()], {
                "000001": {
                    "trade_date": "2026-01-02", "code": "000001", "name": "测试",
                    "tracking_status": GRACE, "first_seen_date": "2026-01-01",
                    "last_rs_eligible_date": "2026-01-01", "grace_start_date": "2026-01-01",
                    "grace_trade_days": 1, "grace_remaining_days": 19,
                }
            }, ["2026-01-01", "2026-01-02", "2026-01-03"], 20,
        )
        frozen = finalize_tracking_rows(
            "2026-01-03", pending, {}, {"000001": {"bloom_status": "DATA_ISSUE"}}, 20
        )[0]
        self.assertEqual((frozen["tracking_status"], frozen["grace_trade_days"]), (GRACE, 1))

        structured = finalize_tracking_rows(
            "2026-01-03", pending, {}, {"000001": {"bloom_status": "FORMING"}}, 20
        )[0]
        self.assertEqual(structured["tracking_status"], STRUCTURE)
        self.assertEqual(structured["grace_trade_days"], 0)

    def test_data_issue_preserves_zero_remaining_on_day_20(self):
        pending = prepare_tracking_rows(
            "2026-01-22", [self.candidate()], {
                "000001": {
                    "trade_date": "2026-01-21", "code": "000001", "name": "测试",
                    "tracking_status": GRACE, "first_seen_date": "2026-01-01",
                    "last_rs_eligible_date": "2026-01-01", "grace_start_date": "2026-01-01",
                    "grace_trade_days": 20, "grace_remaining_days": 0,
                }
            }, ["2026-01-22"], 20,
        )
        frozen = finalize_tracking_rows(
            "2026-01-22", pending, {}, {"000001": {"bloom_status": "DATA_ISSUE"}}, 20
        )[0]
        self.assertEqual(frozen["grace_trade_days"], 20)
        self.assertEqual(frozen["grace_remaining_days"], 0)

    def test_hard_filter_exits_immediately(self):
        row = prepare_tracking_rows(
            "2026-01-02", [self.candidate(reason="LOW_LIQUIDITY")], {
                "000001": {"tracking_status": STRUCTURE, "first_seen_date": "2026-01-01"}
            }, ["2026-01-02"], 20,
        )[0]
        self.assertEqual(row["tracking_status"], EXITED)
        self.assertEqual(row["exit_reason"], "HARD_FILTER:LOW_LIQUIDITY")

    def test_bootstrap_preserves_existing_bloom_candidate(self):
        row = prepare_tracking_rows(
            "2026-01-02", [self.candidate()], {}, ["2026-01-02"], 20,
            bootstrap_codes={"000001"},
        )[0]
        self.assertEqual(row["tracking_status"], STRUCTURE)
        self.assertEqual(row["tracking_source"], "BLOOM_BOOTSTRAP")


if __name__ == "__main__":
    unittest.main()
