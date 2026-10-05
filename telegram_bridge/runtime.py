"""Isolated native CLI environment and loopback-only service entry point."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import stat
import sys

from .agent import AgentError, _executable, discover_executable


APP_DATA = Path.home() / ".local/share/omnirush-telegram-portable"
NATIVE_HOME = APP_DATA / "native"


def private_directory(path: Path) -> Path:
    """Create only a user-owned private directory with no symlink components."""
    path = path.expanduser().absolute()
    from .config import PROJECT_ROOT
    if path.resolve().is_relative_to(PROJECT_ROOT):
        raise AgentError("Native runtime data must be outside the project.")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise AgentError("Native runtime paths must not contain symlinks.")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise AgentError("Native runtime directory must belong to your Linux user.")
        path.chmod(0o700)
    except OSError:
        raise AgentError("Cannot create a private native runtime directory.") from None
    return path


def runtime_environment() -> dict[str, str]:
    """Use separate XDG homes for serve, login and managed API invocations.

    Inherited native endpoint/config overrides are removed. No authentication
    files or credentials are read or copied from the desktop or original bot.
    """
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith(("OPENCODE_", "OMNIRUSH_", "XDG_"))
    }
    private_directory(APP_DATA)
    private_directory(NATIVE_HOME)
    for key, directory in (
        ("XDG_CONFIG_HOME", "config"), ("XDG_DATA_HOME", "data"),
        ("XDG_STATE_HOME", "state"), ("XDG_CACHE_HOME", "cache"),
        ("XDG_RUNTIME_DIR", "run"),
    ):
        environment[key] = str(private_directory(NATIVE_HOME / directory))
    return environment


def serve() -> None:
    from .config import ConfigError, load_config
    config = load_config()
    if config.backend_mode != "headless" or config.server_url != "managed":
        raise ConfigError("Native service requires a headless managed configuration.")
    executable = Path(_executable(config.executable))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise AgentError("Configured native sidecar is missing or not executable.")
    environment = runtime_environment()
    os.umask(0o077)
    # No configurable host or port: the native service owns discovery and auth.
    os.execve(str(executable), [str(executable), "serve", "--service", "--hostname", "127.0.0.1"], environment)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Portable OmniRush native runtime")
    parser.add_argument("command", choices=("serve",))
    parser.parse_args(argv)
    try:
        serve()
    except (AgentError, OSError):
        print("Native service could not start; check setup and private directory permissions.", file=sys.stderr)
        return 1
    except RuntimeError:
        # ConfigError is intentionally not imported at module load (no cycles).
        print("Native service configuration is invalid; rerun setup.", file=sys.stderr)
        return 1
    return 0


__all__ = ["discover_executable", "runtime_environment", "serve", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
