import fcntl
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import Mock, patch
from contextlib import redirect_stdout

import runner
from telegram_bridge.state import StateError, StateStore


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state_path = self.root / "private/state.sqlite3"
        with StateStore(self.state_path):
            pass
        self.record = self.state_path.parent / "process.json"
        self.log = self.state_path.parent / "bridge.log"

    def tearDown(self):
        self.temporary.cleanup()

    def test_existing_state_lock_is_detected_without_database_changes(self):
        before = self.state_path.read_bytes()
        self.assertFalse(runner.state_locked(self.state_path))
        with StateStore(self.state_path):
            self.assertTrue(runner.state_locked(self.state_path))
        self.assertFalse(runner.state_locked(self.state_path))
        self.assertEqual(before, self.state_path.read_bytes())

    def test_process_identity_checks_start_ticks_and_exact_script(self):
        bot = Path("/example/bot.py")
        with patch("runner.process_start_ticks", return_value="100"), \
                patch("runner.Path.read_bytes", return_value=b"python3\0/example/bot.py\0"):
            self.assertTrue(runner.process_matches({"pid": 123, "start_ticks": "100"}, bot))
            self.assertFalse(runner.process_matches({"pid": 123, "start_ticks": "99"}, bot))
            self.assertFalse(runner.process_matches({"pid": 123, "start_ticks": "100"}, Path("/different/bot.py")))
        self.assertFalse(runner.process_matches({"pid": True, "start_ticks": "100"}, bot))

    def test_zombie_process_is_not_running(self):
        fields = ["Z"] + ["0"] * 18 + ["123"]
        with patch("runner.Path.read_text", return_value="12 (python) " + " ".join(fields)):
            self.assertIsNone(runner.process_start_ticks(12))

    def test_start_does_not_launch_duplicate_when_instance_locked(self):
        with StateStore(self.state_path), patch("runner.subprocess.Popen") as popen, redirect_stdout(io.StringIO()):
            self.assertEqual(runner._start(self.state_path, self.record, self.log), 0)
        popen.assert_not_called()

    def test_launch_is_detached_and_credentials_not_in_arguments(self):
        child = Mock(pid=321)
        child.poll.return_value = None
        with patch("runner.subprocess.Popen", return_value=child) as popen, \
                patch("runner.process_start_ticks", return_value="123"), \
                patch("runner.process_matches", side_effect=lambda record, _: bool(record)), \
                patch("runner.state_locked", side_effect=[False, True]), redirect_stdout(io.StringIO()):
            self.assertEqual(runner._start(self.state_path, self.record, self.log), 0)
        args, kwargs = popen.call_args
        self.assertEqual(args[0], [runner.sys.executable, "-u", str(runner.PROJECT_ROOT / "bot.py")])
        self.assertTrue(kwargs["start_new_session"])
        self.assertTrue(kwargs["close_fds"])
        self.assertEqual(kwargs["stdin"], runner.subprocess.DEVNULL)
        self.assertEqual(self.record.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.log.stat().st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(self.record.read_text()), {"pid": 321, "start_ticks": "123"})

    def test_failed_child_is_not_retried(self):
        child = Mock(pid=321)
        child.poll.return_value = 1
        with patch("runner.subprocess.Popen", return_value=child) as popen, \
                patch("runner.process_start_ticks", return_value="123"), \
                patch("runner.process_matches", return_value=False), \
                patch("runner.state_locked", return_value=False), redirect_stdout(io.StringIO()):
            self.assertEqual(runner._start(self.state_path, self.record, self.log), 1)
        popen.assert_called_once()

    def test_symlink_private_log_is_refused(self):
        outside = self.root / "outside.txt"
        outside.write_text("preserve")
        self.log.symlink_to(outside)
        with patch("runner.subprocess.Popen") as popen, self.assertRaises(OSError):
            runner._start(self.state_path, self.record, self.log)
        popen.assert_not_called()
        self.assertEqual(outside.read_text(), "preserve")

    def test_status_does_not_launch_or_modify_files(self):
        before = sorted(p.name for p in self.state_path.parent.iterdir())
        with patch("runner.load_config", return_value=SimpleNamespace(state_path=self.state_path)), \
                patch("runner.subprocess.Popen") as popen, redirect_stdout(io.StringIO()) as output:
            self.assertEqual(runner.main(["--status"]), 0)
        popen.assert_not_called()
        self.assertIn("stopped", output.getvalue())
        self.assertEqual(before, sorted(p.name for p in self.state_path.parent.iterdir()))


if __name__ == "__main__":
    unittest.main()
