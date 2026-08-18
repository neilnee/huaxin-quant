import json
import inspect
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import daily, monitor
from scripts.progress_utils import ProgressTracker


class DailyCapitalWorkflowTests(unittest.TestCase):
    DATE = "260810"

    def _build_complete_outputs(self, root: Path, signal_fetch_enabled: bool = True) -> None:
        month = root / "dashboard" / "data" / "202608"
        required = [
            root / "pool" / "pool_260810.csv",
            root / "cache" / "quant_runs" / "quant_260810.json",
            root / "bloom" / "state" / "bloom_input_260810.json",
            root / "signal_plan" / "signal_plan_260810.json",
            root / "capital" / "capital_observer_260810.json",
            root / "market" / "market_regime_260810.json",
            root / "market" / "data" / "market_context_260810.json",
            month / "capital_context_260810.js",
            month / "market_context_260810.js",
            month / "vcp_context_260810.js",
            month / "signals_context_260810.js",
            month / "backtest_context_260810.js",
            root / "reports" / "ai_daily" / "202608" / "huaxin_quant_ai_report_260810.json",
        ]
        for path in required:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")

        capital = {
            "meta": {
                "trade_date": "2026-08-10",
                "fetch_enabled": True,
                "status": "complete",
            }
        }
        (root / "capital" / "capital_observer_260810.json").write_text(
            json.dumps(capital), encoding="utf-8"
        )
        market_report = {
            "meta": {"run_date": "2026-08-10"},
            "llm": {"status": "success", "analysis": "LLM 市场解读"},
        }
        (root / "market" / "market_regime_260810.json").write_text(
            json.dumps(market_report), encoding="utf-8"
        )
        market_context = {
            "market_state": {"analysis_source": "llm", "analysis": "LLM 市场解读"}
        }
        (root / "market" / "data" / "market_context_260810.json").write_text(
            json.dumps(market_context), encoding="utf-8"
        )
        ai_report = {
            "schema_version": "huaxin_ai_daily_v1.1",
            "report_date": "2026-08-10",
        }
        (root / "reports" / "ai_daily" / "202608" / "huaxin_quant_ai_report_260810.json").write_text(
            json.dumps(ai_report), encoding="utf-8"
        )
        signals = {
            "meta": {"capital_fetch_enabled": signal_fetch_enabled},
            "signals": [{"code": "000001", "capital_support": {"status": "unavailable"}}],
        }
        (month / "signals_context_260810.js").write_text(
            "window.QUANT_DASHBOARD_SIGNALS_CONTEXTS = window.QUANT_DASHBOARD_SIGNALS_CONTEXTS || {};\n"
            f'window.QUANT_DASHBOARD_SIGNALS_CONTEXTS["{self.DATE}"] = '
            + json.dumps(signals)
            + ";\n",
            encoding="utf-8",
        )
        index = {
            module: {"latest": self.DATE, "available": [self.DATE]}
            for module in ("capital", "market", "vcp", "signals", "backtest")
        }
        (root / "dashboard" / "data" / "index.js").write_text(
            "window.QUANT_DASHBOARD_INDEX = " + json.dumps(index) + ";\n",
            encoding="utf-8",
        )

    def test_capital_observer_command_fetches_the_requested_date(self):
        with patch.object(daily.subprocess, "run") as run:
            run.return_value.returncode = 0
            daily.run_capital_observer(self.DATE)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                sys.executable,
                "scripts/capital_observer.py",
                "run",
                "--date",
                "2026-08-10",
                "--fetch",
            ],
        )

    def test_ai_daily_report_runs_as_date_scoped_deterministic_step(self):
        with patch.object(daily.subprocess, "run") as run:
            run.return_value.returncode = 0
            daily.run_ai_daily_report(self.DATE)
        self.assertEqual(
            run.call_args.args[0],
            [sys.executable, "scripts/daily_ai_report.py", "--date", self.DATE],
        )

    def test_ai_daily_report_is_after_dashboard_and_before_verification(self):
        source = inspect.getsource(daily.main)

        self.assertLess(source.index("run_dashboard_publish(date_yy)"), source.index("run_ai_daily_report(date_yy)"))
        self.assertLess(source.index("run_ai_daily_report(date_yy)"), source.index("verify_pipeline_outputs(date_yy)"))

    def test_verify_requires_full_and_signal_capital_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_complete_outputs(root)
            with patch.object(daily, "PROJECT_ROOT", str(root)):
                self.assertTrue(daily.verify_pipeline_outputs(self.DATE))

            (root / "capital" / "capital_observer_260810.json").unlink()
            with patch.object(daily, "PROJECT_ROOT", str(root)):
                self.assertFalse(daily.verify_pipeline_outputs(self.DATE))

    def test_verify_rejects_disabled_signal_capital_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_complete_outputs(root, signal_fetch_enabled=False)
            with patch.object(daily, "PROJECT_ROOT", str(root)):
                self.assertFalse(daily.verify_pipeline_outputs(self.DATE))

    def test_verify_rejects_missing_or_wrong_ai_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_complete_outputs(root)
            report_path = root / "reports" / "ai_daily" / "202608" / "huaxin_quant_ai_report_260810.json"
            report_path.unlink()
            with patch.object(daily, "PROJECT_ROOT", str(root)):
                self.assertFalse(daily.verify_pipeline_outputs(self.DATE))

            self._build_complete_outputs(root)
            report_path.write_text(
                json.dumps({"schema_version": "huaxin_ai_daily_v1", "report_date": "2026-08-10"}),
                encoding="utf-8",
            )
            with patch.object(daily, "PROJECT_ROOT", str(root)):
                self.assertFalse(daily.verify_pipeline_outputs(self.DATE))

    def test_verify_rejects_market_rule_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._build_complete_outputs(root)
            report_path = root / "market" / "market_regime_260810.json"
            report = json.loads(report_path.read_text(encoding="utf-8"))
            report["llm"] = {
                "status": "skipped",
                "reason": "disabled_by_flag",
                "analysis": "",
            }
            report_path.write_text(json.dumps(report), encoding="utf-8")
            context_path = root / "market" / "data" / "market_context_260810.json"
            context_path.write_text(
                json.dumps({
                    "market_state": {
                        "analysis_source": "rule_fallback",
                        "analysis": "规则降级解读",
                    }
                }),
                encoding="utf-8",
            )
            with patch.object(daily, "PROJECT_ROOT", str(root)):
                self.assertFalse(daily.verify_pipeline_outputs(self.DATE))

    def test_market_publish_retries_llm_without_rebuilding_mainline(self):
        first = type("Result", (), {"returncode": 0})()
        second = type("Result", (), {"returncode": 0})()
        with (
            patch.object(daily.subprocess, "run", side_effect=[first, second]) as run,
            patch.object(
                daily,
                "load_market_llm_meta",
                side_effect=[ValueError("LLM 失败"), {"status": "success"}],
            ),
        ):
            result = daily.run_market_publish(self.DATE)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 2)
        self.assertNotIn("--reuse-existing-mainline", run.call_args_list[0].args[0])
        self.assertIn("--reuse-existing-mainline", run.call_args_list[1].args[0])

    def test_idempotent_command_retries_once(self):
        failed = type("Result", (), {"returncode": 1})()
        success = type("Result", (), {"returncode": 0})()
        command = ["python3", "scripts/example.py"]
        with patch.object(daily.subprocess, "run", side_effect=[failed, success]) as run:
            result = daily.run_command_with_retries(command, label="test")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(run.call_count, 2)

    def test_stage_timeout_has_stable_exit_code(self):
        command = [sys.executable, "scripts/example.py"]
        with patch.object(daily.subprocess, "run", side_effect=daily.subprocess.TimeoutExpired(command, 1)):
            result = daily.run_stage(command, timeout=1)
        self.assertEqual(result.returncode, 124)

    def test_progress_tracker_records_failed_root_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            tracker = ProgressTracker(path)
            tracker.init(["dashboard"])
            tracker.mark_failed("dashboard: exit 4")
            progress = ProgressTracker.read(path)
        self.assertEqual(progress["status"], "failed")
        self.assertEqual(progress["failure_reason"], "dashboard: exit 4")

    def test_progress_tracker_records_degraded_and_skipped_steps(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "progress.json"
            tracker = ProgressTracker(path)
            tracker.init(["bloom", "zixuan"])
            tracker.step_start("bloom")
            tracker.step_degraded("bloom", "LLM unavailable")
            tracker.step_skipped("zixuan", "disabled")
            tracker.mark_degraded()
            progress = ProgressTracker.read(path)
        self.assertEqual(progress["status"], "degraded")
        self.assertEqual(progress["steps"]["bloom"]["status"], "degraded")
        self.assertEqual(progress["steps"]["zixuan"]["status"], "skipped")

    def test_monitor_renders_degraded_pipeline_as_successful_terminal(self):
        markdown = monitor._build_progress_markdown({
            "date": self.DATE,
            "status": "degraded",
            "started_at": "2026-08-10T15:00:00",
            "total_elapsed_s": 12,
            "steps": {"bloom": {"status": "degraded", "reason": "LLM unavailable"}},
        })
        self.assertIn("⚠️ 已降级完成", markdown)
        self.assertIn("LLM unavailable", markdown)

    def test_monitor_renders_failed_pipeline_as_terminal(self):
        markdown = monitor._build_progress_markdown({
            "date": self.DATE,
            "status": "failed",
            "started_at": "2026-08-10T15:00:00",
            "total_elapsed_s": 12,
            "failure_reason": "dashboard: exit 4",
            "steps": {},
        })
        self.assertIn("❌ 已中止", markdown)
        self.assertIn("流水线已中止：dashboard: exit 4", markdown)


if __name__ == "__main__":
    unittest.main()
