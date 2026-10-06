"""Application service lifecycle; systemd user units or a stoppable supervisor.

The detached fallback survives terminal logout only, not reboot/host sleep. No
scheduler, public listener, privilege escalation, or firewall rule is created.
"""
from __future__ import annotations

from contextlib import contextmanager
import base64
import fcntl
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import threading
import time
from urllib.parse import urlsplit

from .config import PROJECT_ROOT
from .environment import systemd_user_available


BOT_UNIT = "omnirush-telegram-portable.service"
BACKEND_UNIT = "omnirush-telegram-portable-backend.service"
BACKEND_READY_TIMEOUT = 90
MARKER = "# Managed by omnirush-telegram-portable; application supervision only\n"
UNIT_DIR = Path.home() / ".config/systemd/user"


class ServiceError(RuntimeError):
    pass


def new_backend_endpoint() -> tuple[str, str]:
    """Reserve a loopback port and return its private Basic auth header."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", 0))
            port = int(probe.getsockname()[1])
        # OpenCode's loopback server uses the fixed username "opencode";
        # only the password is secret. This matches both the bundled engine
        # and the official CLI's managed-server client.
        username = "opencode"
        password = secrets.token_urlsafe(32)
        encoded = base64.b64encode(f"{username}:{password}".encode("ascii")).decode("ascii")
        return f"http://127.0.0.1:{port}", "Basic " + encoded
    except (OSError, ValueError):
        raise ServiceError("Could not allocate a private loopback backend endpoint.") from None


def _backend_credentials(config):
    value = getattr(config, "server_auth", None)
    if value is None and config.server_url == "managed":
        return None
    try:
        if not isinstance(value, str) or len(value) > 512 or not value.startswith("Basic "):
            raise ValueError
        raw = base64.b64decode(value[6:], validate=True).decode("ascii")
        username, password = raw.split(":", 1)
        if username != "opencode" or not password or any(not 33 <= ord(char) <= 126 for char in raw):
            raise ValueError
        return username, password
    except (ValueError, UnicodeError):
        raise ServiceError("Private backend authorization is invalid; rerun setup.") from None


def _backend_port(config):
    try:
        endpoint = config.server_url
        if not isinstance(endpoint, str):
            raise ValueError
        parsed = urlsplit(endpoint)
        port = parsed.port
        # The native `serve` command speaks HTTP, and setup owns one exact
        # IPv4 loopback listener. Do not accept credentials, paths, or hosts
        # that its client could interpret differently from the server.
        if port is None or not 1 <= port <= 65535 or endpoint != f"http://127.0.0.1:{port}":
            raise ValueError
        return port
    except (ValueError, TypeError):
        raise ServiceError("Private backend endpoint is invalid; rerun setup.") from None


def backend_command(config) -> list[str]:
    """Build the official engine's explicit loopback-server command."""
    if config.server_url == "managed":
        # Compatibility for configurations created before explicit endpoint
        # credentials were added. New headless setup never uses this branch.
        return [config.executable, "serve", "--service", "--hostname", "127.0.0.1"]
    port = _backend_port(config)
    _backend_credentials(config)
    return [config.executable, "serve", "--hostname", "127.0.0.1", "--port", str(port)]


def backend_environment(config):
    from .runtime import authenticated_runtime_environment
    if config.server_url != "managed":
        _backend_port(config)
    credentials = _backend_credentials(config)
    environment = authenticated_runtime_environment()
    if config.server_url != "managed":
        environment["npm_config_audit"] = "false"
    if credentials is not None:
        username, password = credentials
        environment["OPENCODE_SERVER_USERNAME"] = username
        environment["OPENCODE_SERVER_PASSWORD"] = password
        # The bundled V2 sidecar reads OPENCODE_PASSWORD before the official
        # CLI's OPENCODE_SERVER_PASSWORD. Override both inherited values.
        environment["OPENCODE_PASSWORD"] = password
    return environment


def _primitives():
    # Reuse the preserved baseline's no-symlink dirfd traversal and verified PID
    # lifetime protocol, but never use its start-only lifecycle.
    import runner
    return runner


