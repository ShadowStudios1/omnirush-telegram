"""Private OmniRush device-account authentication."""

from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import stat
import time
import urllib.error
import urllib.request
import webbrowser
from urllib.parse import urlsplit, urlunsplit


DEFAULT_GATEWAY_URL = "https://omnirush.ai/omnirush/v1"
ACCOUNT_PATH = Path.home() / ".local/share/omnirush-telegram-portable/native/omnirush-account.json"
HTTP_TIMEOUT = 15
MAX_RESPONSE_BYTES = 256 * 1024
MAX_LOGIN_SECONDS = 15 * 60
MAX_POLL_INTERVAL = 60


class AccountError(RuntimeError):
    """A safe account error; response bodies and credentials are never exposed."""


def _url(value: str, hosts: set[str] | None = None, *, query: bool = False) -> str:
    if (not isinstance(value, str) or len(value) > 2048 or "\\" in value
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
        raise AccountError("OmniRush account gateway URL is invalid.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if (parsed.scheme != "https" or not host or parsed.username is not None
                or parsed.password is not None or (parsed.query and not query) or parsed.fragment
                or (hosts is not None and host.lower() not in hosts)):
            raise ValueError
        port = parsed.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError
        path = parsed.path or "/"
        if any(part in (".", "..") for part in path.split("/")):
            raise ValueError
    except (ValueError, UnicodeError):
        raise AccountError("OmniRush account gateway URL is invalid.") from None
    return urlunsplit(("https", parsed.netloc, path.rstrip("/") or "/", parsed.query if query else "", ""))


def _host(value: str) -> str:
    return urlsplit(value).hostname.lower()


def _approved_hosts(gateway: str) -> set[str]:
    return {_host(gateway)}


def _control_base(gateway: str) -> str:
    parsed = urlsplit(gateway)
    path = parsed.path.rstrip("/")
    if path.endswith("/v1"):
        path = path[:-3].rstrip("/") or "/"
    return urlunsplit(("https", parsed.netloc, path or "/", "", "")).rstrip("/")


class _ApprovedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, hosts: set[str]):
        super().__init__()
        self.hosts = hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _url(newurl, self.hosts, query=True)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _read_body(response) -> bytes:
    try:
        body = response.read(MAX_RESPONSE_BYTES + 1)
    except (OSError, ValueError):
        raise AccountError("OmniRush account service could not be read safely.") from None
    if not isinstance(body, bytes) or len(body) > MAX_RESPONSE_BYTES:
        raise AccountError("OmniRush account service response was too large.")
    return body


def _request(url: str, body: dict, hosts: set[str]) -> tuple[int, dict]:
    request = urllib.request.Request(
        url,
        data=json.dumps(body, allow_nan=False).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    opener = urllib.request.build_opener(_ApprovedRedirectHandler(hosts))
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            status = int(response.getcode())
            raw = _read_body(response)
    except urllib.error.HTTPError as response:
        with response:
            status = int(response.code)
            raw = _read_body(response)
    except (urllib.error.URLError, OSError, ValueError):
        raise AccountError("OmniRush account service is unavailable; no credentials were changed.") from None
    if not raw:
        return status, {}
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError):
        if status == 428:
            return status, {}
        raise AccountError("OmniRush account service returned an invalid response; no credentials were changed.") from None
    if not isinstance(value, dict):
        raise AccountError("OmniRush account service returned an invalid response; no credentials were changed.")
    return status, value


def _text(value: object, message: str, *, limit: int = 4096) -> str:
    if not isinstance(value, str) or not value or len(value) > limit or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise AccountError(message)
    return value


def _secure_path(path: Path) -> Path:
    path = Path(path).expanduser().absolute()
    from .config import PROJECT_ROOT
    if ".." in path.parts or path.resolve().is_relative_to(PROJECT_ROOT):
        raise AccountError("OmniRush account storage must be outside the project.")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise AccountError("OmniRush account storage must not contain symlinks.")
    return path


def _credentials(data: object) -> dict:
    if not isinstance(data, dict):
        raise AccountError("OmniRush account storage is invalid; sign in again.")
    try:
        gateway = _url(data["gateway_url"])
        access = _text(data["access_token"], "OmniRush account storage is invalid; sign in again.")
        refresh = _text(data["refresh_token"], "OmniRush account storage is invalid; sign in again.")
    except (KeyError, TypeError):
        raise AccountError("OmniRush account storage is invalid; sign in again.") from None
    return {"gateway_url": gateway, "access_token": access, "refresh_token": refresh}


