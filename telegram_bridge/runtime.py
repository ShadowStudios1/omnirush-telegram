"""Isolated native CLI environment and loopback-only service entry point."""

from __future__ import annotations

import argparse
import json
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
        if not key.startswith(("OPENCODE_", "OMNIRUSH_", "ENGINE_", "XDG_"))
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


def authenticated_runtime_environment() -> dict[str, str]:
    """Return the isolated runtime environment plus the signed-in account."""
    from .account import authenticated_environment
    environment = runtime_environment()
    xdg = {key: value for key, value in environment.items() if key.startswith("XDG_")}
    authenticated = authenticated_environment(environment)
    authenticated.update(xdg)
    # The official npm launcher normally creates this provider wiring before
    # spawning its Bun engine. The bridge invokes the engine's API directly,
    # so create the same narrow config here and disable the engine's unrelated
    # public-provider discovery. A transient catalog failure is left to the
    # setup/model-selection path to report; it must not make auth credentials
    # disappear from an otherwise valid environment.
    try:
        from .account import model_catalog
        catalog = model_catalog()
        config_path = _write_engine_config(authenticated["OMNIRUSH_GATEWAY_URL"], catalog)
        authenticated["OPENCODE_CONFIG"] = str(config_path)
        authenticated["OPENCODE_DISABLE_MODELS_FETCH"] = "1"
    except Exception:
        # AccountError deliberately has a safe message, but this environment
        # helper remains usable for login/diagnostics when the gateway is down.
        pass
    return authenticated


def _write_engine_config(gateway_url: str, catalog: list[dict]) -> Path:
    """Write the official CLI's provider config atomically and privately."""
    if not isinstance(gateway_url, str) or not isinstance(catalog, list):
        raise AgentError("The OmniRush model catalog is invalid.")
    models = {}
    for entry in catalog:
        if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
            continue
        limits = entry.get("limits", {}) if isinstance(entry.get("limits"), dict) else {}
        capabilities = entry.get("capabilities", {}) if isinstance(entry.get("capabilities"), dict) else {}
        efforts = entry.get("reasoning_levels", []) if isinstance(entry.get("reasoning_levels"), list) else []
        models[entry["id"]] = {
            "name": entry.get("display_name", entry["id"]),
            "reasoning": capabilities.get("reasoning") is True,
            "tool_call": capabilities.get("tool_call") is True,
            "structured_output": True,
            "temperature": True,
            "limit": {"context": limits.get("context", 400_000), "output": limits.get("output", 128_000)},
            "variants": {effort: {"reasoningEffort": effort} for effort in efforts if isinstance(effort, str)},
        }
    if not models:
        raise AgentError("OmniRush account gateway reported no usable models.")
    default = next((entry["id"] for entry in catalog if isinstance(entry, dict) and entry.get("default") is True and entry.get("id") in models), next(iter(models)))
    payload = {
        "$schema": "https://opencode.ai/config.json",
        "model": "omnirush/" + default,
        "enabled_providers": ["omnirush"],
        "permission": "allow",
        "provider": {"omnirush": {"npm": "@ai-sdk/openai", "env": ["OMNIRUSH_ACCESS_TOKEN"],
                                    "options": {"baseURL": gateway_url.rstrip("/")}, "models": models}},
    }
    config_dir = private_directory(NATIVE_HOME / "config" / "opencode")
    path = config_dir / "omnirush-config.json"
    temporary = path.with_name(path.name + ".new")
    if temporary.exists() or temporary.is_symlink():
        raise AgentError("The private OmniRush engine config has an unsafe temporary file.")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            os.chmod(temporary, 0o600)
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except (OSError, TypeError, ValueError):
        try:
            temporary.unlink()
        except OSError:
            pass
        raise AgentError("Could not save the private OmniRush engine config.") from None
    return path


def serve() -> None:
    from .config import ConfigError, load_config
    config = load_config()
    if config.backend_mode != "headless" or config.server_url != "managed":
        raise ConfigError("Native service requires a headless managed configuration.")
    executable = Path(_executable(config.executable))
    if not executable.is_file() or not os.access(executable, os.X_OK):
        raise AgentError("Configured native sidecar is missing or not executable.")
    environment = authenticated_runtime_environment()
    os.umask(0o077)
    # No configurable host or port: the native service owns discovery and auth.
    os.execve(str(executable), [str(executable), "serve", "--service", "--hostname", "127.0.0.1"], environment)


def main(argv: list[str] | None = None) -> int:
    from .account import AccountError
    parser = argparse.ArgumentParser(description="Portable OmniRush native runtime")
    parser.add_argument("command", choices=("serve",))
    parser.parse_args(argv)
    try:
        serve()
    except AccountError as error:
        print(str(error), file=sys.stderr)
        return 1
    except (AgentError, OSError):
        print("Native service could not start; check setup and private directory permissions.", file=sys.stderr)
        return 1
    except RuntimeError:
        # ConfigError is intentionally not imported at module load (no cycles).
        print("Native service configuration is invalid; rerun setup.", file=sys.stderr)
        return 1
    return 0


__all__ = ["discover_executable", "runtime_environment", "authenticated_runtime_environment", "serve", "main"]

if __name__ == "__main__":
    raise SystemExit(main())
