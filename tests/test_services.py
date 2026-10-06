import base64
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


def basic_auth(value="opencode:private-test-password"):
    return "Basic " + base64.b64encode(value.encode("ascii")).decode("ascii")


def explicit_config(permission="ask"):
    cfg = config("/private")
    cfg.server_url = "http://127.0.0.1:43123"
    cfg.server_auth = basic_auth()
    cfg.permission_mode = permission
    return cfg


class BackendConfigurationTests(unittest.TestCase):
    def test_new_endpoint_uses_private_loopback_and_fixed_username(self):
        probe = Mock()
        probe.getsockname.return_value = ("127.0.0.1", 43123)
        socket = Mock()
        socket.__enter__ = Mock(return_value=probe)
        socket.__exit__ = Mock(return_value=False)
        with patch.object(services.socket, "socket", return_value=socket), \
             patch.object(services.secrets, "token_urlsafe", return_value="private-test-password"):
            endpoint, auth = services.new_backend_endpoint()
        self.assertEqual(endpoint, "http://127.0.0.1:43123")
        self.assertEqual(auth, basic_auth())
        probe.bind.assert_called_once_with(("127.0.0.1", 0))

    def test_invalid_explicit_endpoints_fail_before_authentication_or_spawn(self):
        invalid = [None, 123, "auto", "https://127.0.0.1:43123", "http://127.0.0.1",
                   "http://127.0.0.1:0", "http://127.0.0.1:65536", "http://127.0.0.1:bad",
                   "http://localhost:43123", "http://0.0.0.0:43123", "http://[::1]:43123",
                   "http://127.0.0.1:43123/path", "http://127.0.0.1:43123?secret=SECRET",
                   "http://127.0.0.1:43123#SECRET", "http://user:SECRET@127.0.0.1:43123",
                   "\nhttp://127.0.0.1:43123", "http://127.0.0.1:43123\n"]
        for endpoint in invalid:
            with self.subTest(endpoint=endpoint):
                cfg = explicit_config()
                cfg.server_url = endpoint
                with patch("telegram_bridge.runtime.authenticated_runtime_environment") as environment, \
                     patch.object(services, "_client") as client, \
                     patch.object(services.subprocess, "Popen") as popen:
                    for build in (services.backend_command, services.backend_environment):
                        with self.assertRaisesRegex(services.ServiceError, "endpoint is invalid") as raised:
                            build(cfg)
                        self.assertNotIn("SECRET", str(raised.exception))
                    with self.assertRaises(services.ServiceError):
                        with services.backend_process(cfg):
                            self.fail("Invalid backend must not start")
                    environment.assert_not_called()
                    client.assert_not_called()
                    popen.assert_not_called()

    def test_invalid_or_missing_explicit_auth_fails_without_spawning(self):
        invalid = [None, "", 123, "Bearer SECRET", "Basic !!!", "Basic /w==",
                   basic_auth("user:SECRET"), basic_auth("opencode:"), basic_auth("opencode"),
                   basic_auth("opencode:SECRET\n"), basic_auth("opencode:SECRET\x7f"),
                   basic_auth("opencode:two words"), basic_auth("opencode:" + "x" * 512)]
        for auth in invalid:
            with self.subTest(auth=auth):
                cfg = explicit_config()
                cfg.server_auth = auth
                with patch("telegram_bridge.runtime.authenticated_runtime_environment") as environment, \
                     patch.object(services, "_client") as client, \
                     patch.object(services.subprocess, "Popen") as popen:
                    for build in (services.backend_command, services.backend_environment):
                        with self.assertRaisesRegex(services.ServiceError, "authorization is invalid") as raised:
                            build(cfg)
                        self.assertNotIn("SECRET", str(raised.exception))
                    with self.assertRaises(services.ServiceError):
                        with services.backend_process(cfg):
                            self.fail("Unauthenticated backend must not start")
                    environment.assert_not_called()
                    client.assert_not_called()
                    popen.assert_not_called()

    def test_client_receives_explicit_auth_and_permission_policy(self):
        for permission in ("ask", "full"):
            with self.subTest(permission=permission), patch("telegram_bridge.agent.AgentClient") as client:
                services._client(explicit_config(permission))
                self.assertEqual(client.call_args.kwargs["server_auth"], basic_auth())
                self.assertEqual(client.call_args.kwargs["permission_mode"], permission)
                self.assertEqual(client.call_args.kwargs["backend_mode"], "headless")


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
        self.assertIn("Requires=" + services.BACKEND_UNIT, units[services.BOT_UNIT])
        self.assertIn('"/python with spaces"', units[services.BOT_UNIT])
        self.assertIn("WorkingDirectory=/repo\\x20with\\x20spaces/%%/$/\\x22", units[services.BOT_UNIT])
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
    def test_backend_process_uses_authenticated_native_environment_and_owns_cleanup(self):
        from telegram_bridge.agent import AgentError
        client = Mock()
        client.health.side_effect = [AgentError("not started"), None]
        child = Mock()
        child.poll.return_value = None
        environment = {"OMNIRUSH_GATEWAY_URL": "https://omnirush.ai/omnirush/v1",
                       "OMNIRUSH_ACCESS_TOKEN": "access"}
        with patch.object(services, "_client", return_value=client), \
             patch("telegram_bridge.runtime.authenticated_runtime_environment", return_value=environment), \
             patch.object(services.subprocess, "Popen", return_value=child) as popen:
            with services.backend_process(config("/private")) as active:
                self.assertIs(active, client)
                self.assertIs(active._portable_child, child)
        self.assertEqual(popen.call_args.kwargs["env"], environment)
        self.assertEqual(popen.call_args.args[0], [
            "/native/sidecar", "serve", "--service", "--hostname", "127.0.0.1",
        ])
        self.assertNotIn("access", popen.call_args.args[0])
        child.terminate.assert_called_once()

    def test_backend_process_uses_explicit_official_loopback_protocol(self):
        from telegram_bridge.agent import AgentError
        for permission in ("ask", "full"):
            with self.subTest(permission=permission):
                cfg = explicit_config(permission)
                client = Mock()
                client.health.side_effect = [AgentError("not started"), None]
                child = Mock()
                child.poll.return_value = None
                environment = {"OMNIRUSH_ACCESS_TOKEN": "access", "OPENCODE_PASSWORD": "inherited-secret",
                               "OPENCODE_SERVER_PASSWORD": "other-inherited-secret"}
                with patch.object(services, "_client", return_value=client), \
                     patch("telegram_bridge.runtime.authenticated_runtime_environment", return_value=environment), \
                     patch.object(services.subprocess, "Popen", return_value=child) as popen:
                    with services.backend_process(cfg) as active:
                        self.assertIs(active._portable_child, child)
                self.assertEqual(popen.call_args.args[0], [
                    "/native/sidecar", "serve", "--hostname", "127.0.0.1", "--port", "43123",
                ])
                env = popen.call_args.kwargs["env"]
                self.assertEqual(env["OPENCODE_SERVER_USERNAME"], "opencode")
                self.assertEqual(env["OPENCODE_SERVER_PASSWORD"], "private-test-password")
                self.assertEqual(env["OPENCODE_PASSWORD"], "private-test-password")
                self.assertEqual(env["npm_config_audit"], "false")
                self.assertEqual(popen.call_args.kwargs["stdout"], subprocess.DEVNULL)
                self.assertEqual(popen.call_args.kwargs["stderr"], subprocess.DEVNULL)
                self.assertNotIn("private-test-password", " ".join(popen.call_args.args[0]))
                self.assertNotIn(cfg.server_auth, "".join(services.render_units(cfg).values()))
                popen.assert_called_once()
                child.terminate.assert_called_once()
                child.wait.assert_called_once_with(timeout=15)

    def test_existing_backend_is_reused_without_spawn_or_cleanup(self):
        for permission in ("ask", "full"):
            with self.subTest(permission=permission):
                client = Mock()
                with patch.object(services, "_client", return_value=client), \
                     patch.object(services, "backend_environment") as environment, \
                     patch.object(services.subprocess, "Popen") as popen:
                    with services.backend_process(explicit_config(permission)) as active:
                        self.assertIs(active, client)
                        self.assertIsNone(active._portable_child)
                client.health.assert_called_once()
                environment.assert_not_called()
                popen.assert_not_called()

    def test_backend_startup_exit_reports_only_status_and_does_not_retry(self):
        from telegram_bridge.agent import AgentError
        for code, expected in ((0, "exit status 0"), (2, "exit status 2"), (-15, "signal 15 (return code -15)")):
            with self.subTest(code=code):
                client = Mock()
                client.health.side_effect = AgentError("SECRET diagnostic")
                child = Mock()
                child.poll.return_value = code
                with patch.object(services, "_client", return_value=client), \
                     patch.object(services, "backend_environment", return_value={"OPENCODE_PASSWORD": "SECRET"}), \
                     patch.object(services.subprocess, "Popen", return_value=child) as popen, \
                     patch.object(services, "_finish", wraps=services._finish) as finish, \
                     patch.object(services.time, "sleep") as sleep:
                    with self.assertRaises(services.ServiceError) as raised:
                        with services.backend_process(explicit_config()):
                            self.fail("Dead backend must never be yielded")
                self.assertIn(expected, str(raised.exception))
                self.assertIn("Check the private runtime configuration", str(raised.exception))
                self.assertIn("No automatic retry", str(raised.exception))
                self.assertNotIn("SECRET", str(raised.exception))
                popen.assert_called_once()
                client.health.assert_called_once()
                sleep.assert_not_called()
                finish.assert_called_once_with(child)
                child.terminate.assert_not_called()
                child.kill.assert_not_called()

    def test_backend_readiness_timeout_stops_owned_child_and_hides_diagnostics(self):
        from telegram_bridge.agent import AgentError
        client = Mock()
        client.health.side_effect = AgentError("SECRET diagnostic")
        child = Mock()
        child.poll.return_value = None
        with patch.object(services, "_client", return_value=client), \
             patch.object(services, "backend_environment", return_value={}), \
             patch.object(services.subprocess, "Popen", return_value=child) as popen, \
             patch.object(services.time, "monotonic", side_effect=[0, 91]):
            with self.assertRaisesRegex(services.ServiceError, "not ready") as raised:
                with services.backend_process(explicit_config()):
                    self.fail("Unready backend must not be yielded")
        self.assertNotIn("SECRET", str(raised.exception))
        popen.assert_called_once()
        child.terminate.assert_called_once()
        child.wait.assert_called_once_with(timeout=15)

    def test_systemd_start_uses_bot_dependency_without_detached_fallback(self):
        with patch.object(services, "is_running", return_value=False), \
             patch.object(services, "units_installed", return_value=True), \
             patch.object(services, "systemd_user_available", return_value=True), \
             patch.object(services, "status", return_value="active"), \
             patch.object(services, "_systemctl") as systemctl, \
             patch.object(services.subprocess, "Popen") as popen:
            self.assertEqual(services.start(config("/private")), "active")
        systemctl.assert_called_once_with("start", services.BOT_UNIT)
        popen.assert_not_called()

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
