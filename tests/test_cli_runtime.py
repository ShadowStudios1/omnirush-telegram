import fcntl
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from telegram_bridge import cli_runtime


class CliRuntimeTests(unittest.TestCase):
    def test_portable_install_lock_does_not_leave_official_lock_file(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "official-cli"
            root.mkdir(mode=0o700)
            with cli_runtime._installation_lock(root):
                self.assertTrue((root / ".install.lock").is_file())
                self.assertTrue((root / ".portable-install.lock").is_file())
            self.assertFalse((root / ".install.lock").exists())

    def test_legacy_lock_file_is_migrated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "official-cli"
            root.mkdir(mode=0o700)
            legacy = root / ".install.lock"
            legacy.touch(mode=0o600)
            with cli_runtime._installation_lock(root):
                self.assertTrue(legacy.is_file())
            self.assertFalse(legacy.exists())

    def test_active_legacy_lock_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "official-cli"
            root.mkdir(mode=0o700)
            legacy = root / ".install.lock"
            legacy.touch(mode=0o600)
            with legacy.open("r+") as stream:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                with self.assertRaises(cli_runtime.CliRuntimeError):
                    with cli_runtime._installation_lock(root):
                        pass
                self.assertTrue(legacy.is_file())

    def test_stale_official_lock_directory_is_recovered(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "official-cli"
            root.mkdir(mode=0o700)
            lock = root / ".install.lock"
            lock.mkdir(mode=0o700)
            owner = lock / "owner"
            dead_pid = 999999
            while cli_runtime._pid_alive(dead_pid):
                dead_pid += 1
            owner.write_text(str(dead_pid) + "\n", encoding="ascii")
            owner.chmod(0o600)
            old = time.time() - 120
            os.utime(lock, (old, old))
            with cli_runtime._installation_lock(root):
                self.assertTrue(lock.is_file())
            self.assertFalse(lock.exists())

    def test_live_official_lock_directory_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "official-cli"
            root.mkdir(mode=0o700)
            lock = root / ".install.lock"
            lock.mkdir(mode=0o700)
            owner = lock / "owner"
            owner.write_text(str(os.getpid()) + "\n", encoding="ascii")
            owner.chmod(0o600)
            old = time.time() - 120
            os.utime(lock, (old, old))
            with self.assertRaises(cli_runtime.CliRuntimeError):
                with cli_runtime._installation_lock(root):
                    pass
            self.assertTrue(lock.is_dir())

    def test_safe_archive_members_require_package_root(self):
        self.assertEqual(cli_runtime._safe_member("package/src/bin.js"), ("src", "bin.js"))
        for value in ("../outside", "/absolute", "package/../outside", "package\\bad"):
            with self.subTest(value=value), self.assertRaises(cli_runtime.CliRuntimeError):
                cli_runtime._safe_member(value)

    def test_launcher_is_real_official_cli_and_retains_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "official-cli"
            runtime = cli_runtime.OfficialCliRuntime(root, root / "engine", root / "bun", root)
            content = cli_runtime.launcher_contents(runtime)
            self.assertIn("exec '" + str(root / "bun") + "' '" + str(root / "node_modules/omnirush/src/bin.js") + "' \"$@\"", content)
            self.assertIn("OMNIRUSH_DIR=", content)

    def test_launcher_path_detection(self):
        with patch.object(cli_runtime, "LAUNCHER_PATH", Path("/home/test/.local/bin/omnirush")):
            self.assertTrue(cli_runtime.launcher_path_in_path("/usr/bin:/home/test/.local/bin"))
            self.assertFalse(cli_runtime.launcher_path_in_path("/usr/bin"))


if __name__ == "__main__":
    unittest.main()
