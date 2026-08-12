import json
import multiprocessing
import tempfile
import unittest
from pathlib import Path

from scripts.io_utils import FileLock, LockBusyError, atomic_write_json, atomic_write_text


def try_lock(path: str, result) -> None:
    try:
        with FileLock(path, blocking=False, purpose="child"):
            result.put("acquired")
    except LockBusyError:
        result.put("busy")


class IoUtilsTest(unittest.TestCase):
    def test_atomic_write_replaces_complete_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            atomic_write_text(path, "old")
            atomic_write_json(path, {"status": "done"})
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"status": "done"})
            self.assertFalse(list(path.parent.glob(f".{path.name}.*.tmp")))

    def test_nonblocking_lock_rejects_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "test.lock")
            queue = multiprocessing.Queue()
            with FileLock(path, blocking=False, purpose="parent"):
                process = multiprocessing.Process(target=try_lock, args=(path, queue))
                process.start()
                process.join(5)
                self.assertEqual(process.exitcode, 0)
                self.assertEqual(queue.get(timeout=1), "busy")
            with FileLock(path, blocking=False, purpose="reacquire"):
                pass


if __name__ == "__main__":
    unittest.main()
