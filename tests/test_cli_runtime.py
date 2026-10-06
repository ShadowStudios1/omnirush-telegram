import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from telegram_bridge import cli_runtime


class CliRuntimeTests(unittest.TestCase):
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
