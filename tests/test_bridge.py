import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error

from telegram_bridge.agent import AgentClient, AgentError, AgentUncertainError
from telegram_bridge.bridge import Bridge
from telegram_bridge.config import Config, ConfigError, load_config, save_private
from telegram_bridge.state import StateError, StateStore
from telegram_bridge.telegram import TelegramClient, TelegramError, authorized_message, redact, split_text


OWNER = 12345678
FAKE_TOKEN = "999999:FAKE_TOKEN_FOR_UNIT_TESTS_ONLY"


def update(number=1, text="make a file", date=None):
    return {"update_id": number, "message": {
        "from": {"id": OWNER, "is_bot": False},
        "chat": {"id": OWNER, "type": "private"},
        "date": int(time.time()) if date is None else date, "text": text,
    }}


class PrivateTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="telegram-tests-", dir="/tmp/opencode")
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.state = StateStore(self.root / "private/state.sqlite3")
        self.telegram = Mock(spec=TelegramClient)
        self.telegram.MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
        self.agent = Mock(spec=AgentClient)
        self.agent.create_session.return_value = {"id": "ses_test"}
        self.agent.permissions.return_value = []
        self.agent.forms.return_value = []
        self.agent.messages.return_value = []
        self.agent.active.return_value = {}
        self.config = Config(FAKE_TOKEN, OWNER, self.project, (self.project,),
                             state_path=self.root / "private/state.sqlite3")
        self.bridge = Bridge(self.config, self.state, self.telegram, self.agent)

    def tearDown(self):
        self.state.close()
        self.temporary.cleanup()


