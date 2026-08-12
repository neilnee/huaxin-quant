import json
import os
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from scripts.run_valuation import (
    ControllerError,
    StockLock,
    build_financial_queries,
    build_pipeline_command,
    choose_recovery,
    safe_mx_filename,
    validate_code,
    validate_mx_raw,
    verify_terminal_run,
)


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


class RunValuationControllerTest(unittest.TestCase):
    def test_validate_code(self):
        self.assertEqual(validate_code("300442"), "300442")
        with self.assertRaisesRegex(ControllerError, "六位"):
            validate_code("30044")

    def test_financial_queries_are_bounded_and_cover_stage_zero_inputs(self):
        queries = build_financial_queries("300442", "润泽科技", annual_year=2025)
        self.assertEqual([item["topic"] for item in queries], ["core_financials", "profit_history", "valuation_base"])
        self.assertIn("2025年报", queries[0]["query"])
        self.assertIn("扣非净利润", queries[0]["query"])
        self.assertIn("2023年报", queries[1]["query"])
        self.assertIn("总股本", queries[2]["query"])
        self.assertEqual(len({safe_mx_filename(item["query"]) for item in queries}), 3)

    def test_validate_mx_raw_requires_matching_stock_and_raw_table(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.json"
            write_json(
                path,
                {
                    "status": 0,
                    "data": {"data": {"searchDataResultDTO": {
                        "questionId": "q1",
                        "dataTableDTOList": [{
                            "code": "300442.SZ",
                            "entityTagDTO": {"secuCode": "300442"},
                            "rawTable": {"headName": ["2025年报"], "f1": [1]},
                        }],
                    }}},
                },
            )
            self.assertEqual(validate_mx_raw(path, "300442")["table_count"], 1)
            with self.assertRaisesRegex(ControllerError, "未包含"):
                validate_mx_raw(path, "688285")

    def test_recovery_uses_stage5_when_research_is_complete_without_card(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            write_json(run_dir / "manifest.json", {"status": "failed", "error": "阶段五 LLM 调用失败"})
            for filename in (
                "stage_1_business.json",
                "stage_2_consensus.json",
                "stage_3_expectations.json",
                "stage_4_catalysts.json",
            ):
                write_json(run_dir / filename, {})
            decision = choose_recovery(run_dir)
            self.assertEqual(decision.mode, "stage5")

    def test_recovery_reruns_stage5_contract_but_not_insufficient_consensus(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            for filename in (
                "stage_1_business.json",
                "stage_2_consensus.json",
                "stage_3_expectations.json",
                "stage_4_catalysts.json",
                "research_card_raw.json",
                "research_card.json",
                "stage_5_research.json",
                "stage_5_mapping.json",
            ):
                write_json(run_dir / filename, {})
            write_json(run_dir / "manifest.json", {"status": "failed", "error": "valuation_inputs PE 区间必须有效"})
            self.assertEqual(choose_recovery(run_dir).mode, "stage5")
            write_json(run_dir / "manifest.json", {"status": "failed", "error": "一致预期不足：当前 2 家"})
            self.assertEqual(choose_recovery(run_dir).mode, "resume")

    def test_pipeline_command_keeps_resume_evidence_boundary(self):
        args = Namespace(
            name="润泽科技",
            refresh=False,
            no_fetch_evidence=True,
            no_publish=True,
            search_cache_hours=48,
        )
        run_dir = Path("/tmp/valuation_run")
        command = build_pipeline_command(args, "300442", run_dir, choose_recovery_fixture())
        self.assertIn("--resume-run", command)
        self.assertIn("--rerun-stage5", command)
        self.assertIn("--no-fetch-evidence", command)
        self.assertIn("--no-publish", command)
        self.assertNotIn("--refresh-evidence", command)

    def test_terminal_verification_requires_dashboard_only_when_publishing(self):
        with tempfile.TemporaryDirectory() as directory:
            run_dir = Path(directory)
            for filename in (
                "briefing.json",
                "evidence.json",
                "stage_5_research.json",
                "stage_5_mapping.json",
                "research_card.json",
                "calc_params.json",
                "calc_results.json",
            ):
                write_json(run_dir / filename, {})
            write_json(
                run_dir / "manifest.json",
                {"status": "done", "stages": {"render": {"status": "skipped"}, "dashboard": {"status": "skipped"}}},
            )
            self.assertEqual(verify_terminal_run(run_dir, no_publish=True)["manifest_status"], "done")
            with self.assertRaisesRegex(ControllerError, "发布未完成"):
                verify_terminal_run(run_dir, no_publish=False)

    def test_stock_lock_rejects_live_owner_and_recovers_dead_owner(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "300442.lock"
            write_json(path, {"pid": os.getpid()})
            live_lock = StockLock("300442")
            live_lock.path = path
            with self.assertRaisesRegex(ControllerError, "已有总控任务"):
                live_lock.acquire()
            write_json(path, {"pid": 99999999})
            stale_lock = StockLock("300442")
            stale_lock.path = path
            stale_lock.acquire()
            self.assertTrue(path.exists())
            stale_lock.release()
            self.assertFalse(path.exists())


def choose_recovery_fixture():
    from scripts.run_valuation import RecoveryDecision

    return RecoveryDecision("stage5", "test")


if __name__ == "__main__":
    unittest.main()
