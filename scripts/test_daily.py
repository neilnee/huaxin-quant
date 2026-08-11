import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import daily


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
            root / "market" / "data" / "market_context_260810.json",
            month / "capital_context_260810.js",
            month / "market_context_260810.js",
            month / "vcp_context_260810.js",
            month / "signals_context_260810.js",
            month / "backtest_context_260810.js",
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
            daily.run_capital_observer(self.DATE)
        command = run.call_args.args[0]
        self.assertEqual(
            command,
            [
                "python3",
                "scripts/capital_observer.py",
                "run",
                "--date",
                "2026-08-10",
                "--fetch",
            ],
        )

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


if __name__ == "__main__":
    unittest.main()