def unit_quote(value, *, command=False):
    value = str(value)
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError("Service paths must not contain control characters.")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def unit_path(value):
    """Escape a systemd path assignment without adding literal quote bytes."""
    value = str(value)
    if not value.startswith("/") or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ServiceError("Service working directories must be absolute and contain no control characters.")
    escaped = []
    for char in value:
        if char == "%":
            escaped.append("%%")
        elif char == "\\":
            escaped.append("\\x5c")
        elif char == '"':
            escaped.append("\\x22")
        elif char == " ":
            escaped.append("\\x20")
        elif char == "\t":
            escaped.append("\\x09")
        else:
            escaped.append(char)
    return "".join(escaped)


def render_units(config, root=PROJECT_ROOT, python=None):
    python = python or sys.executable
    prefix = " ".join(unit_quote(p, command=True) for p in (python, "-u"))
    common = (
        "[Service]\nType=simple\n"
        f"WorkingDirectory={unit_path(root)}\n"
        "UMask=0077\nRestart=on-failure\nRestartSec=10s\n"
        "TimeoutStopSec=35s\nKillMode=control-group\n"
        "Environment=PYTHONDONTWRITEBYTECODE=1\n"
        "StandardInput=null\nStandardOutput=null\nStandardError=null\n"
    )
    limit = "StartLimitIntervalSec=120s\nStartLimitBurst=5\n"
    dependency = f"Requires={BACKEND_UNIT}\nAfter={BACKEND_UNIT}\n" if config.backend_mode == "headless" else ""
    bot = (MARKER + "[Unit]\nDescription=Owner-only OmniRush Telegram bridge\n"
           + dependency + limit + "\n" + common
           + f"ExecStart={prefix} {unit_quote(Path(root) / 'bot.py', command=True)}\n"
           + "\n[Install]\nWantedBy=default.target\n")
    result = {BOT_UNIT: bot}
    if config.backend_mode == "headless":
        result[BACKEND_UNIT] = (
            MARKER + "[Unit]\nDescription=Private loopback OmniRush headless backend\n"
            + limit + "\n" + common
            + f"ExecStart={prefix} -m telegram_bridge.runtime serve\n"
            + "\n[Install]\nWantedBy=default.target\n"
        )
    return result


def _systemctl(*args, allow_failure=False):
    try:
        result = subprocess.run(["systemctl", "--user", *args], stdin=subprocess.DEVNULL,
                                capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ServiceError("User service command failed or timed out; verify status before retrying.") from None
    if result.returncode and not allow_failure:
        raise ServiceError("User service command failed; check your user manager locally. No automatic retry.")
    return result


def _owned_unit(name, directory=None):
    path = (directory or UNIT_DIR) / name
    if path.is_symlink():
        raise ServiceError("Service unit is a symlink; it was preserved.")
    try:
        return path.read_text(encoding="utf-8").startswith(MARKER)
    except FileNotFoundError:
        return False
    except (OSError, UnicodeError):
        raise ServiceError("Cannot inspect service unit; it was preserved.") from None


def units_installed():
    return _owned_unit(BOT_UNIT)


def install(config, *, enable=False, directory=None):
    if not systemd_user_available():
        raise ServiceError("No working systemd user manager. Use `python3 omnirush.py run`, or `start` for logout-only durability.")
    directory = directory or UNIT_DIR
    units = render_units(config)
    for name in units:
        path = directory / name
        if path.exists() or path.is_symlink():
            if not _owned_unit(name, directory):
                raise ServiceError("An unrelated service unit already exists; it was not overwritten.")
    primitives = _primitives()
    # Check every destination before creating/replacing any unit.
    for name, contents in units.items():
        path = directory / name
        with primitives._parent_fd(path, create=True) as parent:
            temporary = name + ".new"
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                         0o600, dir_fd=parent)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(contents)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    # Remove an obsolete owned backend unit when attaching to Desktop.
    if config.backend_mode == "desktop" and _owned_unit(BACKEND_UNIT, directory):
        _systemctl("disable", "--now", BACKEND_UNIT)
        (directory / BACKEND_UNIT).unlink()
    _systemctl("daemon-reload")
    if enable:
        _systemctl("enable", *units.keys())