class CommandTests(PrivateTest):
    def test_owner_dispatch_once_and_preserve_offset(self):
        request = update(10)
        self.bridge.handle_update(request)
        self.bridge.handle_update(request)
        self.agent.prompt.assert_called_once()
        args = self.agent.prompt.call_args.args
        self.assertIn("make a file", args[1])
        self.assertEqual(args[2], f"msg_telegram_{OWNER}_10")
        self.assertEqual(self.state.get("offset"), 11)

    def test_unauthorized_and_forwarded_requests_have_no_action(self):
        bad = update(1)
        bad["message"]["from"]["id"] += 1
        forwarded = update(2)
        forwarded["message"]["forward_origin"] = {"type": "user"}
        for request in (bad, forwarded):
            self.bridge.handle_update(request)
        self.agent.prompt.assert_not_called()
        self.telegram.send_text.assert_not_called()

    def test_stale_offline_instruction_never_executes(self):
        self.bridge.handle_update(update(date=self.bridge.accept_after - 1))
        self.agent.prompt.assert_not_called()
        self.assertIn("Ignored", self.telegram.send_text.call_args.args[1])

    def test_queued_start_replies_after_restart_without_agent_work(self):
        self.telegram.send_text.return_value = [88]
        request = update(text="/start", date=self.bridge.accept_after - 30)
        self.bridge.handle_update(request)
        self.bridge.handle_update(request)
        self.telegram.send_text.assert_called_once()
        self.assertIn("OmniRush remote workspace", self.telegram.send_text.call_args.args[1])
        self.agent.create_session.assert_not_called()
        self.agent.prompt.assert_not_called()
        receipt = self.state.get("last_telegram_delivery")
        self.assertEqual(receipt["message_ids"], [88])
        self.assertEqual(receipt["chat_id"], OWNER)
        self.assertNotIn("text", receipt)

    def test_plain_start_help_and_deep_link_start_are_safe_offline(self):
        for number, text in enumerate(("Start", " START ", "/help", "/start@shadow_ari_bot connect", "/start\nconnect"), 20):
            with self.subTest(text=text):
                self.bridge.handle_update(update(number, text, self.bridge.accept_after - 30))
                self.assertIn("OmniRush remote workspace", self.telegram.send_text.call_args.args[1])
        self.agent.prompt.assert_not_called()

    def test_stale_mutating_commands_are_not_enabled_by_start_exception(self):
        for number, text in enumerate(("/approve per_test", "/new", "/stop", "/project /tmp", "start deleting files"), 20):
            self.bridge.handle_update(update(number, text, self.bridge.accept_after - 30))
            self.assertIn("Ignored", self.telegram.send_text.call_args.args[1])
        self.agent.create_session.assert_not_called()
        self.agent.reply_permission.assert_not_called()
        self.agent.interrupt.assert_not_called()
        self.agent.prompt.assert_not_called()

    def test_start_with_invalid_date_or_unauthorized_sender_is_not_dispatched(self):
        request = update(text="/start")
        del request["message"]["date"]
        self.bridge.handle_update(request)
        self.assertIn("Ignored", self.telegram.send_text.call_args.args[1])
        self.telegram.send_text.reset_mock()
        request = update(2, "/start", self.bridge.accept_after - 30)
        request["message"]["from"]["id"] += 1
        self.bridge.handle_update(request)
        self.telegram.send_text.assert_not_called()

    def test_uncertain_start_reply_is_not_repeated_and_no_receipt_claimed(self):
        self.telegram.send_text.side_effect = TelegramError("uncertain", uncertain=True)
        request = update(text="/start", date=self.bridge.accept_after - 30)
        with self.assertRaises(TelegramError):
            self.bridge.handle_update(request)
        self.bridge.handle_update(request)
        self.telegram.send_text.assert_called_once()
        self.assertIsNone(self.state.get("last_telegram_delivery"))

    def test_uncertain_prompt_is_not_retried(self):
        self.agent.prompt.side_effect = AgentUncertainError("safe")
        self.bridge.handle_update(update(1))
        self.bridge.handle_update(update(1))
        self.agent.prompt.assert_called_once()
        self.assertIn("NOT be repeated", self.telegram.send_text.call_args.args[1])

    def test_credential_text_never_reaches_agent(self):
        self.bridge.handle_update(update(text="use this token " + FAKE_TOKEN))
        self.agent.prompt.assert_not_called()

    def test_project_scope_and_persistence(self):
        nested = self.project / "another"
        nested.mkdir()
        self.bridge.command("/project", str(nested))
        self.assertEqual(self.state.get("project"), str(nested))
        with self.assertRaises(ConfigError):
            self.bridge.command("/project", str(self.root))

    def test_approval_is_current_session_and_one_time(self):
        self.bridge.ensure_session()
        self.agent.permissions.return_value = [{"id": "per_test", "action": "shell", "resources": ["echo ok"]}]
        self.bridge.command("/approve", "per_other")
        self.agent.reply_permission.assert_not_called()
        self.bridge.command("/approve", "per_test")
        self.agent.reply_permission.assert_called_once_with("ses_test", "per_test", "once")

    def test_answer_supported_and_sensitive_fields_rejected(self):
        self.bridge.ensure_session()
        self.agent.forms.return_value = [{"id": "frm_test", "fields": [{"key": "choice", "type": "string"}]}]
        self.bridge.command("/answer", 'frm_test {"choice":"yes"}')
        self.agent.reply_form.assert_called_once_with("ses_test", "frm_test", {"choice": "yes"})
        self.agent.forms.return_value = [{"id": "frm_test", "fields": [{"key": "password", "type": "string"}]}]
        with self.assertRaises(ValueError):
            self.bridge.command("/answer", 'frm_test {"password":"private"}')

    def test_restart_preserves_session_and_claims(self):
        self.bridge.handle_update(update(1))
        replacement = Bridge(self.config, self.state, self.telegram, self.agent)
        self.assertEqual(replacement.session_id(), "ses_test")
        replacement.handle_update(update(1))
        self.agent.prompt.assert_called_once()

    def test_no_reply_replay_after_uncertain_delivery(self):
        self.bridge.ensure_session()
        self.agent.messages.return_value = [{"id": "msg_reply", "type": "assistant", "time": {"completed": 1},
                                             "content": [{"type": "text", "text": "done"}]}]
        self.telegram.send_text.side_effect = TelegramError("safe", uncertain=True)
        with self.assertRaises(TelegramError):
            self.bridge.monitor_once()
        self.telegram.send_text.reset_mock()
        self.telegram.send_text.side_effect = None
        self.bridge.monitor_once()
        self.telegram.send_text.assert_not_called()

    def test_only_completed_visible_text_is_forwarded_redacted(self):
        self.bridge.ensure_session()
        self.agent.messages.return_value = [
            {"id": "msg_incomplete", "type": "assistant", "time": {}, "content": [{"type": "text", "text": "draft"}]},
            {"id": "msg_complete", "type": "assistant", "time": {"completed": 1},
             "content": [{"type": "reasoning", "text": "PRIVATE_REASONING"}, {"type": "text", "text": FAKE_TOKEN + " done"}]},
        ]
        self.bridge.monitor_once()
        reply = self.telegram.send_text.call_args.args[1]
        self.assertNotIn(FAKE_TOKEN, reply)
        self.assertNotIn("PRIVATE_REASONING", reply)
        self.bridge.monitor_once()
        self.telegram.send_text.assert_called_once()

    def test_download_checked_bytes_and_secret_rejection(self):
        public = self.project / "output.txt"
        public.write_text("public output")
        self.bridge.download("output.txt")
        self.telegram.send_document.assert_called_once_with(OWNER, public, content=b"public output")
        self.telegram.send_document.reset_mock()
        public.write_text(FAKE_TOKEN)
        with self.assertRaises(ConfigError):
            self.bridge.download("output.txt")
        self.telegram.send_document.assert_not_called()

    def test_download_traversal_symlink_and_private_files_refused(self):
        outside = self.root / "outside.txt"
        outside.write_text("outside")
        (self.project / "link.txt").symlink_to(outside)
        (self.project / ".env").write_text("private")
        for path in ("../outside.txt", "link.txt", ".env", str(outside)):
            with self.subTest(path=path), self.assertRaises(ConfigError):
                self.bridge.download(path)
        self.telegram.send_document.assert_not_called()

    def test_pending_requests_are_not_auto_approved(self):
        self.bridge.ensure_session()
        self.agent.permissions.return_value = [{"id": "per_pending", "action": "shell", "resources": ["echo okay"]}]
        self.bridge.monitor_once()
        self.bridge.monitor_once()
        self.agent.reply_permission.assert_not_called()
        self.telegram.send_text.assert_called_once()
        self.assertIn("/approve per_pending", self.telegram.send_text.call_args.args[1])

    def test_identity_changes_do_not_mix_bot_state(self):
        self.telegram.get_me.return_value = {"id": 111, "is_bot": True}
        self.telegram.webhook_info.return_value = {"url": ""}
        self.state.set("identity", {"bot_id": 222, "owner_id": OWNER})
        with self.assertRaises(ConfigError):
            self.bridge.run()
        self.telegram.updates.assert_not_called()


