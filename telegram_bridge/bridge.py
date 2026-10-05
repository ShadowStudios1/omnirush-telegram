"""At-most-once owner commands, explicit approvals, and private operational state."""

from collections import deque
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time

from .agent import AgentClient, AgentError, AgentUncertainError
from .config import Config, ConfigError
from .state import StateStore
from .telegram import TelegramClient, TelegramError, authorized_callback, authorized_message, redact


HELP = """🚀 OmniRush remote workspace
Send a normal text message to ask the agent to work.
/status — 🟢 backend, project, thread, mode
/projects — 📁 configured project roots
/project /absolute/folder — switch within configured project roots
/threads — 💬 your bot-created conversations
/thread ses_ID — select a bot-created conversation
/new [title] — start a fresh dedicated conversation (only when idle)
/model [provider/id] — list or switch available models
/usage — 📊 session lifetime cost and token usage
/context — model limits (not live context consumption)
/quota — native account quota availability
/mode — session ask/full mode and consent instructions
/latest — explicitly resend the latest completed agent text
/pending — list approvals and questions
/approve per_ID — approve exactly one pending action, once
/deny per_ID — reject an action
/answer frm_ID {\"field_key\":\"value\"} — answer a question
/stop — interrupt the current project session
/get relative/file.ext — download a non-secret project file (up to 20 MiB)
/start or /help — this help (plain Start works too)

Private messages from the configured owner only. Ask mode by default; full session tool access requires typed opt-in. No root/OS/org/website permission bypass. Telegram bot chats are not end-to-end encrypted. Never send credentials.
Commands sent while the bridge is offline are not auto-executed on restart; resend them. Only Start/help requests can receive this help after restart."""

FULL_CONFIRMATION = "I ACCEPT SESSION TOOL ACCESS"
COMMANDS = [{"command": command, "description": description} for command, description in (
    ("start", "🚀 Dashboard and help"), ("status", "🟢 Backend and current thread"),
    ("projects", "📁 Allowed project roots"), ("project", "Select an allowed project folder"),
    ("threads", "💬 Bot-created threads"), ("thread", "Select a bot-created thread"),
    ("new", "New thread (optional title)"), ("model", "Model list or provider/id switch"),
    ("usage", "📊 Lifetime session usage"), ("context", "Model token limits"),
    ("quota", "Account quota availability"), ("mode", "Ask/full session access"),
    ("pending", "Pending approvals and questions"), ("approve", "Approve one pending action once"),
    ("deny", "Reject one pending action"), ("answer", "Answer a non-secret question"),
    ("latest", "Resend latest visible reply"), ("stop", "Interrupt current execution"),
    ("get", "Download a checked project artifact"), ("help", "Help and safety notes"),
)]


