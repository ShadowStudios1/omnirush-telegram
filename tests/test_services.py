import contextlib
import os
from pathlib import Path
import signal
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from telegram_bridge import services


def config(folder):
    return SimpleNamespace(executable="/native/sidecar", backend_mode="headless", permission_mode="ask",
                           model=None, agent=None, server_url="managed", state_path=Path(folder) / "state.sqlite3")


class UnitTests(unittest.TestCase):
    def test_headless_user_units_private_backoff_loopback_runtime(self):
        units = services.render_units(config("/private"), root=Path('/repo with spaces/%/$/"'), python="/python with spaces")
        self.assertEqual(set(units), {services.BOT_UNIT, services.BACKEND_UNIT})
        for contents in units.values():
            self.assertIn("UMask=0077", contents)
            self.assertIn("Restart=on-failure", contents)
            self.assertIn("RestartSec=10s", contents)
            self.assertIn("StartLimitBurst=5", contents)
            self.assertIn("StandardOutput=null", contents)
            self.assertNotIn("User=root", contents)
            self.assertNotIn("0.0.0.0", contents)
        self.assertIn("-m telegram_bridge.runtime serve", units[services.BACKEND_UNIT])
        self.assertIn('"/python with spaces"', units[services.BOT_UNIT])
        self.assertIn("%%", units[services.BOT_UNIT])
        self.assertIn("$$", units[services.BOT_UNIT])

    def test_desktop_has_no_backend_unit_or_account_mutation(self):
        cfg = config("/private")
        cfg.backend_mode = "desktop"
        units = services.render_units(cfg)
        self.assertEqual(set(units), {services.BOT_UNIT})
        self.assertNotIn("auth", units[services.BOT_UNIT])
        self.assertNotIn(services.BACKEND_UNIT, units[services.BOT_UNIT])

    def test_control_char_paths_rejected(self):
        with self.assertRaises(services.ServiceError):
            services.render_units(config("/private"), root=Path("/repo\nmalicious"))

    def test_unavailable_manager_installs_nothing(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "units"
            with patch.object(services, "systemd_user_available", return_value=False):
                with self.assertRaisesRegex(services.ServiceError, "foreground|run"):
                    services.install(config(folder), directory=destination)
            self.assertFalse(destination.exists())

    def test_install_enable_only_on_request_and_private_files(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder) / "units"
            with patch.object(services, "systemd_user_available", return_value=True), \
                 patch.object(services, "_systemctl") as systemctl:
                services.install(config(folder), directory=destination)
                self.assertEqual(systemctl.call_args_list[0].args, ("daemon-reload",))
                self.assertEqual(systemctl.call_count, 1)
                self.assertEqual((destination / services.BOT_UNIT).stat().st_mode & 0o777, 0o600)
                systemctl.reset_mock()
                services.install(config(folder), enable=True, directory=destination)
                self.assertEqual(systemctl.call_args_list[-1].args[0], "enable")
                self.assertNotIn("--now", systemctl.call_args_list[-1].args)

    def test_unrelated_existing_unit_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)
            path = destination / services.BOT_UNIT
            path.write_text("unrelated")
            with patch.object(services, "systemd_user_available", return_value=True), \
                 patch.object(services, "_systemctl") as systemctl:
                with self.assertRaises(services.ServiceError):
                    services.install(config(folder), directory=destination)
            self.assertEqual(path.read_text(), "unrelated")
            systemctl.assert_not_called()

    def test_symlink_unit_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            destination = Path(folder)
            target = destination / "target"
            target.write_text("unrelated")
            (destination / services.BOT_UNIT).symlink_to(target)
            with patch.object(services, "systemd_user_available", return_value=True):
                with self.assertRaises(services.ServiceError):
                    services.install(config(folder), directory=destination)
            self.assertEqual(target.read_text(), "unrelated")

    def test_uninstall_only_own_units_preserves_private_state(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / services.BOT_UNIT).write_text(services.MARKER + "owned")
            (root / services.BACKEND_UNIT).write_text("unrelated")
            state = root / "state.sqlite3"
            state.write_text("preserved")
            with patch.object(services, "UNIT_DIR", root), \
                 patch.object(services, "systemd_user_available", return_value=True), \
                 patch.object(services, "_systemctl") as systemctl:
                services.uninstall()
            self.assertFalse((root / services.BOT_UNIT).exists())
            self.assertEqual((root / services.BACKEND_UNIT).read_text(), "unrelated")
            self.assertEqual(state.read_text(), "preserved")
            self.assertEqual(systemctl.call_args_list[0].args, ("disable", "--now", services.BOT_UNIT))

    def test_linger_no_sudo_and_failure_safe(self):
        with patch.object(services.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
            with self.assertRaisesRegex(services.ServiceError, "No sudo"):
                services.enable_linger()
        self.assertEqual(run.call_args.args[0][:2], ["loginctl", "enable-linger"])
        self.assertNotIn("sudo", run.call_args.args[0])


class LifecycleTests(unittest.TestCase):
    def test_start_idempotent_while_running(self):
        with patch.object(services, "is_running", return_value=True), \
             patch.object(services.subprocess, "Popen") as popen, patch.object(services, "_systemctl") as systemctl:
            self.assertIn("nothing", services.start(config("/private")))
            popen.assert_not_called()
            systemctl.assert_not_called()

    def test_installed_unreachable_does_not_spawn_fallback(self):
        with patch.object(services, "is_running", return_value=False), \
             patch.object(services, "units_installed", return_value=True), \
             patch.object(services, "systemd_user_available", return_value=False), \
             patch.object(services.subprocess, "Popen") as popen:
            with self.assertRaises(services.ServiceError):
                services.start(config("/private"))
            popen.assert_not_called()

    def test_stop_refuses_reused_or_unverified_pid(self):
        primitives = Mock()
        primitives.process_matches.return_value = False
        primitives.state_locked.return_value = True
        with patch.object(services, "units_installed", return_value=False), \
             patch.object(services, "_primitives", return_value=primitives), \
             patch.object(services, "_record", return_value={"pid": 123, "start_ticks": "1"}), \
             patch.object(services.os, "kill") as kill:
            with self.assertRaisesRegex(services.ServiceError, "no signal"):
                services.stop(config("/private"))
            kill.assert_not_called()

    def test_stop_verified_supervisor_preserves_metadata(self):
        primitives = Mock()
        primitives.process_matches.side_effect = [True, False]
        record = {"pid": 123, "start_ticks": "1"}
        with patch.object(services, "units_installed", return_value=False), \
             patch.object(services, "_primitives", return_value=primitives), \
             patch.object(services, "_record", return_value=record), \
             patch.object(services.os, "kill") as kill:
            self.assertIn("preserved", services.stop(config("/private")))
            kill.assert_called_once_with(123, signal.SIGTERM)

    def test_stop_stale_supervisor_does_not_signal(self):
        primitives = Mock()
        primitives.process_matches.return_value = False
        primitives.state_locked.return_value = False
        with patch.object(services, "units_installed", return_value=False), \
             patch.object(services, "_primitives", return_value=primitives), \
             patch.object(services, "_record", return_value={"pid": 123, "start_ticks": "1"}), \
             patch.object(services.os, "kill") as kill:
            self.assertIn("stale metadata preserved", services.stop(config("/private")))
            kill.assert_not_called()

    def test_private_fallback_start_and_no_reboot_claim(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            child = Mock()
            child.poll.return_value = None
            import runner
            with patch.object(services, "is_running", return_value=False), \
                 patch.object(services, "units_installed", return_value=False), \
                 patch.object(services.subprocess, "Popen", return_value=child) as popen, \
                 patch.object(services, "_alive", return_value=True), \
                 patch.object(runner, "state_locked", return_value=True):
                result = services.start(cfg)
            self.assertIn("no reboot", result)
            self.assertTrue(popen.call_args.kwargs["start_new_session"])
            self.assertEqual(popen.call_args.args[0][-1], "run")
            self.assertEqual((Path(folder) / "supervisor.log").stat().st_mode & 0o777, 0o600)

    def test_status_unreachable_manager_unknown_not_always_on(self):
        with patch.object(services, "units_installed", return_value=True), \
             patch.object(services, "systemd_user_available", return_value=False):
            self.assertIn("unknown", services.status(config("/private")))

    def test_run_stops_bot_on_normal_exit_with_private_verified_record(self):
        with tempfile.TemporaryDirectory() as folder:
            cfg = config(folder)
            cfg.backend_mode = "desktop"
            client = Mock()
            child = Mock()
            child.poll.return_value = 0
            import runner
            with patch.object(services, "_client", return_value=client), \
                 patch.object(runner, "state_locked", return_value=False), \
                 patch.object(services.subprocess, "Popen", return_value=child):
                self.assertEqual(services.run(cfg), 0)
            record_path = Path(folder) / "supervisor.json"
            self.assertEqual(record_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(runner._read_record(record_path)["pid"], os.getpid())
            client.health.assert_called_once()

    def test_run_already_running_does_not_launch_child(self):
        with tempfile.TemporaryDirectory() as folder:
            import runner
            with patch.object(runner, "state_locked", return_value=True), \
                 patch.object(services.subprocess, "Popen") as popen:
                with self.assertRaisesRegex(services.ServiceError, "already running"):
                    services.run(config(folder))
                popen.assert_not_called()

    def test_systemctl_timeout_never_exposes_output(self):
        with patch.object(services.subprocess, "run", side_effect=subprocess.TimeoutExpired("SECRET", 5)):
            with self.assertRaises(services.ServiceError) as raised:
                services._systemctl("start", services.BOT_UNIT)
            self.assertNotIn("SECRET", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
