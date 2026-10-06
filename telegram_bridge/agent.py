"""Authenticated, stdlib-only adapter for the installed OmniRush sidecar.

Headless invocations inject only the private OmniRush account's native environment.
Desktop authentication remains owned by the desktop CLI.
An uncertain write must be reconciled using its caller-supplied message ID before
the caller decides what to do; this adapter never retries a mutation.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
from pathlib import Path
import platform
import re
import subprocess
from urllib.parse import urlencode, urlsplit, urlunsplit
import urllib.error
import urllib.request


DEFAULT_EXECUTABLE = "/opt/OmniRush.ai/resources/sidecars/opencode-{}-unknown-linux-gnu".format(
    "aarch64" if platform.machine().lower() in ("aarch64", "arm64") else "x86_64"
)
DEFAULT_SERVER_URL = "auto"
CLI_TIMEOUT = 30
DISCOVERY_TIMEOUT = 5


class AgentError(RuntimeError):
    """An error with a safe message (never raw CLI output or request text)."""


class AgentUncertainError(AgentError):
    """A mutation may have reached the server; do not automatically repeat it."""


def _model_ref(model) -> dict:
    if (
        not isinstance(model, dict)
        or not {"id", "providerID"}.issubset(model)
        or set(model) - {"id", "providerID", "variant"}
        or any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v)
               for v in model.values())
    ):
        raise AgentError("Model must contain id and providerID strings.")
    return dict(model)


def _executable(value: str) -> str:
    if (not isinstance(value, str) or not value or not Path(value).is_absolute()
            or any(ord(c) < 32 for c in value) or "\\" in value
            or Path(value).suffix.lower() in (".exe", ".cmd", ".bat", ".ps1")
            or any(part == ".." for part in Path(value).parts)
            or any(part.is_symlink() for part in (Path(value), *Path(value).parents))):
        raise AgentError("Invalid sidecar executable; use an absolute native Linux path.")
    path = Path(value)
    if path.exists() and (not path.is_file() or not os.access(path, os.X_OK)):
        raise AgentError("Sidecar executable must be an executable regular file.")
    return value


def _rules(mode: str) -> list:
    if mode not in ("ask", "full"):
        raise AgentError("Permission mode must be ask or full.")
    return [{"action": "*", "resource": "*", "effect": "allow" if mode == "full" else "ask"}]


def discover_executable() -> str | None:
    """Discover only official native sidecars; never legacy npm/Windows opencode."""
    from .releases import ReleaseError, installed_runtime
    try:
        private = installed_runtime()
    except ReleaseError:
        raise AgentError("Private runtime failed verification; check the pinned runtime installation.") from None
    candidates = ([private] if private is not None else []) + [Path(DEFAULT_EXECUTABLE)]
    for path in candidates:
        try:
            _executable(str(path))
            if path.is_file() and os.access(path, os.X_OK):
                return str(path.absolute())
        except (OSError, AgentError):
            continue
    return None


def runtime_environment() -> dict[str, str]:
    """Return the isolated native CLI environment, without reading credentials."""
    from .runtime import runtime_environment as environment
    return environment()


def _loopback_url(value: str) -> str:
    if not isinstance(value, str) or any(c.isspace() for c in value):
        raise AgentError("The agent server must be a loopback URL.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        valid_host = host == "localhost" or (
            host is not None and ipaddress.ip_address(host).is_loopback
        )
        if (
            parsed.scheme not in ("http", "https")
            or not valid_host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            raise ValueError
    except ValueError:
        raise AgentError("The agent server must be a loopback URL.") from None
    return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))


def _id(value: str, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) > 256
        or not value.startswith(prefix)
        or len(value) == len(prefix)
        or re.fullmatch(r"[A-Za-z0-9_-]+", value) is None
    ):
        raise AgentError("Invalid agent resource ID.")
    return value


def _server_password(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if (not isinstance(value, str) or len(value) > 512
                or re.fullmatch(r"Basic [A-Za-z0-9+/]+={0,2}", value) is None):
            raise ValueError
        raw = base64.b64decode(value[6:], validate=True).decode("ascii")
        username, password = raw.split(":", 1)
        if username != "opencode" or not password or any(not 33 <= ord(c) <= 126 for c in password):
            raise ValueError
        return password
    except (ValueError, UnicodeError):
        raise AgentError("Invalid private backend authorization.") from None


class AgentClient:
    """Invoke the existing authenticated CLI; never start or configure a server.

    ``auto`` discovers a running loopback sidecar and reconnects once after read
    failures. Explicit URLs stay pinned. All subprocesses have bounded timeouts.
    """

    def __init__(
        self,
        executable: str = DEFAULT_EXECUTABLE,
        server_url: str = DEFAULT_SERVER_URL,
        model: dict | None = None,
        agent: str | None = None,
        permission_mode: str = "ask",
        backend_mode: str = "desktop",
        server_auth: str | None = None,
    ) -> None:
        _executable(executable)
        if model is not None:
            _model_ref(model)
        _rules(permission_mode)
        if backend_mode not in ("desktop", "headless"):
            raise AgentError("Backend mode must be desktop or headless.")
        if backend_mode == "desktop" and server_url == "managed":
            raise AgentError("Managed service requires headless mode.")
        if backend_mode == "headless" and server_url != "managed":
            if urlsplit(_loopback_url(server_url)).port is None:
                raise AgentError("The private agent server must specify a loopback port.")
        self._server_password = _server_password(server_auth)
        if agent is not None and (not isinstance(agent, str) or not agent):
            raise AgentError("Invalid agent selection.")
        self.executable = executable
        self._auto = server_url == "auto"
        self.server_url = server_url if server_url in ("auto", "managed") else _loopback_url(server_url)
        self.model = dict(model) if model is not None else None
        self.agent = agent
        self.permission_mode = permission_mode
        self.backend_mode = backend_mode
        self.server_auth = server_auth

    def _invoke(self, server_url: str, method: str, path: str, data: str | None):
        # Explicit headless endpoints are the official managed-server protocol.
        # Calling HTTP directly avoids the CLI's separate service discovery
        # layer, which can wait indefinitely while the loopback engine is
        # already listening.
        if server_url != "managed" and not self._auto and self.backend_mode == "headless" and self.server_auth:
            return self._invoke_http(server_url, method, path, data)
        command = [self.executable, "api"]
        if server_url != "managed":
            command.extend(["--server", _loopback_url(server_url)])
        command.extend([method, path])
        if data is not None:
            command.extend(["--data", data])
        mutating = method not in ("GET", "HEAD", "OPTIONS")
        environment = self._authenticated_environment() if self.backend_mode == "headless" else None
        if environment is not None and server_url != "managed" and self._server_password is not None:
            # V2 authenticates server discovery before processing API headers.
            # Supply both native password names without exposing secrets in argv.
            environment = {**environment, "OPENCODE_PASSWORD": self._server_password,
                           "OPENCODE_SERVER_PASSWORD": self._server_password}
        try:
            result = subprocess.run(
                command,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLI_TIMEOUT,
                shell=False,
                **({"env": environment} if environment is not None else {}),
            )
        except subprocess.TimeoutExpired:
            if mutating:
                raise AgentUncertainError(
                    "Agent request timed out; its outcome is uncertain. Do not retry automatically."
                ) from None
            raise AgentError("Agent read timed out.") from None
        except (OSError, ValueError):
            raise AgentError("Could not launch the installed agent CLI.") from None
        if result.returncode != 0:
            if mutating:
                raise AgentUncertainError(
                    "Agent CLI failed; the write outcome is uncertain. Do not retry automatically."
                )
            raise AgentError("Agent read failed; check the running app and CLI authentication.")
        output = result.stdout.strip()
        if not output:
            return None  # The CLI emits no body for HTTP 204.
        try:
            return json.loads(output)
        except (ValueError, RecursionError):
            if mutating:
                raise AgentUncertainError(
                    "Agent returned an unreadable write response; its outcome is uncertain."
                ) from None
            raise AgentError("Agent returned an unreadable response.") from None

    def _invoke_http(self, server_url: str, method: str, path: str, data: str | None):
        url = _loopback_url(server_url) + path
        headers = {"Accept": "application/json", "User-Agent": "omnirush-telegram-portable/3.1.1"}
        if self.server_auth:
            headers["Authorization"] = self.server_auth
        if data is not None:
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data.encode("utf-8") if data is not None else None,
                                         headers=headers, method=method)
        mutating = method not in ("GET", "HEAD", "OPTIONS")
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(request, timeout=CLI_TIMEOUT) as response:
                status = int(response.getcode())
                raw = response.read(4 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as response:
            status = int(response.code)
            try:
                response.close()
            except OSError:
                pass
            raw = b""
        except urllib.error.URLError:
            if mutating:
                raise AgentUncertainError("Agent request failed; its outcome is uncertain. Do not retry automatically.") from None
            raise AgentError("Agent read failed; check the running app and CLI authentication.") from None
        except (OSError, ValueError):
            if mutating:
                raise AgentUncertainError("Agent request failed; its outcome is uncertain. Do not retry automatically.") from None
            raise AgentError("Agent read failed; check the running app and CLI authentication.") from None
        if len(raw) > 4 * 1024 * 1024:
            if mutating:
                raise AgentUncertainError("Agent returned an oversized write response; its outcome is uncertain.") from None
            raise AgentError("Agent returned an oversized response.") from None
        if not 200 <= status < 300:
            if mutating:
                raise AgentUncertainError("Agent request was rejected; its outcome is uncertain. Do not retry automatically.") from None
            raise AgentError("Agent read failed; check the running app and CLI authentication.") from None
        if not raw:
            return None
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, ValueError, RecursionError):
            if mutating:
                raise AgentUncertainError("Agent returned an unreadable write response; its outcome is uncertain.") from None
            raise AgentError("Agent returned an unreadable response.") from None

    @staticmethod
    def _authenticated_environment() -> dict[str, str]:
        from .account import AccountError
        from .runtime import authenticated_runtime_environment
        try:
            return authenticated_runtime_environment()
        except AccountError as error:
            raise AgentError(str(error)) from None

    def call(self, method: str, path: str, body=None):
        """Return decoded JSON (or None for 204), never exposing CLI diagnostics."""
        if not isinstance(method, str) or method.upper() not in (
            "GET", "HEAD", "OPTIONS", "POST", "PUT", "PATCH", "DELETE"
        ):
            raise AgentError("Invalid agent API method.")
        method = method.upper()
        if (
            not isinstance(path, str)
            or not path.startswith("/api/")
            or "#" in path
            or any(ord(c) < 33 or ord(c) == 127 for c in path)
            or "\\" in path
            or any(part in (".", "..") for part in path.split("?", 1)[0].split("/"))
        ):
            raise AgentError("Invalid agent API path.")
        try:
            data = json.dumps(body, allow_nan=False) if body is not None else None
        except (TypeError, ValueError, RecursionError):
            raise AgentError("Agent request body is not valid JSON.") from None
        if self.server_url == "auto":
            self.discover()
        try:
            return self._invoke(self.server_url, method, path, data)
        except AgentError:
            if not self._auto or method not in ("GET", "HEAD", "OPTIONS"):
                raise
        # Only a read is safe to retry. Discovery itself only performs reads.
        self.discover()
        return self._invoke(self.server_url, method, path, data)

    @staticmethod
    def _server_info(value) -> dict:
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("version"), str)
            or type(value.get("pid")) is not int
            or value["pid"] <= 0
            or not isinstance(value.get("urls"), list)
            or not all(isinstance(url, str) for url in value["urls"])
            or not isinstance(value.get("paths"), dict)
            or not isinstance(value["paths"].get("tmp"), str)
        ):
            raise AgentError("The endpoint did not identify a valid agent server.")
        return value

    def health(self) -> dict:
        if self.backend_mode == "headless" and self.server_auth and not self._auto:
            result = self.call("GET", "/api/health")
            if not isinstance(result, dict) or result.get("healthy") is not True:
                raise AgentError("The private headless backend did not report healthy.")
            return {"version": "official-engine", "pid": 1,
                    "urls": [self.server_url], "paths": {"tmp": "private"}}
        return self._server_info(self.call("GET", "/api/info"))

    def discover(self) -> str:
        """Find native opencode-owned loopback listeners using ss.

        Validate candidates through the authenticated CLI and match the reported
        server PID to the listener. No /proc, environments or auth files are read.
        """
        try:
            result = subprocess.run(
                ["ss", "-ltnp"],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=DISCOVERY_TIMEOUT,
                shell=False,
            )
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise AgentError("Could not discover the running sidecar using ss.") from None
        if result.returncode != 0:
            raise AgentError("Could not discover the running sidecar using ss.")
        candidates: dict[str, set[int]] = {}
        for line in result.stdout.splitlines():
            columns = line.split()
            if len(columns) < 6 or columns[0] != "LISTEN":
                continue
            local = re.fullmatch(r"(127\.0\.0\.1|\[?::1\]?):(\d{1,5})", columns[3])
            pids = re.findall(r'\("(?:opencode(?:-[A-Za-z0-9_-]+)?|omnirush(?:-[A-Za-z0-9_-]+)?)",pid=(\d+),', line)
            if not local or not pids or not 1 <= int(local[2]) <= 65535:
                continue
            host = "127.0.0.1" if local[1] == "127.0.0.1" else "[::1]"
            url = "http://" + host + ":" + str(int(local[2]))
            candidates.setdefault(url, set()).update(int(pid) for pid in pids)
        valid = []
        for url, pids in candidates.items():
            try:
                info = self._server_info(self._invoke(url, "GET", "/api/info", None))
            except AgentError:
                continue
            if info["pid"] in pids:
                valid.append(url)
        if self.server_url in valid:
            return self.server_url
        if not valid:
            raise AgentError("No authenticated running OmniRush sidecar was found.")
        if len(valid) != 1:
            raise AgentError("Multiple sidecars were found; configure an explicit loopback URL.")
        self.server_url = valid[0]
        return self.server_url

    @staticmethod
    def _data(response, expected_type: type, *, mutating: bool = False):
        if (
            not isinstance(response, dict)
            or not isinstance(response.get("data"), expected_type)
        ):
            if mutating:
                raise AgentUncertainError(
                    "Agent write response was incomplete; its outcome is uncertain."
                )
            raise AgentError("Agent response has an unexpected structure.")
        return response["data"]

    @staticmethod
    def _session_path(session_id: str) -> str:
        return "/api/session/" + _id(session_id, "ses")

    def create_session(
        self, directory: str, title: str = "Telegram remote workspace",
        permission_mode: str | None = None,
    ) -> dict:
        if not isinstance(directory, str) or not directory or "\x00" in directory:
            raise AgentError("A workspace directory is required.")
        if not isinstance(title, str):
            raise AgentError("Session title must be text.")
        body = {
            "title": title,
            "location": {"directory": directory},
            "permissions": _rules(self.permission_mode if permission_mode is None else permission_mode),
            "metadata": {"origin": "telegram-bridge"},
        }
        if self.model is not None:
            body["model"] = dict(self.model)
        if self.agent is not None:
            body["agent"] = self.agent
        return self._data(self.call("POST", "/api/session", body), dict, mutating=True)

    def session(self, session_id: str) -> dict:
        return self._data(self.call("GET", self._session_path(session_id)), dict)

    def set_permissions(self, session_id: str, mode: str) -> None:
        """Change only this session's rules. Never override global policy."""
        response = self.call("PATCH", self._session_path(session_id), {"permissions": _rules(mode)})
        if response is not None:
            raise AgentUncertainError("Permission change response was unexpected; verify session rules before retrying.")

    @staticmethod
    def _location_query(directory: str | None) -> str:
        if directory is None:
            return ""
        if not isinstance(directory, str) or not directory or "\x00" in directory:
            raise AgentError("A workspace directory is required.")
        return "?" + urlencode({"location[directory]": directory})

    def models(self, directory: str | None = None) -> list:
        models = self._data(self.call("GET", "/api/model" + self._location_query(directory)), list)
        for model in models:
            if not isinstance(model, dict):
                raise AgentError("Agent model response is invalid.")
            _model_ref({key: model[key] for key in ("id", "providerID") if key in model})
        return models

    def default_model(self, directory: str | None = None) -> dict | None:
        response = self.call("GET", "/api/model/default" + self._location_query(directory))
        if isinstance(response, dict) and response.get("data", ...) is None:
            return None
        model = self._data(response, dict)
        _model_ref({key: model[key] for key in ("id", "providerID") if key in model})
        return model

    def set_model(self, session_id: str, model: dict | str) -> None:
        """Accept a native Model.Ref or provider/model selection; no global change."""
        if isinstance(model, str):
            provider, separator, name = model.partition("/")
            if not separator:
                raise AgentError("Model selection must be provider/model.")
            model = {"providerID": provider, "id": name}
        response = self.call("POST", self._session_path(session_id) + "/model", {"model": _model_ref(model)})
        if response is not None:
            raise AgentUncertainError("Model change response was unexpected; verify the session before retrying.")

    def usage(self, session_id: str) -> dict:
        """Return native session usage only, never fabricated account quota."""
        session = self.session(session_id)
        return {key: session[key] for key in ("cost", "tokens") if key in session}

    def prompt(self, session_id: str, text: str, message_id: str) -> dict:
        """Queue a prompt with a durable caller-selected msg_ ID, without retries."""
        path = self._session_path(session_id) + "/prompt"
        _id(message_id, "msg_")
        if not isinstance(text, str):
            raise AgentError("Prompt must be text.")
        body = {"id": message_id, "text": text, "delivery": "queue"}
        return self._data(self.call("POST", path, body), dict, mutating=True)

    def messages(self, session_id: str, limit: int = 100) -> list:
        """Return the latest ``limit`` projected messages in chronological order.

        All message types are preserved. Pages contain at most 100 messages and
        subsequent requests use cursor.next without combining cursor and order.
        Callers needing older history may request a larger limit; the default
        deliberately returns only the newest 100, not the entire conversation.
        """
        path = self._session_path(session_id) + "/message"
        if type(limit) is not int or limit < 0:
            raise AgentError("Message limit must be a nonnegative integer.")
        if limit == 0:
            return []
        newest_first = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor = None
        while len(newest_first) < limit:
            query = {"limit": min(100, limit - len(newest_first))}
            if cursor is None:
                query["order"] = "desc"
            else:
                query["cursor"] = cursor
            response = self.call("GET", path + "?" + urlencode(query))
            page = self._data(response, list)
            cursors = response.get("cursor")
            if not isinstance(cursors, dict):
                raise AgentError("Agent message pagination response is invalid.")
            previous_count = len(newest_first)
            for message in page:
                if not isinstance(message, dict) or not isinstance(message.get("id"), str):
                    raise AgentError("Agent message response is invalid.")
                if message["id"] in seen_ids:
                    continue
                seen_ids.add(message["id"])
                newest_first.append(message)
                if len(newest_first) == limit:
                    break
            if len(newest_first) >= limit or not page:
                break
            if len(newest_first) == previous_count:
                raise AgentError("Agent message pagination did not advance.")
            next_cursor = cursors.get("next")
            if next_cursor is None:
                break
            if (
                not isinstance(next_cursor, str)
                or not next_cursor
                or next_cursor in seen_cursors
            ):
                raise AgentError("Agent message pagination did not advance.")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        newest_first.reverse()
        return newest_first

    def permissions(self, session_id: str) -> list:
        path = self._session_path(session_id) + "/permission"
        return self._data(self.call("GET", path), list)

    def reply_permission(
        self, session_id: str, request_id: str, decision: str
    ) -> None:
        if decision not in ("once", "reject"):
            raise AgentError("Permission decision must be once or reject; persistent grants are disabled.")
        path = (
            self._session_path(session_id)
            + "/permission/"
            + _id(request_id, "per")
            + "/reply"
        )
        self.call("POST", path, {"decision": decision})

    def forms(self, session_id: str) -> list:
        path = self._session_path(session_id) + "/form"
        return self._data(self.call("GET", path), list)

    def reply_form(self, session_id: str, form_id: str, answer: dict) -> None:
        if not isinstance(answer, dict) or any(
            not isinstance(key, str)
            or not (
                isinstance(value, (str, bool, int, float))
                or (isinstance(value, list) and all(isinstance(item, str) for item in value))
            )
            for key, value in answer.items()
        ):
            raise AgentError("Form answer must contain supported field values.")
        path = (
            self._session_path(session_id)
            + "/form/"
            + _id(form_id, "frm_")
            + "/reply"
        )
        self.call("POST", path, {"answer": answer})

    def interrupt(self, session_id: str) -> dict:
        response = self.call("POST", self._session_path(session_id) + "/interrupt")
        if not isinstance(response, dict) or type(response.get("interrupted")) is not bool:
            raise AgentUncertainError("Agent interruption response was incomplete; its outcome is uncertain.")
        return response

    def active(self) -> dict:
        return self._data(self.call("GET", "/api/session/active"), dict)


__all__ = ["AgentClient", "AgentError", "AgentUncertainError", "discover_executable", "runtime_environment"]
