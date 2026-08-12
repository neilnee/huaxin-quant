import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import init_runtime


class InitRuntimeTest(unittest.TestCase):
    def test_initializes_current_runtime_layout_and_check_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch("sys.argv", ["init_runtime.py", "--root", directory]):
                self.assertEqual(init_runtime.main(), 0)
            root = Path(directory)
            self.assertTrue((root / "position" / "position_plan.csv").exists())
            self.assertTrue((root / ".tmp" / "locks").is_dir())
            self.assertFalse((root / "signals").exists())
            with patch("sys.argv", ["init_runtime.py", "--root", directory, "--check"]):
                self.assertEqual(init_runtime.main(), 0)


if __name__ == "__main__":
    unittest.main()