def uninstall():
    names = [name for name in (BOT_UNIT, BACKEND_UNIT) if _owned_unit(name)]
    if not names:
        return
    if not systemd_user_available():
        raise ServiceError("User manager unavailable; cannot safely disable installed services. No files were removed.")
    _systemctl("disable", "--now", *names)
    for name in names:
        if not _owned_unit(name):
            raise ServiceError("Service unit changed during removal; it was preserved.")
        (UNIT_DIR / name).unlink()
    _systemctl("daemon-reload")


def enable_linger():
    """Call only after explicit user consent. Never sudo or install a scheduler."""
    try:
        result = subprocess.run(["loginctl", "enable-linger", str(os.getuid())],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired):
        raise ServiceError("Could not enable linger; ask your administrator, without rerunning setup as root.") from None
    if result.returncode:
        raise ServiceError("Linger was not enabled; your administrator may need to allow it. No sudo was attempted.")


def _client(config):
    from .agent import AgentClient
    return AgentClient(config.executable, config.server_url, config.model, config.agent,
                       permission_mode=config.permission_mode, backend_mode=config.backend_mode,
                       server_auth=getattr(config, "server_auth", None))


def _finish(child):
    if child is None or child.poll() is not None:
        return
    child.terminate()
    try:
        child.wait(timeout=15)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=5)