class Bridge:
    def __init__(self, config: Config, state: StateStore,
                 telegram: TelegramClient, agent: AgentClient):
        self.config, self.state, self.telegram, self.agent = config, state, telegram, agent
        self.lock = threading.RLock()
        self.stopping = threading.Event()
        self.accept_after = int(time.time())
        self.requests = deque()
        self.last_monitor_error = 0.0
        stored = state.get("project", str(config.project))
        try:
            self.project = config.project_path(stored)
        except ConfigError:
            self.project = config.project
        self.sessions = state.get("sessions", {})
        if not isinstance(self.sessions, dict):
            raise ConfigError("Session state is invalid; inspect private state locally.")
        self.threads = state.get("threads", {})
        self.work = state.get("work", {})
        if not isinstance(self.threads, dict) or not isinstance(self.work, dict):
            raise ConfigError("Thread state is invalid; inspect private state locally.")
        # The legacy mapping contains only sessions created by this bridge.
        for project, sid in self.sessions.items():
            if isinstance(sid, str) and re.fullmatch(r"ses[A-Za-z0-9_-]+", sid):
                self.threads.setdefault(sid, {"project": project, "created": 0,
                                             "mode": "ask"})
        self.state.set("threads", self.threads)
        self.future_mode = state.get("future_mode", "ask")
        if self.future_mode not in ("ask", "full"):
            self.future_mode = "ask"
        # A config flag alone cannot turn an old bridge-owned session into full.
        # Consent is typed in this bot; policy applies only to this bot's sessions.
        self.agent.permission_mode = self.future_mode
        self.cards = {}
        self.quick_actions = {}
        self.watch_once = set(self.threads)
        self.pending_sessions = set(state.get("pending_sessions", [])) & set(self.threads)
        self.last_card_edit = self.last_typing = float("-inf")
        self.state.set("project", str(self.project))

    def send(self, text: str, actions: tuple = ()) -> list[int] | None:
        text = redact(text, (self.config.token,))
        now = time.time()
        self.quick_actions = {key: value for key, value in self.quick_actions.items()
                              if value["expires"] >= now}
        keys, markup = [], {"inline_keyboard": []}
        for label, command, argument in actions:
            key = "q:" + secrets.token_hex(12)
            keys.append((key, command, argument))
            markup["inline_keyboard"].append([{"text": label, "callback_data": key}])
        if actions:
            ids = self.telegram.send_text(self.config.owner_id, text, reply_markup=markup)
        else:
            ids = self.telegram.send_text(self.config.owner_id, text)
        if isinstance(ids, list) and ids and all(type(value) is int and value >= 0 for value in ids):
            self.state.set("last_telegram_delivery", {
                "message_ids": ids, "chat_id": self.config.owner_id, "time": int(time.time()),
            })
            for key, command, argument in keys:
                self.quick_actions[key] = {"command": command, "argument": argument,
                                           "message_id": ids[-1], "expires": now + 600}
            # Only ephemeral, fixed command mappings, never prompt contents.
            while len(self.quick_actions) > 200:
                self.quick_actions.pop(next(iter(self.quick_actions)))
            return ids
        return None

    def session_id(self) -> str | None:
        sid = self.sessions.get(str(self.project))
        metadata = self.threads.get(sid, {})
        return sid if metadata.get("project") == str(self.project) else None

    def ensure_session(self, fresh: bool = False, title: str = "") -> str:
        existing = self.session_id()
        if existing and not fresh:
            # Validate without silently replacing lost sessions or replaying work.
            self.checked_session(existing)
            return existing
        self.agent.permission_mode = self.future_mode
        session = self.agent.create_session(str(self.project), title or f"Telegram: {self.project.name}")
        session_id = session.get("id")
        if not isinstance(session_id, str) or not re.fullmatch(r"ses[A-Za-z0-9_-]+", session_id):
            raise AgentUncertainError("Session creation is uncertain; inspect OmniRush before retrying.")
        self.sessions[str(self.project)] = session_id
        # Persist identifiers, location, timestamps and policy only, not titles
        # supplied by the user, prompts, tools, reasoning, or output text.
        self.threads[session_id] = {"project": str(self.project), "created": int(time.time()),
                                    "mode": self.future_mode}
        self.state.set("threads", self.threads)
        self.state.set("sessions", self.sessions)
        self.watch_once.add(session_id)
        return session_id

    def checked_session(self, sid: str) -> dict:
        metadata = self.threads.get(sid)
        if not metadata:
            raise ConfigError("Only bot-created threads can be accessed. Use /threads.")
        project = self.config.project_path(metadata["project"])
        info = self.agent.session(sid)
        if isinstance(info, dict):
            directory = info.get("location", {}).get("directory")
            if directory is not None and self.config.project_path(directory) != project:
                raise ConfigError("This thread's native project changed. Select another bot-created thread.")
        return info

    def busy(self, sid: str | None) -> bool:
        if not sid:
            return False
        return bool(sid in self.work or sid in self.agent.active()
                    or self.agent.permissions(sid) or self.agent.forms(sid))

    def handle_callback(self, update: dict) -> None:
        callback = authorized_callback(update, self.config.owner_id)
        if not callback:
            return
        message = callback["message"]
        with self.lock:
            action = self.quick_actions.get(callback["data"])
            now = time.time()
            if (not action or action["expires"] < now
                    or message["date"] < self.accept_after or message["date"] > now + 30
                    or action["message_id"] != message["message_id"]):
                return
            if not self.state.claim_callback(callback["id"]):
                return
            self.quick_actions.pop(callback["data"])
            # Read-only fixed menu actions and owned-thread selection only.
            if action["command"] not in ("/status", "/projects", "/threads", "/thread",
                                           "/usage", "/context", "/pending", "/help"):
                return
            monotonic = time.monotonic()
            while self.requests and monotonic - self.requests[0] > 60:
                self.requests.popleft()
            if len(self.requests) >= 30:
                return
            self.requests.append(monotonic)
            try:
                self.command(action["command"], action["argument"])
            except AgentUncertainError:
                self.send("The backend request outcome is uncertain. It will NOT be repeated.")
            except (AgentError, ConfigError, ValueError) as error:
                self.send(str(error))
            # Answer only after dispatch; callback acknowledgement never gates work
            # and is never retried on a failed/uncertain acknowledgement.
            try:
                self.telegram.answer_callback(callback["id"])
            except TelegramError:
                pass

    def handle_update(self, update: dict) -> None:
        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
            return
        update_id = update["update_id"]
        if not self.state.claim_update(update_id):
            self.state.set("offset", update_id + 1)
            return
        # Both claim and offset are durable BEFORE dispatch. Crashes do not replay.
        self.state.set("offset", update_id + 1)
        if "callback_query" in update:
            self.handle_callback(update)
            return
        message = authorized_message(update, self.config.owner_id)
        if not message:
            return
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            self.send("Text instructions only for now. Use /get to receive project files.")
            return
        normalized = text.strip()
        command = normalized.split(maxsplit=1)[0].split("@", 1)[0].lower()
        help_request = normalized.lower() == "start" or command in ("/start", "/help")
        date = message.get("date")
        if type(date) is not int or date < 0 or (date < self.accept_after and not help_request):
            self.send("Ignored a message sent before this bridge started. Please resend the instruction.")
            return
        now = time.monotonic()
        while self.requests and now - self.requests[0] > 60:
            self.requests.popleft()
        if len(self.requests) >= 30:
            self.send("Rate limit reached; wait a minute. This command was not dispatched.")
            return
        self.requests.append(now)
        if len(text) > 16000:
            self.send("Instruction too long; keep it under 16,000 characters.")
            return
        if redact(text, (self.config.token,)) != text:
            self.send("Credential-shaped text refused. Keep tokens and passwords out of Telegram instructions.")
            return
        try:
            with self.lock:
                self.dispatch(normalized, update_id)
        except AgentUncertainError:
            self.send("The backend request outcome is uncertain. It will NOT be repeated. Check /status, /latest and OmniRush before issuing it again.")
        except (AgentError, ConfigError, ValueError) as error:
            self.send(str(error))

    def dispatch(self, text: str, update_id: int) -> None:
        if text.lower() == "start":
            self.command("/start", "")
            return
        if text.startswith("/"):
            parts = text.split(maxsplit=1)
            command, argument = parts[0], parts[1] if len(parts) > 1 else ""
            command = command.split("@", 1)[0].lower()
            self.command(command, argument.strip())
            return
        sid = self.ensure_session()
        guidance = (
            "[Telegram remote request from the authenticated workspace owner]\n"
            f"Current project: {self.project}\n"
            "Work in this project and respect existing OmniRush folder/OS permissions. "
            "Do not read or send credentials, bot configuration, authentication stores, or private logs. "
            "Require explicit confirmation for deletion, privilege escalation, deployment and other consequential actions. "
            "Do not disable approvals or change global settings. Report actual edits, file paths, tests and any blockers. "
            "Replies will be forwarded to Telegram; never include secrets.\n\nRequest:\n"
        )
        previous_work = dict(self.work[sid]) if sid in self.work else None
        if sid not in self.work:
            self.work[sid] = {"started": time.time(), "uncertain": False}
        self.work[sid]["request_id"] = f"msg_telegram_{self.config.owner_id}_{update_id}"
        self.work[sid]["accepted"] = False
        # Durable metadata before queueing; an uncertain request is watched, not replayed.
        self.state.set("work", self.work)
        try:
            self.agent.prompt(sid, guidance + text, f"msg_telegram_{self.config.owner_id}_{update_id}")
        except AgentUncertainError:
            self.work[sid]["uncertain"] = True
            self.state.set("work", self.work)
            raise
        except AgentError:
            if previous_work is None:
                self.work.pop(sid, None)
            else:
                self.work[sid] = previous_work
            self.state.set("work", self.work)
            raise
        if sid not in self.cards:
            # Claim card creation before send. An uncertain send cannot create a second card.
            self.cards[sid] = {"message_id": None, "disabled": False,
                               "typing_disabled": False, "last_edit": float("-inf"),
                               "started": self.work[sid]["started"], "frame": 0}
            ids = self.send(f"⏳ Request queued in OmniRush · 0s\nProject: {self.project}\n"
                            f"Thread: {sid}\nReplies and approval requests will arrive here.")
            if ids and len(ids) == 1:
                self.cards[sid]["message_id"] = ids[0]

    def command(self, command: str, argument: str) -> None:
        sid = self.session_id()
        if sid and command in ("/latest", "/pending", "/approve", "/deny", "/answer", "/stop"):
            self.checked_session(sid)
        if command in ("/start", "/help"):
            self.send(HELP, (("🟢 Status", "/status", ""), ("📁 Projects", "/projects", ""),
                             ("💬 Threads", "/threads", "")))
        elif command == "/status":
            info = self.agent.health()
            active = bool(sid and sid in self.agent.active())
            mode = self.threads.get(sid, {}).get("mode", self.future_mode)
            status = "working / waiting for approval" if active else "idle"
            if sid in self.work and not active:
                status = "queued / checking completion"
                if self.work[sid].get("uncertain"):
                    status = "request outcome uncertain; inspect OmniRush (no automatic retry)"
            self.send(f"🟢 Bridge: online\nLinux user: {os.getuid()} (no automatic sudo)\n"
                      f"Backend: OpenCode {info['version']} inside OmniRush\n"
                      f"Placement: {getattr(self.config, 'backend_mode', 'desktop')}\n"
                      f"📁 Project: {self.project}\n💬 Thread: {sid or 'not created yet'}\n"
                      f"Mode: {mode} · future threads: {self.future_mode}\n"
                      f"Agent: {status}\n"
                      "Laptop/VM and bridge must stay awake; desktop placement also needs OmniRush running.")
        elif command == "/projects":
            self.send("📁 Allowed project roots\n" + "\n".join(str(root) for root in self.config.roots)
                      + f"\nSelected: {self.project}\n/project /absolute/folder")
        elif command == "/threads":
            lines, actions = ["💬 Bot-created threads"], []
            for thread, metadata in self.threads.items():
                try:
                    self.config.project_path(metadata["project"])
                except (ConfigError, KeyError):
                    continue
                # Titles are retrieved only on request, never kept in private state.
                info = self.checked_session(thread)
                title = info.get("title", "") if isinstance(info, dict) else ""
                title = title[:120] if isinstance(title, str) else ""
                lines.append(f"{'▶ ' if thread == sid else ''}{thread} · {title or 'Telegram thread'}\n  {metadata['project']}")
                if len(actions) < 8:
                    actions.append((f"💬 {thread[:30]}", "/thread", thread))
            if len(lines) == 1:
                lines.append("No threads yet. Use /new [title] or send an instruction.")
            lines.append("/thread ses_ID — only the IDs listed here can be selected.")
            self.send("\n".join(lines), tuple(actions))
        elif command == "/thread":
            if argument not in self.threads:
                self.send("Unknown bot-created thread. Use /threads; arbitrary session IDs are not accessible.")
                return
            if sid != argument and self.busy(sid):
                self.send("Current thread is working or awaiting input. Finish it before switching threads.")
                return
            self.checked_session(argument)
            candidate = self.config.project_path(self.threads[argument]["project"])
            self.sessions[str(candidate)] = argument
            self.state.set("sessions", self.sessions)
            self.state.set("project", str(candidate))
            self.project = candidate
            self.watch_once.add(argument)
            self.send(f"💬 Thread selected: {argument}\n📁 Project: {candidate}")
        elif command == "/project":
            if not argument:
                self.send(f"Current project: {self.project}\nUsage: /project /absolute/folder")
                return
            candidate = self.config.project_path(argument)
            if candidate != self.project and self.busy(sid):
                self.send("Current thread is working or awaiting input. Finish it before switching projects.")
                return
            self.state.set("project", str(candidate))
            self.project = candidate
            self.send(f"Project selected: {candidate}. Send a text instruction to work on it.")
        elif command == "/new":
            if self.busy(sid):
                self.send("This session is working or awaiting input. Finish it or use /stop before /new.")
                return
            if len(argument) > 120 or redact(argument, (self.config.token,)) != argument:
                raise ValueError("Use a non-secret thread title of at most 120 characters.")
            self.send(f"💬 New dedicated session: {self.ensure_session(fresh=True, title=argument)}")
        elif command == "/model":
            self.model_command(argument)
        elif command == "/mode":
            self.mode_command(argument)
        elif command == "/quota":
            self.send("📊 Account quota: unavailable from native API. Check your provider or organization portal; session usage is available with /usage.")
        elif command == "/get":
            self.download(argument)
        elif not sid:
            self.send("No conversation yet for this project. Send a normal text instruction first.")
        elif command in ("/usage", "/context"):
            self.usage_command(sid, context=command == "/context")
        elif command == "/latest":
            latest = [m for m in self.agent.messages(sid, 500)
                      if m.get("type") == "assistant" and m.get("time", {}).get("completed")
                      and self.assistant_text(m)]
            self.send(self.assistant_text(latest[-1]) if latest else "No completed text reply yet.")
        elif command == "/pending":
            self.show_pending(sid, force=True)
        elif command in ("/approve", "/deny"):
            pending = self.agent.permissions(sid)
            if not argument or not any(p.get("id") == argument for p in pending):
                self.send("That action is not pending in this project. Use /pending and copy its exact per_ID.")
                return
            self.agent.reply_permission(sid, argument, "once" if command == "/approve" else "reject")
            self.send("Action approved once." if command == "/approve" else "Action rejected.")
        elif command == "/answer":
            form_id, _, answer_text = argument.partition(" ")
            forms = self.agent.forms(sid)
            form = next((f for f in forms if f.get("id") == form_id), None)
            if not form:
                self.send("That form is not pending. Use /pending and copy its frm_ID.")
                return
            answer = json.loads(answer_text)
            if not isinstance(answer, dict):
                raise ValueError('Answer must be a JSON object, such as {"field_key":"value"}.')
            fields = {f["key"]: f for f in form.get("fields", []) if isinstance(f, dict) and "key" in f}
            for key in answer:
                if key not in fields or fields[key].get("type") == "external":
                    raise ValueError("Use the desktop to answer external/sign-in fields.")
                label = " ".join(str(fields[key].get(name, "")) for name in ("key", "title", "description"))
                if fields[key].get("hidden") or re.search(r"password|secret|token|credential|api.?key|\botp\b", label, re.I):
                    raise ValueError("Do not send sensitive input via Telegram; answer it on the desktop.")
            if redact(answer_text, (self.config.token,)) != answer_text:
                raise ValueError("Credential-shaped input refused. Enter secrets only in the local app.")
            self.agent.reply_form(sid, form_id, answer)
            self.send("Answer submitted.")
        elif command == "/stop":
            result = self.agent.interrupt(sid)
            if sid in self.work:
                self.work[sid]["stopped"] = True
                self.state.set("work", self.work)
            if not result["interrupted"]:
                self.work.pop(sid, None)
                self.state.set("work", self.work)
                self.cards.pop(sid, None)
            self.send("Active execution interrupted. Already-completed edits are preserved. Pending queued inputs may remain parked in OmniRush; review them in the desktop before resuming."
                      if result["interrupted"] else "No active execution to interrupt.")
        else:
            self.send("Unknown command. Use /help.")

    def mode_command(self, argument: str) -> None:
        sid = self.session_id()
        mode = self.threads.get(sid, {}).get("mode", self.future_mode)
        warning = ("⚠️ Full mode lets this bot's current and future sessions run native tools without individual ask approvals. "
                   "It can edit/delete project files or run commands as your user. It does not grant sudo, OS, organization, "
                   "website, or previously denied actions; pending approvals are never auto-approved. "
                   f"Only when idle, type exactly:\n/mode full {FULL_CONFIRMATION}\n"
                   "/mode ask — restore ask for the current and future bot sessions.")
        if not argument:
            self.send(f"🛡 Current mode: {mode}\nFuture threads: {self.future_mode}\n" + warning)
            return
        if argument not in ("ask", "full " + FULL_CONFIRMATION):
            self.send("No policy changed. " + warning)
            return
        if self.busy(sid):
            self.send("No policy changed. Finish active work and pending input before switching mode.")
            return
        chosen = "ask" if argument == "ask" else "full"
        if sid:
            self.checked_session(sid)
            self.agent.set_permissions(sid, chosen)
            self.threads[sid]["mode"] = chosen
            self.state.set("threads", self.threads)
        self.future_mode = chosen
        self.agent.permission_mode = chosen
        self.state.set("future_mode", chosen)
        self.send(f"🛡 {chosen.capitalize()} mode selected for the current and future bot-created threads. "
                  "Other existing threads keep their own policy. No global permissions changed.")

    @staticmethod
    def model_label(model) -> str:
        if not isinstance(model, dict):
            return "unavailable"
        provider, identifier = model.get("providerID"), model.get("id")
        return f"{provider}/{identifier}" if isinstance(provider, str) and isinstance(identifier, str) else "unavailable"

    def model_command(self, argument: str) -> None:
        models = self.agent.models(str(self.project))
        models = [model for model in models if isinstance(model, dict)
                  and model.get("enabled") is not False and self.model_label(model) != "unavailable"]
        if not argument:
            sid = self.session_id()
            current = self.checked_session(sid).get("model") if sid else None
            if not current:
                current = self.agent.default_model(str(self.project))
            lines = ["🤖 Current model: " + self.model_label(current), "Available models:"]
            lines.extend(self.model_label(model) for model in models)
            if not models:
                lines.append("No enabled models reported. Connect a provider in OmniRush.")
            lines.append("/model provider/id — switch this thread when idle.")
            self.send("\n".join(lines))
            return
        selected = next((model for model in models if self.model_label(model) == argument), None)
        if selected is None:
            self.send("Unknown or disabled model. Use /model and copy a listed provider/id.")
            return
        sid = self.session_id()
        if self.busy(sid):
            self.send("Finish active work and pending input before switching models.")
            return
        sid = self.ensure_session()
        self.agent.set_model(sid, {"providerID": selected["providerID"], "id": selected["id"]})
        self.send("🤖 Model selected for this thread: " + argument)

    @staticmethod
    def numeric(value, money: bool = False) -> str:
        if type(value) not in (int, float) or value < 0 or value != value or value == float("inf"):
            return "unavailable"
        return f"${value:.6f} USD" if money else f"{value:,.0f}"

    def usage_command(self, sid: str, context: bool = False) -> None:
        info = self.checked_session(sid)
        tokens = info.get("tokens", {})
        tokens = tokens if isinstance(tokens, dict) else {}
        cache = tokens.get("cache", {})
        cache = cache if isinstance(cache, dict) else {}
        ref = info.get("model")
        if not isinstance(ref, dict):
            ref = self.agent.default_model(str(self.project))
        model = next((item for item in self.agent.models(str(self.project))
                      if self.model_label(item) == self.model_label(ref)), None)
        limits = model.get("limit", {}) if isinstance(model, dict) else {}
        limits = limits if isinstance(limits, dict) else {}
        lines = ["📊 " + ("Model context limits" if context else "Session lifetime usage"),
                 f"Thread: {sid}", "Model: " + self.model_label(ref),
                 "Session lifetime cost: " + self.numeric(info.get("cost"), money=True),
                 "Lifetime input: " + self.numeric(tokens.get("input")),
                 "Lifetime output: " + self.numeric(tokens.get("output")),
                 "Lifetime reasoning tokens (count only): " + self.numeric(tokens.get("reasoning")),
                 "Lifetime cache read / write: " + self.numeric(cache.get("read")) + " / " + self.numeric(cache.get("write")),
                 "Model context limit: " + self.numeric(limits.get("context")),
                 "Model output limit: " + self.numeric(limits.get("output")),
                 "Live current-context consumption: unavailable from native API. Lifetime totals are NOT current context usage."]
        self.send("\n".join(lines))

    @staticmethod
    def assistant_text(message: dict) -> str:
        # Forward only visible prose, not reasoning, raw tools, logs, or payloads.
        content = message.get("content", [])
        return "\n\n".join(p["text"] for p in content if isinstance(p, dict)
                            and p.get("type") == "text" and isinstance(p.get("text"), str)).strip()

    def download(self, argument: str) -> None:
        if not argument:
            self.send("Usage: /get relative/path/to/output.ext")
            return
        raw = Path(argument)
        if raw.is_absolute() or any(p in ("..",) or p.startswith(".") for p in raw.parts):
            raise ConfigError("Downloads require a non-hidden path relative to the selected project.")
        try:
            path = (self.project / raw).resolve(strict=True)
            relative = path.relative_to(self.project)
        except (OSError, ValueError, RuntimeError):
            raise ConfigError("File does not exist inside the selected project.") from None
        if any(p.startswith(".") for p in relative.parts):
            raise ConfigError("Hidden/private files cannot be downloaded through this bridge.")
        if (path.name.lower() in {"auth.json", "config.json", "credentials.json", "id_rsa", "id_ed25519"}
                or path.suffix.lower() in {".pem", ".key", ".p12", ".pfx", ".sqlite3", ".log"}
                or re.search(r"token|secret|credential|password", path.name, re.I)):
            raise ConfigError("Credential/configuration/log files cannot be sent through Telegram.")
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > self.telegram.MAX_DOCUMENT_BYTES:
                    raise ConfigError("Download must be a regular project file no larger than 20 MiB.")
                data = stream.read(self.telegram.MAX_DOCUMENT_BYTES + 1)
        except OSError:
            raise ConfigError("Could not securely read this project artifact.") from None
        if len(data) > self.telegram.MAX_DOCUMENT_BYTES:
            raise ConfigError("File grew beyond the download limit.")
        text = data.decode("utf-8", errors="ignore")
        if redact(text, (self.config.token,)) != text:
            raise ConfigError("This file appears to contain credentials; download refused.")
        # Upload the exact bytes checked above, not a file reopened after checking.
        self.telegram.send_document(self.config.owner_id, path, content=data)

    def show_pending(self, sid: str, force: bool = False) -> bool:
        permissions = self.agent.permissions(sid)
        forms = self.agent.forms(sid)
        if force and not permissions and not forms:
            self.send("No native agent approvals or questions pending. Website/sign-in confirmations may still require the desktop browser panel.")
        for request in permissions:
            key = f"permission-notified:{sid}:{request['id']}"
            if force or not self.state.get(key, False):
                self.state.set(key, True)
                resources = "\n".join(str(r) for r in request.get("resources", []))[:8000]
                self.send(f"Approval required ({sid})\nAction: {request.get('action', 'tool')}\n"
                          f"Resources:\n{resources}\n\nReview before approving:\n"
                          f"/approve {request['id']}\n/deny {request['id']}\nApproval is one-time only.")
        for form in forms:
            key = f"form-notified:{sid}:{form['id']}"
            if not force and self.state.get(key, False):
                continue
            self.state.set(key, True)
            lines = [f"Question ({sid}): {form.get('title', '')}"]
            for field in form.get("fields", []):
                lines.append(f"Field {field.get('key')}: {field.get('title', '')} ({field.get('type', 'string')})")
                if field.get("description"):
                    lines.append(str(field["description"])[:2000])
                if field.get("options"):
                    lines.append("Options: " + json.dumps(field["options"], ensure_ascii=False)[:2000])
            lines.append(f"/answer {form['id']} {{\"field_key\":\"value\"}}\nUse JSON arrays for multiple-choice fields. Never answer secret/sign-in fields here.")
            self.send("\n".join(lines))
        return bool(permissions or forms)

    def progress(self, sid: str, finished: bool = False) -> None:
        card = self.cards.get(sid)
        if not card:
            return  # Restart recovery never resends or recreates a card.
        now = time.monotonic()
        if finished:
            card.setdefault("finished_at", time.time())
        finished = "finished_at" in card
        elapsed = max(0, int(card.get("finished_at", time.time()) - card["started"]))
        edited = False
        if (not card["disabled"] and card["message_id"] is not None
                and now - card["last_edit"] >= 10 and now - self.last_card_edit >= 10):
            card["last_edit"] = self.last_card_edit = now
            card["frame"] += 1
            icon = "✅" if finished else ("⏳", "⌛", "🔄", "⚙️")[card["frame"] % 4]
            status = "Finished" if finished else "Working / waiting for input"
            try:
                edited = True  # Claim before the edit; never repeat an uncertain edit.
                self.telegram.edit_text(self.config.owner_id, card["message_id"],
                                        f"{icon} {status} · {elapsed}s\nThread: {sid}\n"
                                        "Visible replies and approvals arrive separately.")
            except TelegramError:
                # Includes 429 and uncertain outcomes: disable, do not retry this card.
                card["disabled"] = True
        if not finished and not card["typing_disabled"] and now - self.last_typing >= 8:
            self.last_typing = now
            try:
                self.telegram.typing(self.config.owner_id)
            except TelegramError:
                card["typing_disabled"] = True
        if finished and (edited or card["disabled"] or card["message_id"] is None):
            self.cards.pop(sid, None)

    def monitor_once(self) -> None:
        with self.lock:
            active = self.agent.active()
            candidates = (set(self.work) | self.watch_once | self.pending_sessions
                          | (set(active) & set(self.threads)))
            for sid in sorted(candidates):
                metadata = self.threads.get(sid)
                if not metadata:
                    continue
                project = metadata["project"]
                try:
                    self.config.project_path(project)
                except ConfigError:
                    continue
                self.checked_session(sid)
                messages = self.agent.messages(sid, 500 if sid in self.watch_once else 100)
                expected = self.work.get(sid, {}).get("request_id")
                request_index = next((index for index, message in enumerate(messages)
                                      if message.get("id") == expected), None)
                if request_index is not None and sid in self.work:
                    self.work[sid]["accepted"] = True
                    self.state.set("work", self.work)
                relevant = messages[request_index + 1:] if request_index is not None else messages
                accepted = expected is None or self.work.get(sid, {}).get("accepted", False)
                terminal = bool(accepted and any(m.get("type") == "assistant" and
                                m.get("time", {}).get("completed") for m in relevant)
                                and not any(m.get("type") == "assistant" and
                                not m.get("time", {}).get("completed") for m in relevant))
                reported_key = f"reported:{sid}"
                reported = self.state.get(reported_key, [])
                for message in messages:
                    if (message.get("type") != "assistant" or
                            not message.get("time", {}).get("completed") or
                            message["id"] in reported):
                        continue
                    text = self.assistant_text(message)
                    if not text and message.get("error"):
                        text = "The agent reported an error. Inspect this session in OmniRush for details; raw error payloads are not forwarded."
                    # Claim delivery BEFORE Telegram send. Never automatically resend
                    # after a timeout or crash; /latest is an explicit recovery action.
                    reported = (reported + [message["id"]])[-1000:]
                    self.state.set(reported_key, reported)
                    if text:
                        started = self.work.get(sid, {}).get("started")
                        duration = f" · {max(0, int(time.time() - started))}s elapsed" if started else ""
                        self.send(f"✅ [{Path(project).name}] · {sid}{duration}\n{text}")
                self.watch_once.discard(sid)
                pending = self.show_pending(sid)
                if pending:
                    self.pending_sessions.add(sid)
                else:
                    self.pending_sessions.discard(sid)
                self.state.set("pending_sessions", sorted(self.pending_sessions))
                # Active state is a point-in-time snapshot. The queued request must
                # appear before an older completion is allowed to finish its watch.
                finished = bool((terminal or self.work.get(sid, {}).get("stopped"))
                                and sid not in active and not pending)
                self.progress(sid, finished=finished)
                if finished:
                    self.work.pop(sid, None)
                    self.state.set("work", self.work)
            # A completion inside the edit throttle schedules one final edit,
            # without continuing to read messages for an idle thread.
            for sid in list(self.cards):
                if "finished_at" in self.cards[sid]:
                    self.progress(sid, finished=True)

    def monitor(self) -> None:
        while not self.stopping.is_set():
            try:
                self.monitor_once()
            except (AgentError, TelegramError, ConfigError):
                now = time.monotonic()
                if now - self.last_monitor_error >= 60:
                    print("Monitor could not read or deliver an update; no action was repeated.", flush=True)
                    self.last_monitor_error = now
            except Exception:
                print("Monitor stopped after an internal error; inspect locally. No action was repeated.", flush=True)
                self.stopping.set()
                return
            self.stopping.wait(self.config.monitor_interval)

    def run(self) -> None:
        self.agent.health()
        identity = self.telegram.get_me()
        webhook = self.telegram.webhook_info()
        if not isinstance(webhook, dict) or webhook.get("url"):
            raise ConfigError("An existing Telegram webhook prevents polling. It has not been changed.")
        if not isinstance(identity, dict) or not identity.get("is_bot"):
            raise ConfigError("Telegram did not identify a bot.")
        binding = {"bot_id": identity.get("id"), "owner_id": self.config.owner_id}
        previous = self.state.get("identity")
        if previous is not None and previous != binding:
            raise ConfigError("This private state belongs to a different bot or owner. Use a separate state_path; existing work was preserved.")
        self.state.set("identity", binding)
        try:
            self.telegram.set_commands(self.config.owner_id, COMMANDS)
        except TelegramError:
            print("Telegram menu update failed or is uncertain; it was not retried. Text commands remain available.", flush=True)
        print(f"Bridge ready: @{identity.get('username', 'bot')}; owner-only private chats.", flush=True)
        print("Keep OmniRush, this process, the VM and laptop awake. No public listening socket opened.", flush=True)
        monitor = threading.Thread(target=self.monitor, name="agent-replies", daemon=True)
        monitor.start()
        backoff = 1
        try:
            while not self.stopping.is_set():
                try:
                    updates = self.telegram.updates(self.state.get("offset", 0), timeout=25)
                except TelegramError as error:
                    if any(code in str(error) for code in ("401", "409")):
                        raise ConfigError("Polling stopped: invalid/revoked token or another active bot poller. Nothing was replaced.") from None
                    print("Telegram polling unavailable; retrying reads only.", flush=True)
                    self.stopping.wait(min(error.retry_after or backoff, 60))
                    backoff = min(backoff * 2, 30)
                    continue
                backoff = 1
                for update in updates:
                    if self.stopping.is_set():
                        break
                    try:
                        self.handle_update(update)
                    except TelegramError:
                        print("Telegram reply failed or is uncertain; dispatched commands were not repeated. Use /latest.", flush=True)
        finally:
            self.stopping.set()
            # Wait until no worker can touch private state before closing it.
            monitor.join(timeout=180)
            if monitor.is_alive():
                print("Reply worker is still unwinding; process exit will stop it.", flush=True)
