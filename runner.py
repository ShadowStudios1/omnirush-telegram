#!/usr/bin/env python3
"""Start or inspect a detached bridge; never stop it or reset its state."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
import uuid

from telegram_bridge.config import ConfigError, PROJECT_ROOT, load_config
from telegram_bridge.state import StateError, StateStore


STARTUP_TIMEOUT = 30.0
POLL_INTERVAL = 0.2


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally echoes unknown arguments, which could be secrets.
        self.print_usage(sys.stderr)
        self.exit(2, "Use exactly one of --start or --status; no other arguments are accepted.\n")


def process_start_ticks(pid: int) -> str | None:
    """Read Linux start ticks, excluding dead/zombie processes and invalid PIDs."""
    if type(pid) is not int or pid <= 0:
        return None
    try:
        # comm is parenthesized and can itself contain spaces or parentheses.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        ticks = fields[19]  # Field 22; fields[0] is field 3 (state).
        if fields[0] in {"Z", "X", "x"} or not ticks.isascii() or not ticks.isdigit():
            return None
        return ticks
    except (OSError, ValueError, IndexError):
        return None


def process_matches(record: dict, bot_path: Path) -> bool:
    """Check both PID lifetime and the exact script argument; never expose argv."""
    if not isinstance(record, dict):
        return False
    pid, ticks = record.get("pid"), record.get("start_ticks")
    if type(pid) is not int or pid <= 0 or not isinstance(ticks, str):
        return False
    if process_start_ticks(pid) != ticks:
        return False
    try:
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return (os.fsencode(bot_path.absolute()) in arguments
                and process_start_ticks(pid) == ticks)
    except (OSError, ValueError):
        return False


@contextmanager
def _parent_fd(path: Path, *, create: bool = False):
    """Walk without following symlinks, keeping private operations dirfd-relative."""
    path = Path(path).absolute()
    if ".." in path.parts or path.is_relative_to(PROJECT_ROOT):
        raise StateError("Unsafe runtime location.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for name in path.parent.parts[1:]:
            if create:
                try:
                    os.mkdir(name, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            next_fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                              | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        info = os.fstat(fd)
        if info.st_uid != os.getuid():
            raise StateError("Runtime directory must belong to this user.")
        if create:
            os.fchmod(fd, 0o700)
        elif stat.S_IMODE(info.st_mode) != 0o700:
            raise StateError("Runtime directory must be private.")
        yield fd
    finally:
        os.close(fd)


def _check_file(fd: int, *, secure: bool = False) -> None:
    info = os.fstat(fd)
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
            or info.st_nlink != 1):
        raise StateError("Runtime file must be a private regular file.")
    if secure:
        os.fchmod(fd, 0o600)
    elif stat.S_IMODE(info.st_mode) != 0o600:
        raise StateError("Runtime file must have private permissions.")


def state_locked(state_path: Path) -> bool:
    """Probe StateStore's existing .lock, without constructing/opening its DB.

    StateStore.__init__ also initializes SQLite tables, so it must not be used
    for this read-only probe. Use precisely its flock filename and protocol.
    """
    lock_path = state_path.with_name(state_path.name + ".lock")
    try:
        with _parent_fd(lock_path) as parent:
            fd = os.open(lock_path.name, os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            try:
                _check_file(fd)
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(fd, fcntl.LOCK_UN)
                return False
            finally:
                os.close(fd)
    except FileNotFoundError:
        return False
    except StateError:
        raise
    except OSError:
        raise StateError("Cannot inspect the bridge instance lock.") from None


def _read_record(path: Path) -> dict:
    try:
        with _parent_fd(path) as parent:
            fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW
                         | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                _check_file(stream.fileno())
                if os.fstat(stream.fileno()).st_size > 4096:
                    raise StateError("Invalid process metadata.")
                record = json.load(stream)
    except FileNotFoundError:
        return {}
    if (not isinstance(record, dict) or set(record) != {"pid", "start_ticks"}
            or type(record["pid"]) is not int or record["pid"] <= 0
            or not isinstance(record["start_ticks"], str)
            or not record["start_ticks"].isascii()
            or not record["start_ticks"].isdigit()):
        raise StateError("Invalid process metadata; it was preserved.")
    return record


def _save_record(parent: int, name: str, record: dict) -> None:
    """Atomic private writer. Failed temporary files are left private, not deleted."""
    temporary = ".process-" + uuid.uuid4().hex + ".new"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                 | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(record, stream, allow_nan=False)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    # Reject an unexpected symlink destination, even though rename wouldn't
    # follow it. The private directory and launcher flock protect normal writes.
    try:
        info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1):
            raise StateError("Unsafe process metadata destination.")
    os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    os.fsync(parent)


def _report(state: str, record: dict, log_path: Path) -> None:
    # No configuration, raw command line, credentials, or log contents here.
    print(f"PID: {record.get('pid', 'unknown')}\nState: {state}\nLog: {log_path}")


def _status(state_path: Path, record_path: Path, log_path: Path) -> int:
    record = _read_record(record_path)
    alive = process_matches(record, PROJECT_ROOT / "bot.py")
    locked = state_locked(state_path)
    if alive:
        _report("ready" if locked else "initializing / not ready", record, log_path)
    elif locked:
        _report("already running (instance locked; recorded identity unverified)", {}, log_path)
    else:
        _report("stopped", {}, log_path)
    return 0


def _start(state_path: Path, record_path: Path, log_path: Path) -> int:
    with _parent_fd(record_path, create=True) as parent:
        launcher_fd = os.open("launcher.lock", os.O_RDWR | os.O_CREAT
                              | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                              0o600, dir_fd=parent)
        try:
            _check_file(launcher_fd, secure=True)
            try:
                fcntl.flock(launcher_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _report("launch already in progress; no duplicate started", {}, log_path)
                return 0
            record = _read_record(record_path)
            if state_locked(state_path):
                verified = record if process_matches(record, PROJECT_ROOT / "bot.py") else {}
                _report("already running (instance locked)", verified, log_path)
                return 0
            if process_matches(record, PROJECT_ROOT / "bot.py"):
                _report("initializing / not ready; no duplicate started", record, log_path)
                return 0
            if record:
                _save_record(parent, "process.previous-" + uuid.uuid4().hex + ".json", record)
            log_fd = os.open(log_path.name, os.O_WRONLY | os.O_CREAT | os.O_APPEND
                             | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                             0o600, dir_fd=parent)
            try:
                _check_file(log_fd, secure=True)
                environment = os.environ.copy()
                environment["PYTHONDONTWRITEBYTECODE"] = "1"
                child = subprocess.Popen(
                    [sys.executable, "-u", str(PROJECT_ROOT / "bot.py")],
                    cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL,
                    stdout=log_fd, stderr=log_fd, close_fds=True,
                    start_new_session=True, env=environment,
                )
            finally:
                os.close(log_fd)
            ticks = process_start_ticks(child.pid)
            if ticks is None:
                _report("startup identity unavailable; not retried", {}, log_path)
                return 1
            record = {"pid": child.pid, "start_ticks": ticks}
            _save_record(parent, record_path.name, record)
            deadline = time.monotonic() + STARTUP_TIMEOUT
            while True:
                if child.poll() is not None or not process_matches(record, PROJECT_ROOT / "bot.py"):
                    _report("startup failed; state and logs preserved", record, log_path)
                    return 1
                if state_locked(state_path) and process_matches(record, PROJECT_ROOT / "bot.py"):
                    _report("ready", record, log_path)
                    return 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _report("initializing / not ready; process left running", record, log_path)
                    return 1
                time.sleep(min(POLL_INTERVAL, remaining))
        finally:
            os.close(launcher_fd)


def main(argv=None) -> int:
    parser = _ArgumentParser(description=__doc__, allow_abbrev=False)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--start", action="store_true", help="Start once, detached from this terminal")
    actions.add_argument("--status", action="store_true", help="Inspect private process identity and instance lock")
    args = parser.parse_args(argv)
    try:
        # Only the validated state location is used; no credential is forwarded.
        state_path = load_config().state_path
        record_path = state_path.parent / "process.json"
        log_path = state_path.parent / "bridge.log"
        if state_path.name in {"process.json", "bridge.log", "launcher.lock"}:
            raise StateError("Runtime filenames conflict.")
        # Refuse a symlink/nonregular database without opening or modifying it.
        with _parent_fd(state_path, create=args.start) as parent:
            try:
                info = os.stat(state_path.name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                        or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                    raise StateError("Unsafe state database.")
        if args.start:
            return _start(state_path, record_path, log_path)
        return _status(state_path, record_path, log_path)
    except FileNotFoundError:
        if args.status:
            _report("stopped", {}, log_path)
            return 0
        print("Launcher could not start; private state and logs were preserved.")
        return 1
    except KeyboardInterrupt:
        print("Launcher interrupted; no process was stopped or automatically retried.")
        return 1
    except (ConfigError, StateError, OSError, ValueError, TypeError):
        print("Launcher could not verify private runtime state; no automatic retry. Check local permissions and configuration.")
        return 1
    except Exception:
        print("Launcher encountered an internal error; no automatic retry. State and logs were preserved.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