def load_credentials() -> dict | None:
    path = _secure_path(ACCOUNT_PATH)
    from runner import _parent_fd
    from .state import StateError
    try:
        with _parent_fd(path) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                info = os.fstat(stream.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                        or info.st_size > 16384):
                    raise AccountError("OmniRush account storage must be a private chmod 600 file.")
                data = json.loads(stream.read(16385))
    except FileNotFoundError:
        return None
    except AccountError:
        raise
    except (OSError, UnicodeError, ValueError, RecursionError, StateError):
        raise AccountError("Cannot read private OmniRush account storage.") from None
    return _credentials(data)


def _store_credentials(data: dict) -> dict:
    data = _credentials(data)
    path = _secure_path(ACCOUNT_PATH)
    from runner import _parent_fd
    from .state import StateError
    temporary = path.name + ".new"
    try:
        with _parent_fd(path, create=True) as parent:
            try:
                old = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                old = None
            if old is not None and (not stat.S_ISREG(old.st_mode) or old.st_uid != os.getuid()
                                    or old.st_nlink != 1):
                raise AccountError("OmniRush account storage must be a user-owned regular file; it was preserved.")
            created = False
            try:
                fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                created = True
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    json.dump(data, stream, allow_nan=False, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path.name, src_dir_fd=parent, dst_dir_fd=parent)
                created = False
            finally:
                if created:
                    try:
                        os.unlink(temporary, dir_fd=parent)
                    except OSError:
                        pass
    except (OSError, TypeError, ValueError, StateError):
        raise AccountError("Could not save private OmniRush account credentials; old credentials were preserved.") from None
    return dict(data)


def login(ui, gateway_url: str | None = None) -> dict:
    gateway = _url(gateway_url or DEFAULT_GATEWAY_URL)
    hosts = _approved_hosts(gateway)
    control = _control_base(gateway)
    status, authorization = _request(control + "/device/authorize", {
        "device_name": "omnirush-telegram-portable",
        "platform": platform.system().lower() or "linux",
    }, hosts)
    if not 200 <= status < 300:
        raise AccountError("OmniRush account authorization could not be started; no credentials were changed.")
    try:
        device_code = _text(authorization["device_code"], "OmniRush account authorization response was invalid.")
        user_code = _text(authorization["user_code"], "OmniRush account authorization response was invalid.", limit=256)
        verification = _url(authorization["verification_uri_complete"], hosts, query=True)
        interval = authorization["interval"]
        expires = authorization["expires_in"]
        if (isinstance(interval, bool) or not isinstance(interval, (int, float))
                or not 0 < interval <= MAX_POLL_INTERVAL
                or isinstance(expires, bool) or not isinstance(expires, (int, float))
                or not 0 < expires <= 86400 or device_code in verification or device_code in user_code):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise AccountError("OmniRush account authorization response was invalid.") from None
    ui.say("Open this OmniRush verification URL in a browser: " + verification)
    ui.say("OmniRush verification code: " + user_code)
    try:
        webbrowser.open(verification)
    except Exception:
        pass
    deadline = time.monotonic() + min(float(expires), MAX_LOGIN_SECONDS)
    while True:
        if time.monotonic() >= deadline:
            raise AccountError("OmniRush account authorization timed out; no credentials were changed.")
        status, token = _request(control + "/device/token", {"device_code": device_code}, hosts)
        if status == 428:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AccountError("OmniRush account authorization timed out; no credentials were changed.")
            time.sleep(min(float(interval), remaining))
            continue
        if not 200 <= status < 300:
            raise AccountError("OmniRush account authorization was not completed; no credentials were changed.")
        try:
            result = {
                "gateway_url": _url(token["gateway_url"], hosts),
                "access_token": _text(token["access_token"], "OmniRush account token response was invalid."),
                "refresh_token": _text(token["refresh_token"], "OmniRush account token response was invalid."),
            }
        except (KeyError, TypeError):
            raise AccountError("OmniRush account token response was invalid; no credentials were changed.") from None
        return _store_credentials(result)


def authenticated_environment(base_env: dict[str, str] | None = None) -> dict[str, str]:
    credentials = load_credentials()
    if credentials is None:
        raise AccountError("OmniRush account is not signed in; run python3 omnirush.py login in a local terminal.")
    environment = dict(base_env if base_env is not None else os.environ)
    for key in tuple(environment):
        if key.startswith(("OMNIRUSH_", "ENGINE_", "XDG_", "OPENCODE_")):
            environment.pop(key, None)
    environment["OMNIRUSH_GATEWAY_URL"] = credentials["gateway_url"]
    environment["OMNIRUSH_ACCESS_TOKEN"] = credentials["access_token"]
    return environment


__all__ = ["ACCOUNT_PATH", "DEFAULT_GATEWAY_URL", "AccountError", "authenticated_environment", "load_credentials", "login"]
