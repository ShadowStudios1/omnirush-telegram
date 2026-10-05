import contextlib
import io
import os
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import omnirush
from telegram_bridge import environment, installer
from telegram_bridge.cli_ui import UI, TerminalError, require_terminal, safe_text


def config(root):
    return SimpleNamespace(token="123456:secret_hidden_token", owner_id=42, roots=(root,), project=root,
                           executable="/installed/sidecar", server_url="auto", backend_mode="desktop",
                           permission_mode="ask", model=None, agent=None, state_path=root / "state.sqlite3",
                           monitor_interval=5)


CATALOG = {"location": {}, "data": [
    {"id": "model-z", "providerID": "provider-b", "name": "Actual Model", "enabled": True,
     "limit": {"context": 128000}},
    {"id": "model-a", "providerID": "provider-a", "name": "Other Model", "enabled": True,
     "limit": {"context": 10000}},
    {"id": "disabled", "providerID": "provider-a", "enabled": False},
]}


class UITests(unittest.TestCase):
    def test_plain_and_no_color_no_escapes(self):
        stream = io.StringIO()
        with patch.dict(os.environ, {"NO_COLOR": ""}):
            ui = UI(stream=stream)
            ui.say("Success", "ok")
            with ui.busy("Work"):
                pass
        self.assertNotIn("\033", stream.getvalue())
        self.assertFalse(ui.animated)

    def test_spinner_joins_on_error(self):
        stream = Mock()
        stream.isatty.return_value = True
        with patch.dict(os.environ, {}, clear=True):
            ui = UI(stream=stream)
            with self.assertRaises(ValueError):
                with ui.busy("Work"):
                    raise ValueError()
        self.assertFalse(any(t.name == "omnirush-progress" for t in threading.enumerate()))

    def test_non_terminal_refuses_secret(self):
        with patch("sys.stdin.isatty", return_value=False), patch("getpass.getpass") as secret:
            with self.assertRaises(TerminalError):
                UI(plain=True).secret("Token")
            secret.assert_not_called()

    def test_external_controls_sanitized(self):
        self.assertNotIn("\033", safe_text("bad\033[31mname\n"))
        self.assertNotIn("\n", safe_text("line\n"))


class EnvironmentTests(unittest.TestCase):
    def test_systemctl_binary_without_manager_is_not_available(self):
        with patch.object(environment.shutil, "which", return_value="/usr/bin/systemctl"), \
             patch.object(environment.subprocess, "run", return_value=SimpleNamespace(returncode=1)) as run:
            self.assertFalse(environment.systemd_user_available())
            self.assertEqual(run.call_args.args[0], ["systemctl", "--user", "show-environment"])

    def test_manager_probe_timeout_is_false(self):
        with patch.object(environment.shutil, "which", return_value="systemctl"), \
             patch.object(environment.subprocess, "run", side_effect=environment.subprocess.TimeoutExpired("systemctl", 5)):
            self.assertFalse(environment.systemd_user_available())

    def test_local_wsl_ssh_rdp_container_signals(self):
        with patch.dict(os.environ, {"SSH_CONNECTION": "local", "XRDP_SESSION": "1", "container": "test"}, clear=True), \
             patch.object(environment, "_read", side_effect=lambda p: "6.1-microsoft" if "osrelease" in p else ""), \
             patch.object(environment, "distro_info", return_value={"PRETTY_NAME": "Ubuntu"}), \
             patch.object(environment.platform, "libc_ver", return_value=("glibc", "2.35")), \
             patch.object(environment, "systemd_user_available", return_value=False):
            info = environment.detect_environment()
        self.assertTrue(info.wsl and info.ssh and info.rdp and info.container)
        self.assertIn("glibc", info.libc)
        self.assertIn("24/7", " ".join(info.lines()))

    def test_root_requires_dedicated_user(self):
        with patch.object(environment.os, "geteuid", return_value=0):
            with self.assertRaisesRegex(environment.EnvironmentError, "dedicated unprivileged"):
                environment.refuse_root()


