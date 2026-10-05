"""Product behavior tests: isolated metadata store, no credentials or live APIs."""

import copy
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

from telegram_bridge.agent import AgentError, AgentUncertainError
from telegram_bridge.bridge import Bridge, COMMANDS, FULL_CONFIRMATION
from telegram_bridge.config import Config, ConfigError
from telegram_bridge.state import StateStore
from telegram_bridge.telegram import TelegramClient, TelegramError, authorized_callback


OWNER = 12345678
TOKEN = "999999:FAKE_TOKEN_FOR_PRODUCT_TESTS_ONLY"


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir="/tmp/opencode", prefix="product-tests-")
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.other = self.project / "other"
        self.other.mkdir(parents=True)
        self.state = StateStore(self.root / "private/state.sqlite3")
        self.config = Config(TOKEN, OWNER, self.project, (self.project,),
                             state_path=self.root / "private/state.sqlite3")
        self.telegram = Mock(spec=TelegramClient)
        self.telegram.send_text.return_value = [55]
        self.agent = Mock()
        self.agent.active.return_value = {}
        self.agent.permissions.return_value = []
        self.agent.forms.return_value = []
        self.agent.messages.return_value = []
        self.agent.health.return_value = {"version": "3.1.1"}
        self.agent.session.return_value = {"id": "ses_one", "location": {"directory": str(self.project)}}
        self.agent.create_session.return_value = {"id": "ses_one"}
        self.agent.models.return_value = [{"providerID": "demo", "id": "tiny", "name": "Tiny",
                                           "enabled": True, "limit": {"context": 128000, "output": 8192}}]
        self.agent.default_model.return_value = {"providerID": "demo", "id": "tiny"}
        self.bridge = Bridge(self.config, self.state, self.telegram, self.agent)

    def tearDown(self):
        self.state.close()
        self.tmp.cleanup()

    def update(self, number=1, text="do useful work"):
        return {"update_id": number, "message": {"from": {"id": OWNER, "is_bot": False},
                "chat": {"id": OWNER, "type": "private"}, "date": int(time.time()), "text": text}}

    def callback(self, number=10, data=None):
        if data is None:
            data = next(iter(self.bridge.quick_actions))
        return {"update_id": number, "callback_query": {"id": "callback_1", "data": data,
                "from": {"id": OWNER, "is_bot": False}, "message": {"message_id": 55,
                "chat": {"id": OWNER, "type": "private"}, "date": self.bridge.accept_after}}}

    def reply(self, identifier="msg_done", text="visible answer"):
        return {"id": identifier, "type": "assistant", "time": {"completed": int(time.time() * 1000)},
                "content": [{"type": "text", "text": text}]}

    def test_dashboard_and_owner_scoped_slash_menu(self):
        self.bridge.command("/start", "")
        text = self.telegram.send_text.call_args.args[1]
        self.assertIn("🚀", text)
        self.assertIn("/threads", text)
        markup = self.telegram.send_text.call_args.kwargs["reply_markup"]
        self.assertTrue(markup["inline_keyboard"])
        self.assertTrue(all(len(button[0]["callback_data"].encode()) <= 64
                            for button in markup["inline_keyboard"]))
        self.assertIn("mode", [item["command"] for item in COMMANDS])

    def test_owner_quick_action_once_and_durable_offset(self):
        self.bridge.command("/start", "")
        request = self.callback()
        with patch.object(self.bridge, "command") as dispatch:
            self.bridge.handle_update(request)
            self.bridge.handle_update(request)
            replay = copy.deepcopy(request)
            replay["update_id"] += 1
            replay["callback_query"]["id"] = "callback_2"
            self.bridge.handle_update(replay)
            dispatch.assert_called_once_with("/status", "")
        self.assertEqual(self.state.get("offset"), 12)
        self.assertFalse(self.state.claim_callback("callback_1"))

    def test_callback_auth_private_date_binding_and_fixed_mapping(self):
        self.bridge.command("/start", "")
        for mutation in ("owner", "group", "bot", "forward", "business", "stale", "future", "message", "arbitrary", "inline", "bool"):
            request = self.callback()
            cb = request["callback_query"]
            if mutation == "owner": cb["from"]["id"] += 1
            elif mutation == "group": cb["message"]["chat"]["type"] = "group"
            elif mutation == "bot": cb["from"]["is_bot"] = True
            elif mutation == "forward": cb["message"]["forward_date"] = 1
            elif mutation == "business": cb["message"]["business_connection_id"] = "x"
            elif mutation == "stale": cb["message"]["date"] -= 1
            elif mutation == "future": cb["message"]["date"] += 600
            elif mutation == "message": cb["message"]["message_id"] += 1
            elif mutation == "arbitrary": cb["data"] = "/approve per_anything"
            elif mutation == "inline": cb["inline_message_id"] = "x"
            else: cb["from"]["id"] = True
            with self.subTest(mutation=mutation), patch.object(self.bridge, "command") as dispatch:
                self.bridge.handle_callback(request)
                dispatch.assert_not_called()
        self.assertTrue(self.bridge.quick_actions)

    def test_callbacks_expire_and_do_not_survive_restart(self):
        self.bridge.command("/start", "")
        request = self.callback()
        for value in self.bridge.quick_actions.values(): value["expires"] = time.time() - 1
        with patch.object(self.bridge, "command") as dispatch:
            self.bridge.handle_callback(request)
            dispatch.assert_not_called()
        replacement = Bridge(self.config, self.state, self.telegram, self.agent)
        with patch.object(replacement, "command") as dispatch:
            replacement.handle_update(request)
            dispatch.assert_not_called()

    def test_uncertain_callback_reply_does_not_repeat_dispatch(self):
        self.bridge.command("/start", "")
        request = self.callback()
        self.telegram.answer_callback.side_effect = TelegramError("uncertain", uncertain=True)
        with patch.object(self.bridge, "command") as dispatch:
            self.bridge.handle_update(request)
            self.bridge.handle_update(request)
            dispatch.assert_called_once()
        self.telegram.answer_callback.assert_called_once()

    def test_guessed_thread_never_reaches_api(self):
        self.bridge.command("/thread", "ses_someone_elses")
        self.agent.session.assert_not_called()
        self.assertIsNone(self.bridge.session_id())
        self.assertIn("arbitrary", self.telegram.send_text.call_args.args[1])

    def test_threads_persist_and_restore_exact_project_scope(self):
        self.bridge.ensure_session()
        self.bridge.command("/project", str(self.other))
        self.agent.create_session.return_value = {"id": "ses_two"}
        self.bridge.command("/new", "Private custom title")
        self.agent.session.return_value = {"id": "ses_one", "title": "First title",
                                           "location": {"directory": str(self.project)}}
        self.bridge.command("/thread", "ses_one")
        self.assertEqual(self.bridge.project, self.project)
        self.assertEqual(self.bridge.session_id(), "ses_one")
        replacement = Bridge(self.config, self.state, self.telegram, self.agent)
        self.assertEqual(set(replacement.threads), {"ses_one", "ses_two"})
        self.assertEqual(replacement.session_id(), "ses_one")
        self.assertNotIn("Private custom title", json.dumps(self.state.get("threads")))

    def test_native_thread_directory_change_is_rejected(self):
        self.bridge.ensure_session()
        self.agent.session.return_value = {"location": {"directory": str(self.other)}}
        with self.assertRaises(ConfigError): self.bridge.command("/thread", "ses_one")
        with self.assertRaises(ConfigError): self.bridge.command("/approve", "per_one")
        self.agent.reply_permission.assert_not_called()

    def test_new_project_thread_mode_model_guard_pending(self):
        self.bridge.ensure_session()
        self.agent.permissions.return_value = [{"id": "per_one"}]
        self.agent.create_session.reset_mock()
        self.bridge.command("/new", "title")
        self.bridge.command("/project", str(self.other))
        self.bridge.command("/mode", "full " + FULL_CONFIRMATION)
        self.bridge.command("/model", "demo/tiny")
        self.assertEqual(self.bridge.project, self.project)
        self.agent.create_session.assert_not_called()
        self.agent.set_permissions.assert_not_called()
        self.agent.set_model.assert_not_called()
        self.agent.reply_permission.assert_not_called()

    def test_full_mode_requires_exact_typed_consent_and_reverts_ask(self):
        self.bridge.ensure_session()
        for argument in ("full", "full yes", "full " + FULL_CONFIRMATION.lower()):
            self.bridge.command("/mode", argument)
        self.agent.set_permissions.assert_not_called()
        self.bridge.command("/mode", "full " + FULL_CONFIRMATION)
        self.agent.set_permissions.assert_called_once_with("ses_one", "full")
        self.assertEqual(self.state.get("future_mode"), "full")
        self.bridge.command("/mode", "ask")
        self.assertEqual(self.agent.set_permissions.call_args.args, ("ses_one", "ask"))
        self.assertEqual(self.state.get("threads")["ses_one"]["mode"], "ask")

    def test_consent_sets_future_creation_only_and_never_auto_approves(self):
        self.bridge.command("/mode", "full " + FULL_CONFIRMATION)
        self.bridge.ensure_session()
        self.assertEqual(self.agent.permission_mode, "full")
        self.assertEqual(self.bridge.threads["ses_one"]["mode"], "full")
        self.agent.reply_permission.assert_not_called()
        replacement = Bridge(self.config, self.state, self.telegram, self.agent)
        self.assertEqual(replacement.future_mode, "full")

    def test_uncertain_mode_switch_keeps_local_policy_and_is_not_retried(self):
        self.bridge.ensure_session()
        self.agent.set_permissions.side_effect = AgentUncertainError("safe")
        request = self.update(text="/mode full " + FULL_CONFIRMATION)
        self.bridge.handle_update(request)
        self.bridge.handle_update(request)
        self.agent.set_permissions.assert_called_once()
        self.assertEqual(self.bridge.future_mode, "ask")
        self.assertEqual(self.bridge.threads["ses_one"]["mode"], "ask")

    def test_model_list_switch_validated_reference_and_no_private_fields(self):
        self.agent.models.return_value[0]["headers"] = {"Authorization": "PRIVATE_HEADER"}
        self.agent.models.return_value[0]["body"] = {"key": "PRIVATE_BODY"}
        self.bridge.command("/model", "")
        reply = self.telegram.send_text.call_args.args[1]
        self.assertIn("demo/tiny", reply)
        self.assertNotIn("PRIVATE_", reply)
        self.bridge.command("/model", "unknown/no")
        self.agent.set_model.assert_not_called()
        self.bridge.command("/model", "demo/tiny")
        self.agent.set_model.assert_called_once_with("ses_one", {"providerID": "demo", "id": "tiny"})

    def test_usage_actual_cost_tokens_limits_not_context_occupancy(self):
        self.bridge.ensure_session()
        self.agent.session.return_value.update({"model": {"providerID": "demo", "id": "tiny"}, "cost": 0.125,
                    "tokens": {"input": 1200, "output": 60, "reasoning": 40, "cache": {"read": 300, "write": 10}}})
        for command in ("/usage", "/context"):
            self.bridge.command(command, "")
            reply = self.telegram.send_text.call_args.args[1]
            for expected in ("$0.125000 USD", "1,200", "128,000", "8,192", "NOT current context usage"):
                self.assertIn(expected, reply)
        self.bridge.command("/quota", "")
        self.assertIn("unavailable from native API", self.telegram.send_text.call_args.args[1])

    def test_progress_animation_typing_throttle_and_completion_elapsed(self):
        with patch("telegram_bridge.bridge.time.time", return_value=1000), patch("telegram_bridge.bridge.time.monotonic", return_value=0):
            self.bridge.dispatch("keep private prompt", 1)
            self.bridge.progress("ses_one")
            self.bridge.progress("ses_one")
        self.telegram.edit_text.assert_called_once()
        self.telegram.typing.assert_called_once()
        with patch("telegram_bridge.bridge.time.time", return_value=1011), patch("telegram_bridge.bridge.time.monotonic", return_value=11):
            self.bridge.progress("ses_one")
        self.assertEqual(self.telegram.edit_text.call_count, 2)
        self.assertIn("11s", self.telegram.edit_text.call_args.args[2])
        self.assertNotIn("keep private", self.telegram.edit_text.call_args.args[2])
        self.agent.messages.return_value = [{"id": f"msg_telegram_{OWNER}_1", "type": "user"}, self.reply()]
        with patch("telegram_bridge.bridge.time.time", return_value=1025), patch("telegram_bridge.bridge.time.monotonic", return_value=25):
            self.bridge.monitor_once()
        self.assertIn("25s elapsed", self.telegram.send_text.call_args.args[1])
        self.assertNotIn("ses_one", self.bridge.work)
        self.assertNotIn("ses_one", self.bridge.cards)

    def test_progress_edit_failure_disables_card_and_never_retries(self):
        self.bridge.dispatch("request", 1)
        self.telegram.edit_text.side_effect = TelegramError("uncertain", uncertain=True)
        with patch("telegram_bridge.bridge.time.monotonic", return_value=0): self.bridge.progress("ses_one")
        with patch("telegram_bridge.bridge.time.monotonic", return_value=60): self.bridge.progress("ses_one")
        self.telegram.edit_text.assert_called_once()
        self.assertTrue(self.bridge.cards["ses_one"]["disabled"])

    def test_completion_waits_for_edit_throttle_without_idle_message_polling(self):
        self.bridge.dispatch("work", 1)
        with patch("telegram_bridge.bridge.time.monotonic", return_value=0):
            self.bridge.progress("ses_one")
        self.agent.messages.return_value = [{"id": f"msg_telegram_{OWNER}_1", "type": "user"}, self.reply()]
        with patch("telegram_bridge.bridge.time.monotonic", return_value=5):
            self.bridge.monitor_once()
        self.assertIn("finished_at", self.bridge.cards["ses_one"])
        self.assertNotIn("ses_one", self.bridge.work)
        self.agent.messages.reset_mock()
        with patch("telegram_bridge.bridge.time.monotonic", return_value=10):
            self.bridge.monitor_once()
        self.agent.messages.assert_not_called()
        self.assertIn("✅ Finished", self.telegram.edit_text.call_args.args[2])
        self.assertNotIn("ses_one", self.bridge.cards)

    def test_new_queued_input_cannot_finish_on_an_older_reply(self):
        self.bridge.dispatch("first work", 1)
        self.bridge.work["ses_one"]["accepted"] = True
        self.bridge.dispatch("second work", 2)
        self.agent.messages.return_value = [{"id": f"msg_telegram_{OWNER}_1", "type": "user"}, self.reply()]
        self.bridge.monitor_once()
        self.assertIn("ses_one", self.bridge.work)
        self.assertFalse(self.bridge.work["ses_one"]["accepted"])
        self.agent.messages.return_value += [{"id": f"msg_telegram_{OWNER}_2", "type": "user"}, self.reply("msg_second")]
        self.bridge.monitor_once()
        self.assertNotIn("ses_one", self.bridge.work)

    def test_typing_and_edit_rates_are_bounded_across_cards(self):
        self.bridge.dispatch("work", 1)
        self.bridge.cards["ses_two"] = dict(self.bridge.cards["ses_one"])
        with patch("telegram_bridge.bridge.time.monotonic", return_value=0):
            self.bridge.progress("ses_one")
            self.bridge.progress("ses_two")
        self.telegram.edit_text.assert_called_once()
        self.telegram.typing.assert_called_once()
        with patch("telegram_bridge.bridge.time.monotonic", return_value=9):
            self.bridge.progress("ses_two")
        self.telegram.edit_text.assert_called_once()
        self.assertEqual(self.telegram.typing.call_count, 2)

    def test_uncertain_queue_card_is_never_resent(self):
        self.telegram.send_text.side_effect = TelegramError("uncertain", uncertain=True)
        request = self.update()
        with self.assertRaises(TelegramError): self.bridge.handle_update(request)
        self.bridge.handle_update(request)
        self.agent.active.return_value = {"ses_one": {}}
        self.bridge.monitor_once()
        self.telegram.send_text.assert_called_once()
        self.agent.prompt.assert_called_once()
        self.telegram.edit_text.assert_not_called()

    def test_completed_reply_uncertain_send_is_claimed_and_watch_finishes(self):
        self.bridge.dispatch("work", 1)
        self.agent.messages.return_value = [{"id": f"msg_telegram_{OWNER}_1", "type": "user"}, self.reply()]
        self.telegram.send_text.reset_mock()
        self.telegram.send_text.side_effect = TelegramError("uncertain", uncertain=True)
        with self.assertRaises(TelegramError): self.bridge.monitor_once()
        self.telegram.send_text.side_effect = None
        self.bridge.monitor_once()
        self.telegram.send_text.assert_called_once()
        self.assertNotIn("ses_one", self.bridge.work)

    def test_idle_historic_threads_only_one_recovery_scan(self):
        self.bridge.ensure_session()
        self.bridge.monitor_once()
        self.bridge.monitor_once()
        self.agent.messages.assert_called_once()
        replacement = Bridge(self.config, self.state, self.telegram, self.agent)
        self.agent.messages.reset_mock()
        self.agent.messages.return_value = [self.reply(text="fresh recovery")]
        replacement.monitor_once()
        replacement.monitor_once()
        self.agent.messages.assert_called_once()
        self.assertIn("fresh recovery", self.telegram.send_text.call_args.args[1])

    def test_private_state_has_no_prompt_reasoning_tool_reply_or_title(self):
        self.bridge.command("/new", "PRIVATE_TITLE")
        self.bridge.dispatch("PRIVATE_PROMPT", 1)
        message = self.reply(text="VISIBLE_REPLY")
        message["content"] += [{"type": "reasoning", "text": "PRIVATE_REASONING"},
                               {"type": "tool", "text": "PRIVATE_TOOL"}]
        self.agent.messages.return_value = [message]
        self.bridge.monitor_once()
        values = json.dumps(self.state._db().execute("SELECT key,value FROM metadata").fetchall())
        for secret in (TOKEN, "PRIVATE_TITLE", "PRIVATE_PROMPT", "PRIVATE_REASONING", "PRIVATE_TOOL", "VISIBLE_REPLY"):
            self.assertNotIn(secret, values)
        self.assertNotIn("PRIVATE_REASONING", self.telegram.send_text.call_args.args[1])
        self.assertNotIn("PRIVATE_TOOL", self.telegram.send_text.call_args.args[1])


class ProductTransportTests(unittest.TestCase):
    def test_progress_transport_no_retry_and_redacts(self):
        telegram = TelegramClient(TOKEN)
        with patch.object(telegram, "call", side_effect=TelegramError("uncertain", uncertain=True)) as call:
            with self.assertRaises(TelegramError): telegram.edit_text(OWNER, 1, "hello " + TOKEN)
            call.assert_called_once()
            self.assertNotIn(TOKEN, call.call_args.args[1]["text"])

    def test_owner_menu_scope_and_callback_updates(self):
        telegram = TelegramClient(TOKEN)
        with patch.object(telegram, "call", return_value=[]) as call:
            telegram.set_commands(OWNER, COMMANDS)
            self.assertEqual(call.call_args.args[1]["scope"], {"type": "chat", "chat_id": OWNER})
            telegram.updates(0)
            self.assertEqual(call.call_args.args[1]["allowed_updates"], ["message", "callback_query"])


if __name__ == "__main__":
    unittest.main()