class StateAndConfigTests(unittest.TestCase):
    def test_lock_permissions_persistence_and_monotonic_offset(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as tmp:
            path = Path(tmp) / "state/state.sqlite3"
            with StateStore(path) as state:
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.parent.stat().st_mode & 0o777, 0o700)
                with self.assertRaises(StateError):
                    StateStore(path)
                state.set("offset", 40)
                state.set("offset", 10)
                self.assertEqual(state.get("offset"), 40)
                self.assertTrue(state.claim_update(99))
                self.assertFalse(state.claim_update(99))
                with self.assertRaises(StateError):
                    state.set("token", "never-store")
            with StateStore(path) as reopened:
                self.assertFalse(reopened.claim_update(99))
                self.assertEqual(reopened.get("offset"), 40)

    def test_config_private_file_and_scoped_root_validation(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as tmp:
            root = Path(tmp)
            project = root / "project"
            project.mkdir()
            config_path = root / "private/config.json"
            data = {"telegram_token": FAKE_TOKEN, "owner_id": OWNER,
                    "default_project": str(project), "project_roots": [str(project)],
                    "state_path": str(root / "state/state.sqlite3")}
            save_private(data, config_path)
            config = load_config(config_path)
            self.assertEqual(config.owner_id, OWNER)
            self.assertNotIn(FAKE_TOKEN, repr(config))
            config_path.chmod(0o644)
            with self.assertRaises(ConfigError):
                load_config(config_path)
            config_path.chmod(0o600)
            data["project_roots"] = [str(root)]
            save_private(data, config_path)
            with self.assertRaises(ConfigError):
                load_config(config_path)

    def test_symlink_configuration_refused(self):
        with tempfile.TemporaryDirectory(dir="/tmp/opencode") as tmp:
            root = Path(tmp)
            (root / "target").write_text("private")
            (root / "link").symlink_to(root / "target")
            with self.assertRaises(ConfigError):
                load_config(root / "link")


class TransportTests(unittest.TestCase):
    def test_unicode_splitting_keeps_text_and_limits(self):
        text = "hello 😀" * 2000
        chunks = split_text(text)
        self.assertEqual("".join(chunks), text)
        self.assertTrue(all(len(chunk.encode("utf-16-le")) // 2 <= 3500 for chunk in chunks))

    def test_redaction(self):
        text = FAKE_TOKEN + "\nAuthorization: Bearer private\nkeep this"
        safe = redact(text)
        self.assertNotIn(FAKE_TOKEN, safe)
        self.assertNotIn("private", safe)
        self.assertIn("keep this", safe)

    def test_groups_bots_edits_and_business_updates_refused(self):
        for change in ("group", "bot", "edited", "business", "forward"):
            request = update()
            if change == "group":
                request["message"]["chat"]["type"] = "group"
            elif change == "bot":
                request["message"]["from"]["is_bot"] = True
            elif change == "edited":
                request["edited_message"] = request.pop("message")
            elif change == "business":
                request["message"]["business_connection_id"] = "connection"
            else:
                request["message"]["forward_date"] = 1
            with self.subTest(change=change):
                self.assertIsNone(authorized_message(request, OWNER))

    def test_network_mutation_no_retry_or_token_diagnostics(self):
        transport = TelegramClient(FAKE_TOKEN)
        transport._opener = Mock()
        transport._opener.open.side_effect = urllib.error.URLError("raw URL with " + FAKE_TOKEN)
        with self.assertRaises(TelegramError) as context:
            transport.send_text(OWNER, "hello")
        self.assertTrue(context.exception.uncertain)
        self.assertNotIn(FAKE_TOKEN, str(context.exception))
        transport._opener.open.assert_called_once()

    def test_plain_text_post_and_no_forwarded_secret(self):
        transport = TelegramClient(FAKE_TOKEN)
        with patch.object(transport, "call", return_value={"message_id": 77, "chat": {"id": OWNER}}) as call:
            ids = transport.send_text(OWNER, "reply " + FAKE_TOKEN)
            self.assertEqual(ids, [77])
            method, payload = call.call_args.args
            self.assertEqual(method, "sendMessage")
            self.assertNotIn("parse_mode", payload)
            self.assertNotIn(FAKE_TOKEN, payload["text"])

    def test_invalid_delivery_receipt_is_uncertain_and_never_retried(self):
        for receipt in ({}, {"message_id": True, "chat": {"id": OWNER}},
                        {"message_id": 88, "chat": {"id": OWNER + 1}}, None):
            transport = TelegramClient(FAKE_TOKEN)
            with patch.object(transport, "call", return_value=receipt) as call:
                with self.assertRaises(TelegramError) as context:
                    transport.send_text(OWNER, "reply")
                self.assertTrue(context.exception.uncertain)
                call.assert_called_once()


class AgentTests(unittest.TestCase):
    def test_loopback_only_and_no_path_injection(self):
        with self.assertRaises(AgentError):
            AgentClient(server_url="http://example.com:4096")
        agent = AgentClient(server_url="http://127.0.0.1:4096")
        with self.assertRaises(AgentError):
            agent.session("ses_bad/../../credential")

    def test_cli_keeps_auth_and_prompt_schema(self):
        agent = AgentClient(server_url="http://127.0.0.1:4096")
        result = subprocess.CompletedProcess([], 0, '{"data":{"id":"msg_test"}}', "")
        with patch("telegram_bridge.agent.subprocess.run", return_value=result) as run:
            agent.prompt("ses_test", "make file", "msg_test")
        args = run.call_args.args[0]
        self.assertEqual(args[1:6], ["api", "--server", "http://127.0.0.1:4096", "POST", "/api/session/ses_test/prompt"])
        self.assertEqual(json.loads(args[-1]), {"id": "msg_test", "text": "make file", "delivery": "queue"})
        self.assertIs(run.call_args.kwargs["shell"], False)

    def test_agent_uncertain_write_never_retried(self):
        agent = AgentClient(server_url="http://127.0.0.1:4096")
        with patch("telegram_bridge.agent.subprocess.run", side_effect=subprocess.TimeoutExpired("safe", 30)) as run:
            with self.assertRaises(AgentUncertainError):
                agent.prompt("ses_test", "work", "msg_test")
        run.assert_called_once()

    def test_persistent_grants_refused(self):
        agent = AgentClient(server_url="http://127.0.0.1:4096")
        with self.assertRaises(AgentError):
            agent.reply_permission("ses_test", "per_test", "always")


if __name__ == "__main__":
    unittest.main()
