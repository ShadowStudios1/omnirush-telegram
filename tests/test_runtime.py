import base64
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

from telegram_bridge.agent import AgentClient, AgentError, AgentUncertainError, discover_executable
from telegram_bridge.config import CONFIG_PATH, STATE_PATH, Config, ConfigError, load_config, save_private
from telegram_bridge import runtime


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.executable = self.root / "opencode-x86_64-unknown-linux-gnu"
        self.executable.write_bytes(b"test fixture")
        self.executable.chmod(0o700)

    def tearDown(self):
        self.temporary.cleanup()

    def config(self, **changes):
        options = dict(token="fake-unit-token", owner_id=123, project=self.project,
                       roots=(self.project,), state_path=self.root / "private/state.sqlite3")
        options.update(changes)
        return Config(**options)

    def environment(self):
        return patch.multiple(runtime, APP_DATA=self.root / "data", NATIVE_HOME=self.root / "data/native")

    def test_private_defaults_and_backward_compatibility(self):
        self.assertIn("omnirush-telegram-portable", str(CONFIG_PATH))
        self.assertIn("omnirush-telegram-portable", str(STATE_PATH))
        config = self.config()
        self.assertEqual((config.backend_mode, config.permission_mode), ("desktop", "ask"))
        self.assertNotIn("fake-unit-token", repr(config))

    def test_config_round_trip_and_invalid_fields(self):
        path = self.root / "private/config.json"
        data = {"telegram_token": "fake-unit-token", "owner_id": 123,
                "default_project": str(self.project), "project_roots": [str(self.project)],
                "state_path": str(self.root / "private/state.sqlite3"),
                "backend_mode": "headless", "permission_mode": "full",
                "server_url": "managed", "executable": str(self.executable)}
        save_private(data, path)
        config = load_config(path)
        self.assertEqual((config.backend_mode, config.permission_mode), ("headless", "full"))
        for field, bad in (("backend_mode", "cloud"), ("permission_mode", "allow"),
                           ("executable", "opencode"), ("executable", "C:\\opencode.exe"),
                           ("executable", "/mnt/c/opencode.exe"),
                           ("executable", str(self.project)),
                           ("model", {"id": "x"}), ("monitor_interval", True),
                           ("monitor_interval", "5"), ("monitor_interval", 1),
                           ("server_url", "http://example.com")):
            with self.subTest(field=field, value=bad):
                invalid = {**data, field: bad}
                save_private(invalid, path)
                with self.assertRaises(ConfigError):
                    load_config(path)
        with self.assertRaises(ConfigError):
            self.config(monitor_interval=float("nan"))

    def test_private_paths_and_project_symlinks_are_rejected(self):
        with self.assertRaises(ConfigError):
            self.config(state_path=self.project / "state.sqlite3")
        link = self.root / "project-link"
        link.symlink_to(self.project, target_is_directory=True)
        with self.assertRaises(ConfigError):
            self.config(project=link)
        self.executable.with_name("link").symlink_to(self.executable)
        with self.assertRaises(ConfigError):
            self.config(executable=str(self.executable.with_name("link")))
        with patch("telegram_bridge.config.PRIVATE_DATA", self.project), self.assertRaises(ConfigError):
            self.config()

    def test_environment_is_isolated_and_does_not_copy_desktop_overrides(self):
        with self.environment(), patch.dict(os.environ, {"OPENCODE_CONFIG": "/desktop/secrets",
                "OPENCODE_SERVER_URL": "http://remote", "OMNIRUSH_HOME": "/desktop",
                "XDG_CONFIG_HOME": "/desktop", "PATH": "/usr/bin"}):
            environment = runtime.runtime_environment()
        self.assertEqual(environment["PATH"], "/usr/bin")
        self.assertNotIn("OPENCODE_CONFIG", environment)
        self.assertNotIn("OPENCODE_SERVER_URL", environment)
        self.assertNotIn("OMNIRUSH_HOME", environment)
        for name in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            location = Path(environment[name])
            self.assertTrue(location.is_relative_to(self.root / "data/native"))
            self.assertEqual(location.stat().st_mode & 0o777, 0o700)

    def test_environment_refuses_symlink(self):
        data = self.root / "data"
        data.symlink_to(self.project, target_is_directory=True)
        with self.environment(), self.assertRaises(AgentError):
            runtime.runtime_environment()

    def test_only_authenticated_runtime_environment_loads_private_account(self):
        credentials = {"gateway_url": "https://omnirush.ai/omnirush/v1",
                       "access_token": "native-access", "refresh_token": "native-refresh"}
        with self.environment(), patch("telegram_bridge.account.load_credentials", return_value=credentials) as load, \
             patch.dict(os.environ, {"OMNIRUSH_ACCESS_TOKEN": "desktop-access", "ENGINE_GATEWAY_URL": "https://wrong", "XDG_CONFIG_HOME": "/wrong"}):
            plain = runtime.runtime_environment()
            load.assert_not_called()
            authenticated = runtime.authenticated_runtime_environment()
        self.assertNotIn("OMNIRUSH_ACCESS_TOKEN", plain)
        self.assertEqual(authenticated["OMNIRUSH_ACCESS_TOKEN"], "native-access")
        self.assertEqual(authenticated["OMNIRUSH_GATEWAY_URL"], credentials["gateway_url"])
        self.assertNotIn("ENGINE_GATEWAY_URL", authenticated)
        self.assertNotIn("native-refresh", authenticated.values())
        self.assertEqual(authenticated["XDG_CONFIG_HOME"], str(self.root / "data/native/config"))

    def test_missing_account_errors_before_headless_api_launch(self):
        agent = AgentClient(str(self.executable), "managed", backend_mode="headless")
        with self.environment(), patch("telegram_bridge.account.load_credentials", return_value=None), \
             patch("telegram_bridge.agent.subprocess.run") as run:
            with self.assertRaisesRegex(AgentError, "OmniRush account is not signed in"):
                agent.models()
        run.assert_not_called()

    def test_managed_api_omits_server_and_passes_isolated_environment(self):
        agent = AgentClient(str(self.executable), "managed", permission_mode="full", backend_mode="headless")
        result = subprocess.CompletedProcess([], 0, '{"data":{"id":"ses_test"}}', "")
        with patch("telegram_bridge.agent.AgentClient._authenticated_environment", return_value={"XDG_CONFIG_HOME": "isolated"}), \
                patch("telegram_bridge.agent.subprocess.run", return_value=result) as run:
            session = agent.create_session(str(self.project))
        command = run.call_args.args[0]
        self.assertEqual(command[:4], [str(self.executable), "api", "POST", "/api/session"])
        self.assertNotIn("--server", command)
        body = json.loads(command[-1])
        self.assertEqual(body["permissions"], [{"action": "*", "resource": "*", "effect": "allow"}])
        self.assertEqual(session["id"], "ses_test")
        self.assertEqual(run.call_args.kwargs["env"], {"XDG_CONFIG_HOME": "isolated"})

    def test_explicit_headless_endpoint_authenticates_discovery_without_secret_argv(self):
        authorization = "Basic " + base64.b64encode(b"opencode:pass").decode("ascii")
        agent = AgentClient(str(self.executable), "http://127.0.0.1:43123",
                            backend_mode="headless", server_auth=authorization)
        result = subprocess.CompletedProcess([], 0, '{"healthy":true}', "")
        environment = {"PATH": "/usr/bin", "OMNIRUSH_ACCESS_TOKEN": "private-access"}
        response = Mock()
        response.getcode.return_value = 200
        response.read.return_value = result.stdout.encode("utf-8")
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch("telegram_bridge.agent.AgentClient._authenticated_environment", return_value=environment), \
             patch("telegram_bridge.agent.urllib.request.build_opener", return_value=opener) as build:
            agent.health()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:43123/api/health")
        self.assertEqual(request.get_header("Authorization"), authorization)
        self.assertNotIn("pass", request.full_url)
        build.assert_called_once()

    def test_explicit_headless_auth_uses_sanitized_private_account_environment(self):
        authorization = "Basic " + base64.b64encode(b"opencode:pass").decode("ascii")
        agent = AgentClient(str(self.executable), "http://127.0.0.1:43123",
                            backend_mode="headless", server_auth=authorization)
        credentials = {"gateway_url": "https://omnirush.ai/omnirush/v1",
                       "access_token": "private-access", "refresh_token": "private-refresh"}
        response = Mock()
        response.getcode.return_value = 200
        response.read.return_value = b'{"data":[]}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(agent, "_authenticated_environment", return_value={
                "OMNIRUSH_ACCESS_TOKEN": "private-access", "XDG_CONFIG_HOME": str(self.root / "data/native/config")}), \
             patch("telegram_bridge.agent.urllib.request.build_opener", return_value=opener):
            self.assertEqual(agent.models(), [])
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), authorization)
        self.assertNotIn("private-access", request.full_url)

    def test_private_basic_auth_rejects_malformed_and_unsupported_credentials(self):
        invalid = ["", "Bearer pass", "Basic !!!!", "Basic abc", "Basic A===", 123]
        for raw in (b"user:pass", b"opencode:", b"opencode", b":pass", b"opencode:pass\x00",
                    b"opencode:pass\n", b"opencode:pass\x7f", b"opencode:pass word", b"opencode:\xff"):
            invalid.append("Basic " + base64.b64encode(raw).decode("ascii"))
        for authorization in invalid:
            with self.subTest(authorization=authorization), \
                 patch("telegram_bridge.agent.subprocess.run") as run, \
                 self.assertRaisesRegex(AgentError, "Invalid private backend authorization"):
                AgentClient(str(self.executable), "http://127.0.0.1:43123",
                            backend_mode="headless", server_auth=authorization)
            run.assert_not_called()

    def test_explicit_headless_urls_require_loopback_and_port(self):
        for url in ("http://example.com:43123", "http://0.0.0.0:43123", "http://[::]:43123",
                    "http://127.0.0.1", "http://localhost", "http://[::1]", "http://127.0.0.1:",
                    "http://127.0.0.1:0", "auto"):
            with self.subTest(url=url), self.assertRaises(AgentError):
                AgentClient(str(self.executable), url, backend_mode="headless")
        for url in ("http://127.0.0.1:43123", "http://localhost:43123", "http://[::1]:43123"):
            with self.subTest(url=url):
                AgentClient(str(self.executable), url, backend_mode="headless")

    def test_desktop_api_keeps_cli_owned_authentication(self):
        agent = AgentClient(str(self.executable), "http://localhost")
        result = subprocess.CompletedProcess([], 0, '{"data":[]}', "")
        with patch("telegram_bridge.agent.AgentClient._authenticated_environment") as environment, \
             patch("telegram_bridge.agent.subprocess.run", return_value=result) as run:
            self.assertEqual(agent.models(), [])
        environment.assert_not_called()
        self.assertNotIn("env", run.call_args.kwargs)
        self.assertNotIn("--header", run.call_args.args[0])

    def test_serve_executes_loopback_service_only(self):
        config = self.config(executable=str(self.executable), backend_mode="headless", server_url="managed")
        with patch("telegram_bridge.config.load_config", return_value=config), \
                patch.object(runtime, "authenticated_runtime_environment", return_value={"PATH": "/usr/bin"}), \
                patch.object(runtime.os, "umask"), patch.object(runtime.os, "execve") as execute:
            runtime.serve()
        self.assertEqual(execute.call_args.args, (str(self.executable),
            [str(self.executable), "serve", "--service", "--hostname", "127.0.0.1"], {"PATH": "/usr/bin"}))
        with patch("telegram_bridge.config.load_config", return_value=self.config()), \
                patch.object(runtime.os, "execve") as execute, self.assertRaises(ConfigError):
            runtime.serve()
        execute.assert_not_called()

    def test_discovery_uses_native_candidate_and_never_path_or_windows_npm(self):
        with patch("telegram_bridge.releases.installed_runtime", return_value=self.executable):
            self.assertEqual(discover_executable(), str(self.executable))
        with patch("telegram_bridge.releases.installed_runtime", return_value=None), \
                patch("telegram_bridge.agent.DEFAULT_EXECUTABLE", str(self.root / "missing")):
            self.assertIsNone(discover_executable())

    def test_desktop_discovery_matches_arm_process_and_pid(self):
        agent = AgentClient(str(self.executable))
        output = ('LISTEN 0 4096 127.0.0.1:4096 0.0.0.0:* users:(("opencode-aarch6",pid=99,fd=1))\n'
                  'LISTEN 0 4096 0.0.0.0:4000 0.0.0.0:* users:(("opencode",pid=88,fd=1))\n')
        info = {"version": "3.1.1", "pid": 99, "urls": [], "paths": {"tmp": "/private"}}
        with patch("telegram_bridge.agent.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")), \
                patch.object(agent, "_invoke", return_value=info) as invoke:
            self.assertEqual(agent.discover(), "http://127.0.0.1:4096")
        invoke.assert_called_once_with("http://127.0.0.1:4096", "GET", "/api/info", None)
        info["pid"] = 100
        with patch("telegram_bridge.agent.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")), \
                patch.object(agent, "_invoke", return_value=info), self.assertRaises(AgentError):
            agent.discover()

    def test_permissions_patch_is_session_only_and_validates_204(self):
        agent = AgentClient(str(self.executable), "http://127.0.0.1:4096")
        with patch.object(agent, "call", return_value=None) as call:
            agent.set_permissions("ses_test", "full")
        call.assert_called_once_with("PATCH", "/api/session/ses_test", {
            "permissions": [{"action": "*", "resource": "*", "effect": "allow"}]})
        with patch.object(agent, "call", return_value={"data": {}}), self.assertRaises(AgentUncertainError):
            agent.set_permissions("ses_test", "ask")
        with patch.object(agent, "call") as call, self.assertRaises(AgentError):
            agent.set_permissions("ses_test", "allow")
        call.assert_not_called()

    def test_model_helpers_native_query_and_switch(self):
        agent = AgentClient(str(self.executable), "http://127.0.0.1:4096")
        model = {"id": "model/with/slash", "providerID": "provider", "name": "Public name"}
        with patch.object(agent, "call", return_value={"data": [model]}) as call:
            self.assertEqual(agent.models("/project space"), [model])
        call.assert_called_once_with("GET", "/api/model?location%5Bdirectory%5D=%2Fproject+space")
        with patch.object(agent, "call", return_value={"data": None}):
            self.assertIsNone(agent.default_model())
        with patch.object(agent, "call", return_value={"data": model}):
            self.assertEqual(agent.default_model(), model)
        with patch.object(agent, "call", return_value=None) as call:
            agent.set_model("ses_test", "provider/model/with/slash")
        call.assert_called_once_with("POST", "/api/session/ses_test/model", {
            "model": {"providerID": "provider", "id": "model/with/slash"}})
        with patch.object(agent, "session", return_value={"tokens": {"input": 12}, "cost": 0.3, "model": model}):
            self.assertEqual(agent.usage("ses_test"), {"tokens": {"input": 12}, "cost": 0.3})

    def test_uncertain_headless_mutation_is_never_repeated(self):
        agent = AgentClient(str(self.executable), "managed", backend_mode="headless")
        with patch("telegram_bridge.agent.AgentClient._authenticated_environment", return_value={}), \
                patch("telegram_bridge.agent.subprocess.run", side_effect=subprocess.TimeoutExpired("fixture", 30)) as run, \
                self.assertRaises(AgentUncertainError):
            agent.set_permissions("ses_test", "ask")
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