class InstallerTests(unittest.TestCase):
    def ui(self):
        ui = Mock()
        ui.busy.side_effect = lambda *_: contextlib.nullcontext()
        return ui

    def test_real_native_model_envelope_and_context(self):
        entries = installer.model_entries(CATALOG)
        self.assertEqual([m["providerID"] for m, _ in entries], ["provider-a", "provider-b"])
        self.assertIn("128000", entries[1][1])
        self.assertNotIn("disabled", str(entries))

    def test_no_native_default_requires_explicit_real_model(self):
        ui = self.ui()
        ui.choose.return_value = 0
        client = Mock()
        client.models.return_value = CATALOG
        client.default_model.return_value = {"location": {}, "data": None}
        selected = installer.select_model(ui, client, Path("/project"))
        self.assertEqual(selected, {"id": "model-a", "providerID": "provider-a"})
        self.assertNotIn("Native backend default", str(ui.choose.call_args))

    def test_native_default_keeps_backend_selection(self):
        ui = self.ui()
        ui.choose.return_value = 0
        client = Mock()
        client.models.return_value = CATALOG
        client.default_model.return_value = {"data": {"id": "model-z", "providerID": "provider-b"}}
        self.assertIsNone(installer.select_model(ui, client, Path("/project")))
        self.assertIn("provider-b/model-z", str(ui.choose.call_args))

    def test_existing_model_preserved_when_requested(self):
        ui = self.ui()
        ui.confirm.return_value = True
        client = Mock(models=Mock(return_value=CATALOG), default_model=Mock(return_value=None))
        existing = SimpleNamespace(model={"id": "custom", "providerID": "private", "variant": "high"})
        self.assertEqual(installer.select_model(ui, client, Path("/project"), existing), existing.model)
        ui.choose.assert_not_called()

    def test_full_needs_exact_typed_consent(self):
        ui = self.ui()
        ui.choose.return_value = 1
        ui.prompt.return_value = "full"
        with self.assertRaisesRegex(installer.SetupError, "FULL consent"):
            installer.permission_choice(ui)
        ui.prompt.return_value = "FULL"
        self.assertEqual(installer.permission_choice(ui), "full")
        self.assertIn("NOT an OS sandbox", str(ui.say.call_args_list))

    def test_safe_permission_default(self):
        ui = self.ui()
        ui.choose.return_value = 0
        self.assertEqual(installer.permission_choice(ui), "ask")
        self.assertEqual(ui.choose.call_args.kwargs["default"], 0)

    def test_project_creation_requires_consent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "project"
            ui = self.ui()
            ui.prompt.return_value = str(root)
            ui.confirm.return_value = False
            with self.assertRaises(installer.SetupError):
                installer.select_project(ui)
            self.assertFalse(root.exists())
            ui.confirm.return_value = True
            ui.choose.return_value = 0
            self.assertEqual(installer.select_project(ui), ((root,), root))
            self.assertTrue(root.is_dir())

    def test_broad_home_root_rejected(self):
        ui = self.ui()
        ui.prompt.return_value = str(Path.home())
        with self.assertRaises(installer.SetupError):
            installer.select_project(ui)

    def test_token_hidden_and_webhook_never_deleted(self):
        ui = self.ui()
        ui.secret.return_value = "123456:hidden"
        transport = Mock()
        transport.get_me.return_value = {"is_bot": True, "username": "test"}
        transport.webhook_info.return_value = {"url": "https://existing.example"}
        with patch("telegram_bridge.telegram.TelegramClient", return_value=transport):
            with self.assertRaisesRegex(installer.SetupError, "NOT deleted"):
                installer.telegram_identity(ui)
        self.assertEqual([c[0] for c in transport.method_calls], ["get_me", "webhook_info"])
        self.assertNotIn("123456:hidden", str(ui.say.call_args_list))

    def test_numeric_owner_id_validation(self):
        ui = self.ui()
        ui.secret.return_value = "123456:hidden"
        ui.prompt.side_effect = ["@name", "-1", "0", "42"]
        transport = Mock()
        transport.get_me.return_value = {"is_bot": True, "username": "test"}
        transport.webhook_info.return_value = {"url": ""}
        with patch("telegram_bridge.telegram.TelegramClient", return_value=transport):
            self.assertEqual(installer.telegram_identity(ui), ("123456:hidden", 42))

    def test_headless_login_uses_omnirush_account_wrapper_not_sidecar_auth(self):
        ui = self.ui()
        ui.confirm.return_value = True
        with patch.object(installer, "require_terminal"), \
             patch("telegram_bridge.account.login", return_value={"gateway_url": "https://omnirush.ai/omnirush/v1", "access_token": "access", "refresh_token": "refresh"}) as login:
            self.assertTrue(installer.login("/path with spaces/sidecar", "headless", ui))
        login.assert_called_once_with(ui)

    def test_desktop_login_only_directs_to_gui_account(self):
        ui = self.ui()
        with patch.object(installer, "require_terminal"), patch("telegram_bridge.account.login") as login:
            self.assertFalse(installer.login("/path with spaces/sidecar", "desktop", ui))
        login.assert_not_called()
        self.assertIn("Settings > Account", str(ui.say.call_args_list))

    def test_headless_empty_models_explains_gateway_entitlement(self):
        client = Mock(backend_mode="headless")
        client.models.return_value = []
        with self.assertRaisesRegex(installer.SetupError, "account model entitlement"):
            installer.select_model(self.ui(), client, Path("/project"), SimpleNamespace(model={"id": "old", "providerID": "old"}))
        client.default_model.assert_not_called()

    def test_running_existing_config_refused_before_setup_side_effects(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / "config.json"
            private.touch()
            existing = config(Path(folder))
            with patch.object(installer, "CONFIG_PATH", private), \
                 patch.object(installer, "refuse_root"), patch.object(installer, "require_terminal"), \
                 patch.object(installer, "detect_environment", return_value=SimpleNamespace(lines=lambda: [])), \
                 patch.object(installer, "load_config", return_value=existing), \
                 patch.object(installer.services, "is_running", return_value=True), \
                 patch.object(installer, "save_private") as save, \
                 patch.object(installer, "select_executable") as executable:
                with self.assertRaisesRegex(installer.SetupError, "running"):
                    installer.setup(self.ui())
                save.assert_not_called()
                executable.assert_not_called()

    def test_existing_config_decline_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder:
            private = Path(folder) / "config.json"
            private.touch()
            ui = self.ui()
            ui.confirm.return_value = False
            with patch.object(installer, "CONFIG_PATH", private), \
                 patch.object(installer, "refuse_root"), patch.object(installer, "require_terminal"), \
                 patch.object(installer, "detect_environment", return_value=SimpleNamespace(lines=lambda: [])), \
                 patch.object(installer, "load_config", return_value=config(Path(folder))), \
                 patch.object(installer.services, "is_running", return_value=False), \
                 patch.object(installer, "save_private") as save:
                self.assertEqual(installer.setup(ui), 0)
                save.assert_not_called()

    def test_complete_desktop_setup_no_install_or_start_without_consent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            ui = self.ui()
            ui.confirm.side_effect = lambda message, **_: message == "Save this private configuration?"
            ui.choose.side_effect = lambda message, choices, **_: 1 if message == "Backend" else 0
            client = Mock()
            client.models.return_value = CATALOG
            client.default_model.return_value = {"id": "model-z", "providerID": "provider-b"}
            with patch.object(installer, "CONFIG_PATH", root / "config.json"), \
                 patch.object(installer, "refuse_root"), patch.object(installer, "require_terminal"), \
                 patch.object(installer, "detect_environment", return_value=SimpleNamespace(lines=lambda: [], systemd_user=False)), \
                 patch.object(installer, "select_executable", return_value="/installed/sidecar"), \
                 patch.object(installer, "select_project", return_value=((root,), root)), \
                 patch.object(installer, "telegram_identity", return_value=("hidden", 42)), \
                 patch.object(installer.services, "_client", return_value=client), \
                 patch.object(installer, "save_private") as save, \
                 patch.object(installer, "load_config", return_value=config(root)), \
                 patch.object(installer.services, "install") as install, \
                 patch.object(installer.services, "start") as start:
                self.assertEqual(installer.setup(ui), 0)
                saved = save.call_args.args[0]
                self.assertEqual(saved["permission_mode"], "ask")
                self.assertEqual(saved["backend_mode"], "desktop")
                self.assertIsNone(saved["model"])
                install.assert_not_called()
                start.assert_not_called()


class CLITests(unittest.TestCase):
    def test_login_is_available_before_setup_has_saved_config(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch.object(environment, "refuse_root"), \
             patch("telegram_bridge.cli_ui.require_terminal"), \
             patch("telegram_bridge.config.CONFIG_PATH", Path(folder) / "missing.json"), \
             patch("telegram_bridge.config.load_config") as load, \
             patch.object(installer, "login", return_value=True) as login:
            self.assertEqual(omnirush.main(["login"]), 0)
        load.assert_not_called()
        self.assertEqual(login.call_args.args[:2], (None, "headless"))

    def test_help_includes_lifecycle(self):
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream), self.assertRaises(SystemExit) as raised:
            omnirush.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        for command in ("setup", "doctor", "service", "login", "start", "stop", "run", "restart"):
            self.assertIn(command, stream.getvalue())

    def test_unknown_token_argument_not_echoed(self):
        stream = io.StringIO()
        with contextlib.redirect_stderr(stream), self.assertRaises(SystemExit):
            omnirush.main(["setup", "123456:SECRET_TOKEN"])
        self.assertNotIn("SECRET_TOKEN", stream.getvalue())

    def test_root_error_clear(self):
        stream = io.StringIO()
        with patch.object(environment.os, "geteuid", return_value=0), contextlib.redirect_stdout(stream):
            self.assertEqual(omnirush.main(["status"]), 1)
        self.assertIn("dedicated unprivileged", stream.getvalue())


if __name__ == "__main__":
    unittest.main()
