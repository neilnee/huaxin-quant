import tempfile
import unittest
from pathlib import Path

from scripts.position import append_rows, read_csv_rows


class PositionPersistenceTest(unittest.TestCase):
    def test_append_is_atomic_and_rejects_duplicate_trade_id(self):
        fields = ["trade_id", "code"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "ledger.csv"
            append_rows(path, fields, [{"trade_id": "T1", "code": "000001"}])
            with self.assertRaisesRegex(ValueError, "duplicate trade_id"):
                append_rows(path, fields, [{"trade_id": "T1", "code": "000002"}])
            self.assertEqual(read_csv_rows(path), [{"trade_id": "T1", "code": "000001"}])
            self.assertFalse(list(path.parent.glob(f".{path.name}.*.tmp")))


if __name__ == "__main__":
    unittest.main()