@contextmanager
def backend_process(config, *, stopping=None):
    """Own only the backend we create; never stop an existing managed backend."""
    from .agent import AgentError
    command = backend_command(config)
    client = _client(config)
    child = None
    try:
        try:
            client.health()
        except AgentError:
            child = subprocess.Popen(
                command, cwd=PROJECT_ROOT, env=backend_environment(config), stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            # The official 1.18.x engine can bind its port before its database,
            # config and provider graph are ready on a small EC2 instance.
            deadline = time.monotonic() + BACKEND_READY_TIMEOUT
            while True:
                if stopping is not None and stopping.is_set():
                    raise ServiceError("Headless backend stopped before becoming ready.")
                code = child.poll()
                if code is not None:
                    status = f"exit status {code}" if code >= 0 else f"signal {-code} (return code {code})"
                    raise ServiceError(
                        f"Headless backend stopped before becoming ready ({status}). "
                        "Check the private runtime configuration locally; auth/config was preserved. No automatic retry."
                    ) from None
                try:
                    client.health()
                    break
                except AgentError:
                    if time.monotonic() >= deadline:
                        raise ServiceError("Headless backend is not ready; its private auth/config was preserved.") from None
                    time.sleep(0.25)
        client._portable_child = child
        yield client
    finally:
        _finish(child)


def _record_path(config):
    return config.state_path.parent / "supervisor.json"


def _record(config):
    return _primitives()._read_record(_record_path(config))


def _alive(config):
    return _primitives().process_matches(_record(config), PROJECT_ROOT / "omnirush.py")


def is_running(config):
    primitives = _primitives()
    if _alive(config) or primitives.state_locked(config.state_path):
        return True
    # The old launcher may still be starting, before it owns the state lock.
    old = primitives._read_record(config.state_path.parent / "process.json")
    if primitives.process_matches(old, PROJECT_ROOT / "bot.py"):
        return True
    if units_installed() and systemd_user_available():
        result = _systemctl("show", "--property=ActiveState", "--value", BOT_UNIT, allow_failure=True)
        return result.stdout.strip() in (b"active", b"activating", b"deactivating", b"reloading")
    return False


def status(config):
    if units_installed():
        if not systemd_user_available():
            return "User services installed, but manager unreachable; state is unknown."
        result = _systemctl("show", "--property=ActiveState", "--value", BOT_UNIT, allow_failure=True)
        state = result.stdout.decode("utf-8", "replace").strip()
        if state not in {"active", "inactive", "failed", "activating", "deactivating", "reloading"}:
            state = "unknown"
        placement = ("The private native backend is managed independently of the desktop GUI."
                     if config.backend_mode == "headless" else "Desktop attach requires the desktop app to stay open.")
        return f"Systemd bridge: {state}. {placement}"
    if _alive(config):
        ready = _primitives().state_locked(config.state_path)
        return "Supervisor: " + ("running (bot instance lock held)" if ready else "initializing / not ready") + ". No reboot durability."
    if _primitives().state_locked(config.state_path):
        return "Bot instance lock held by another launcher; not controlled by this supervisor."
    return "Stopped."


def run(config):
    """Foreground/container supervisor, also used by detached start; owns children."""
    primitives = _primitives()
    record_path = _record_path(config)
    stopping = threading.Event()
    handlers = {}
    children = []
    os.umask(0o077)
    with primitives._parent_fd(record_path, create=True) as parent:
        lock = os.open("supervisor.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW,
                       0o600, dir_fd=parent)
        try:
            primitives._check_file(lock, secure=True)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ServiceError("A supervisor is already running; no duplicate started.") from None
            if primitives.state_locked(config.state_path):
                raise ServiceError("A bot is already running; no duplicate started.")
            ticks = primitives.process_start_ticks(os.getpid())
            if ticks is None:
                raise ServiceError("Could not verify supervisor process identity.")
            primitives._save_record(parent, record_path.name,
                                    {"pid": os.getpid(), "start_ticks": ticks})
            for sig in (signal.SIGINT, signal.SIGTERM):
                handlers[sig] = signal.signal(sig, lambda *_: stopping.set())
            @contextmanager
            def desktop():
                _client(config).health()
                yield
            with (backend_process(config, stopping=stopping) if config.backend_mode == "headless" else desktop()) as client:
                if stopping.is_set():
                    return 0
                child = subprocess.Popen([sys.executable, "-u", str(PROJECT_ROOT / "bot.py")],
                                         cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL)
                children.append(child)
                try:
                    while not stopping.wait(0.25):
                        backend = getattr(client, "_portable_child", None)
                        if backend is not None and backend.poll() is not None:
                            return 1
                        code = child.poll()
                        if code is not None:
                            return code if code >= 0 else 1
                    return 0
                finally:
                    # Stop bot before taking down its backend.
                    _finish(child)
        finally:
            for child in children:
                _finish(child)
            for sig, handler in handlers.items():
                signal.signal(sig, handler)
            os.close(lock)


def start(config):
    if is_running(config):
        return "Already running or initializing; nothing was started."
    if units_installed():
        if not systemd_user_available():
            raise ServiceError("Installed user services cannot be reached; restore the manager or explicitly uninstall them before fallback.")
        _systemctl("start", BOT_UNIT)
        return status(config)
    primitives = _primitives()
    log_path = config.state_path.parent / "supervisor.log"
    with primitives._parent_fd(log_path, create=True) as parent:
        fd = os.open(log_path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        try:
            primitives._check_file(fd, secure=True)
            child = subprocess.Popen([sys.executable, "-u", str(PROJECT_ROOT / "omnirush.py"), "run"],
                                     cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=fd,
                                     stderr=fd, start_new_session=True)
        finally:
            os.close(fd)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if child.poll() is not None:
            raise ServiceError("Supervisor exited during startup. Private state/log preserved; no automatic retry.")
        if _alive(config) and primitives.state_locked(config.state_path):
            return "Terminal-detached supervisor started (bot instance lock held). Logout policy may stop it; no reboot or sleep durability."
        time.sleep(0.2)
    raise ServiceError("Supervisor startup is uncertain; inspect status before retrying. A process may still be running.")


def stop(config):
    if units_installed():
        if not systemd_user_available():
            raise ServiceError("User manager unavailable; services were not stopped.")
        names = [BOT_UNIT]
        if _owned_unit(BACKEND_UNIT):
            names.append(BACKEND_UNIT)
        _systemctl("stop", *names)
        return "User services stopped. Configuration/auth/state preserved."
    record = _record(config)
    primitives = _primitives()
    if not primitives.process_matches(record, PROJECT_ROOT / "omnirush.py"):
        if primitives.state_locked(config.state_path):
            raise ServiceError("Another launcher owns the bot; cannot verify its process identity, so no signal was sent.")
        return "Already stopped; stale metadata preserved."
    # Identity includes start ticks and exact launcher path, not just a reusable PID.
    os.kill(record["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        if not primitives.process_matches(record, PROJECT_ROOT / "omnirush.py"):
            return "Supervisor stopped. Configuration/auth/state preserved."
        time.sleep(0.2)
    raise ServiceError("Stop timed out; status is uncertain. No unverified process was killed.")
