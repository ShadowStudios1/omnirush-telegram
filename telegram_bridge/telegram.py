"""Small, non-retrying Telegram transport with privacy-safe failures."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets as _secrets
import stat
import urllib.error
import urllib.request


class TelegramError(RuntimeError):
    """A safe error message; an uncertain mutation must not be replayed."""

    def __init__(self, message: str, retry_after: int | None = None,
                 uncertain: bool = False) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.uncertain = uncertain


_TOKEN = re.compile(r"(?<![\w])(?:bot)?\d{5,}:[A-Za-z0-9_-]{20,}(?![\w])")
_AUTHORIZATION = re.compile(
    r"(?im)\b(?:proxy-)?authorization\s*[:=]\s*[^\r\n]+"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_COMMON_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{10,})\b"
)


def redact(text: str, secrets=()) -> str:
    """Remove supplied literal secrets and common credential-shaped text."""
    # Longest first so overlapping secrets cannot leave a partial credential.
    values = sorted({s for s in secrets if isinstance(s, str) and s},
                    key=len, reverse=True)
    for value in values:
        text = text.replace(value, "[REDACTED]")
    text = _AUTHORIZATION.sub("Authorization: [REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _TOKEN.sub("[REDACTED]", text)
    return _COMMON_TOKEN.sub("[REDACTED]", text)


def split_text(text: str, limit: int = 3500) -> list[str]:
    """Split without losing text or breaking an astral character in two."""
    if type(limit) is not int or not 2 <= limit <= 3500:
        raise ValueError("Text chunk limit must be between 2 and 3500.")
    if not isinstance(text, str):
        raise TypeError("Text must be a string.")
    chunks: list[str] = []
    start = 0
    units = 0
    for index, character in enumerate(text):
        width = 2 if ord(character) > 0xFFFF else 1
        if units + width > limit:
            chunks.append(text[start:index])
            start = index
            units = 0
        units += width
    if start < len(text):
        chunks.append(text[start:])
    return chunks


def authorized_message(update: dict, owner_id: int) -> dict | None:
    """Accept only new, direct private messages from the configured owner.

    'New' means the ordinary message update, not an edit or another event.
    Polling startup/offset policy determines which queued updates are fresh.
    """
    if type(owner_id) is not int or owner_id <= 0 or not isinstance(update, dict):
        return None
    if any(key in update for key in (
        "edited_message", "channel_post", "edited_channel_post",
        "business_message", "edited_business_message", "deleted_business_messages",
    )):
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    if any(isinstance(key, str) and key.startswith("forward_") for key in message):
        return None
    if any(key in message for key in (
        "via_bot", "sender_chat", "edit_date", "is_automatic_forward",
        "business_connection_id", "sender_business_bot",
    )):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict):
        return None
    if type(sender.get("id")) is not int or sender["id"] != owner_id:
        return None
    if sender.get("is_bot") is not False:
        return None
    if (chat.get("type") != "private" or type(chat.get("id")) is not int
            or chat["id"] != owner_id):
        return None
    return message


def authorized_callback(update: dict, owner_id: int) -> dict | None:
    """Validate the owner and the original private card, never inline mode."""
    if type(owner_id) is not int or owner_id <= 0 or not isinstance(update, dict):
        return None
    callback = update.get("callback_query")
    if not isinstance(callback, dict) or "inline_message_id" in callback:
        return None
    sender, message = callback.get("from"), callback.get("message")
    if (not isinstance(sender, dict) or type(sender.get("id")) is not int
            or sender["id"] != owner_id or sender.get("is_bot") is not False
            or not isinstance(message, dict)):
        return None
    chat = message.get("chat")
    if (not isinstance(chat, dict) or chat.get("type") != "private"
            or type(chat.get("id")) is not int or chat["id"] != owner_id
            or type(message.get("message_id")) is not int
            or type(message.get("date")) is not int
            or not isinstance(callback.get("id"), str) or not callback["id"]
            or not isinstance(callback.get("data"), str)):
        return None
    if any(key in message for key in ("via_bot", "sender_chat", "edit_date",
                                      "business_connection_id", "sender_business_bot")):
        return None
    if any(isinstance(key, str) and key.startswith("forward_") for key in message):
        return None
    return callback


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TelegramClient:
    MAX_DOCUMENT_BYTES = 20 * 1024 * 1024
    MAX_RESPONSE_BYTES = 8 * 1024 * 1024

    def __init__(self, token: str) -> None:
        if not isinstance(token, str) or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise TelegramError("Invalid Telegram token configuration.")
        self._token = token
        self._opener = urllib.request.build_opener(_NoRedirects())

    @staticmethod
    def _retry_after(body) -> int | None:
        if isinstance(body, dict) and isinstance(body.get("parameters"), dict):
            value = body["parameters"].get("retry_after")
            if type(value) is int and value >= 0:
                return value
        return None

    def _post(self, method: str, data: bytes, content_type: str,
              timeout: int = 40):
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", method):
            raise TelegramError("Invalid Telegram API method.")
        mutation = not method.startswith("get")
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + self._token + "/" + method,
            data=data, headers={"Content-Type": content_type}, method="POST",
        )
        try:
            with self._opener.open(request, timeout=timeout) as response:
                raw = response.read(self.MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            # Never propagate HTTPError: its representation includes the token URL.
            code = error.code if type(error.code) is int else 0
            body = None
            try:
                raw = error.read(self.MAX_RESPONSE_BYTES + 1)
                if len(raw) <= self.MAX_RESPONSE_BYTES:
                    body = json.loads(raw)
            except Exception:
                pass
            finally:
                try:
                    error.close()
                except Exception:
                    pass
            if 300 <= code < 400:
                raise TelegramError("Telegram redirect refused.") from None
            safe_code = str(code) if 400 <= code <= 599 else "unknown"
            raise TelegramError(
                "Telegram API request failed (HTTP " + safe_code + ").",
                retry_after=self._retry_after(body),
                uncertain=mutation and (code >= 500 or code == 0),
            ) from None
        except Exception:
            raise TelegramError("Telegram connection failed.", uncertain=mutation) from None
        try:
            if len(raw) > self.MAX_RESPONSE_BYTES:
                raise ValueError
            body = json.loads(raw)
            if not isinstance(body, dict) or type(body.get("ok")) is not bool:
                raise ValueError
            if body["ok"] and "result" not in body:
                raise ValueError
        except Exception:
            raise TelegramError("Invalid Telegram response.", uncertain=mutation) from None
        if not body["ok"]:
            code = body.get("error_code")
            safe_code = str(code) if type(code) is int and 400 <= code <= 599 else "unknown"
            raise TelegramError(
                "Telegram API rejected request (error " + safe_code + ").",
                retry_after=self._retry_after(body),
                uncertain=mutation and type(code) is int and code >= 500,
            ) from None
        return body["result"]

    def call(self, method: str, payload: dict | None = None):
        if payload is not None and not isinstance(payload, dict):
            raise TelegramError("Invalid Telegram request payload.")
        try:
            data = json.dumps(payload or {}, ensure_ascii=True, allow_nan=False).encode("utf-8")
        except Exception:
            raise TelegramError("Cannot encode Telegram request.") from None
        timeout = 40
        if method == "getUpdates" and payload:
            wait = payload.get("timeout", 25)
            if type(wait) is int and 0 <= wait <= 50:
                timeout = wait + 15
        return self._post(method, data, "application/json", timeout)

    def get_me(self):
        return self.call("getMe")

    def webhook_info(self):
        # A nonempty result['url'] is left for the caller to handle; never delete it.
        return self.call("getWebhookInfo")

    def updates(self, offset: int, timeout=25) -> list:
        if type(offset) is not int or offset < 0:
            raise TelegramError("Invalid Telegram update offset.")
        if type(timeout) is not int or not 0 <= timeout <= 50:
            raise TelegramError("Invalid Telegram polling timeout.")
        result = self.call("getUpdates", {
            "offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"],
        })
        if not isinstance(result, list):
            raise TelegramError("Invalid Telegram updates response.")
        return result

    @staticmethod
    def _check_chat(chat_id: int) -> None:
        if type(chat_id) is not int:
            raise TelegramError("Invalid Telegram chat identifier.")

    def send_text(self, chat_id: int, text: str, reply_markup: dict | None = None) -> list[int]:
        self._check_chat(chat_id)
        if not isinstance(text, str):
            raise TelegramError("Invalid Telegram message text.")
        message_ids: list[int] = []
        chunks = split_text(redact(text, (self._token,)))
        for index, chunk in enumerate(chunks):
            # No parse_mode and no retry, including after a partially sent message.
            payload = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = reply_markup
            receipt = self.call("sendMessage", payload)
            if not isinstance(receipt, dict):
                raise TelegramError("Invalid Telegram delivery receipt.", uncertain=True)
            message_id, chat = receipt.get("message_id"), receipt.get("chat")
            if (type(message_id) is not int or message_id < 0
                    or not isinstance(chat, dict) or type(chat.get("id")) is not int
                    or chat["id"] != chat_id):
                raise TelegramError("Invalid Telegram delivery receipt.", uncertain=True)
            message_ids.append(message_id)
        return message_ids

    def edit_text(self, chat_id: int, message_id: int, text: str) -> None:
        self._check_chat(chat_id)
        if type(message_id) is not int or message_id < 0 or not isinstance(text, str):
            raise TelegramError("Invalid Telegram card edit.")
        chunks = split_text(redact(text, (self._token,)))
        if len(chunks) != 1:
            raise TelegramError("Progress card must fit in one message.")
        self.call("editMessageText", {"chat_id": chat_id, "message_id": message_id,
                                     "text": chunks[0]})

    def typing(self, chat_id: int) -> None:
        self._check_chat(chat_id)
        self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})

    def answer_callback(self, callback_id: str) -> None:
        if not isinstance(callback_id, str) or not callback_id:
            raise TelegramError("Invalid Telegram callback identifier.")
        self.call("answerCallbackQuery", {"callback_query_id": callback_id})

    def set_commands(self, chat_id: int, commands: list[dict]) -> None:
        self._check_chat(chat_id)
        self.call("setMyCommands", {"commands": commands,
                                   "scope": {"type": "chat", "chat_id": chat_id}})

    def send_document(self, chat_id: int, path: Path, content: bytes | None = None) -> None:
        self._check_chat(chat_id)
        try:
            path = Path(path)
            if content is None:
                fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if not stat.S_ISREG(info.st_mode):
                        raise TelegramError("Telegram document must be a regular file.")
                    if info.st_size > self.MAX_DOCUMENT_BYTES:
                        raise TelegramError("Telegram document exceeds the 20 MiB limit.")
                    content = stream.read(self.MAX_DOCUMENT_BYTES + 1)
            elif not isinstance(content, bytes):
                raise TelegramError("Telegram document data must be bytes.")
            if len(content) > self.MAX_DOCUMENT_BYTES:
                raise TelegramError("Telegram document exceeds the 20 MiB limit.")
            filename = re.sub(r"[^A-Za-z0-9._-]", "_", path.name)[:200] or "document"
        except TelegramError:
            raise
        except Exception:
            raise TelegramError("Cannot read Telegram document.") from None
        boundary = "telegram-" + _secrets.token_hex(24)
        data = (
            ("--" + boundary + '\r\nContent-Disposition: form-data; name="chat_id"\r\n\r\n'
             + str(chat_id) + "\r\n").encode("ascii")
            + ("--" + boundary + '\r\nContent-Disposition: form-data; name="document"; filename="'
               + filename + '"\r\nContent-Type: application/octet-stream\r\n\r\n').encode("ascii")
            + content + ("\r\n--" + boundary + "--\r\n").encode("ascii")
        )
        self._post("sendDocument", data, "multipart/form-data; boundary=" + boundary, 90)
