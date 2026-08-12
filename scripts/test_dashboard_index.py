import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from scripts.dashboard_index import update_dashboard_module


def publish(data_dir: str, kind: str, date: str) -> None:
    update_dashboard_module(Path(data_dir), kind, [date])


class DashboardIndexTest(unittest.TestCase):
    def test_concurrent_publishers_preserve_both_modules(self):
        with tempfile.TemporaryDirectory() as directory:
            processes = [
                multiprocessing.Process(target=publish, args=(directory, "market", "260810")),
                multiprocessing.Process(target=publish, args=(directory, "signals", "260811")),
            ]
            for process in processes:
                process.start()
            for process in processes:
                process.join(5)
                self.assertEqual(process.exitcode, 0)
            text = (Path(directory) / "index.js").read_text(encoding="utf-8")
            payload = json.loads(text.split(" = ", 1)[1].rsplit(";", 1)[0])
            self.assertEqual(payload["market"]["available"], ["260810"])
            self.assertEqual(payload["signals"]["available"], ["260811"])
