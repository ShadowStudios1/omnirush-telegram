"""Private local configuration; credentials never belong in the project."""

from dataclasses import dataclass, field
import json
import math
import os
from pathlib import Path
import stat

from .agent import AgentClient, AgentError, DEFAULT_EXECUTABLE


CONFIG_PATH = Path.home() / ".config/omnirush-telegram-portable/config.json"
STATE_PATH = Path.home() / ".local/state/omnirush-telegram-portable/state.sqlite3"
PRIVATE_DATA = Path.home() / ".local/share/omnirush-telegram-portable"
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class Config:
    token: str = field(repr=False)
    owner_id: int
    project: Path
    roots: tuple[Path, ...]
    executable: str = DEFAULT_EXECUTABLE
    server_url: str = "auto"
    server_auth: str | None = field(default=None, repr=False)
    model: dict | None = None
    agent: str | None = None
    state_path: Path = STATE_PATH
    monitor_interval: float = 5.0
    backend_mode: str = "desktop"
    permission_mode: str = "ask"

    def __post_init__(self) -> None:
        try:
            if (not isinstance(self.token, str) or not self.token
                    or type(self.owner_id) is not int or self.owner_id <= 0):
                raise ValueError
            if (isinstance(self.monitor_interval, bool)
                    or not isinstance(self.monitor_interval, (int, float))
                    or not math.isfinite(self.monitor_interval)
                    or not 2 <= self.monitor_interval <= 60):
                raise ValueError
            AgentClient(self.executable, self.server_url, self.model, self.agent,
                        self.permission_mode, self.backend_mode, self.server_auth)
            if not self.roots or any(not isinstance(root, Path) for root in self.roots):
                raise ValueError
            roots = tuple(_project_directory(root) for root in self.roots)
            if Path("/") in roots:
                raise ConfigError("Use designated project roots, not the entire filesystem.")
            project = _project_directory(self.project)
            state = _private_location(self.state_path)
            if not any(project.is_relative_to(root) for root in roots):
                raise ConfigError("Project is outside the configured project roots.")
            if any(private.is_relative_to(root) for root in roots
                   for private in (state, CONFIG_PATH.absolute(), STATE_PATH.absolute())):
                raise ConfigError("Project roots must not include private bridge configuration or state.")
            if any(root.is_relative_to(private) or private.is_relative_to(root)
                   for root in roots for private in (CONFIG_PATH.parent.absolute(),
                       STATE_PATH.parent.absolute(), PRIVATE_DATA.absolute())):
                raise ConfigError("Project roots must not overlap private bridge or native authentication directories.")
            object.__setattr__(self, "roots", roots)
            object.__setattr__(self, "project", project)
            object.__setattr__(self, "state_path", state)
        except ConfigError:
            raise
        except (ValueError, TypeError, AttributeError, OSError, RuntimeError, AgentError):
            raise ConfigError("Invalid private configuration; rerun setup.") from None

    def project_path(self, value: str) -> Path:
        try:
            path = _project_directory(Path(value))
        except (OSError, ValueError, RuntimeError):
            raise ConfigError("Project directory does not exist.") from None
        if not path.is_dir() or not any(path.is_relative_to(root) for root in self.roots):
            raise ConfigError("Project is outside the configured project roots.")
        return path


def _project_directory(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ConfigError("Project paths must not contain symlinks.")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ConfigError("Project directory does not exist.")
    return path


def _private_location(path: Path) -> Path:
    path = path.expanduser().absolute()
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError, ValueError):
        raise ConfigError("Invalid private configuration path.") from None
    if resolved.is_relative_to(PROJECT_ROOT):
        raise ConfigError("Keep credentials and runtime state outside the project.")
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ConfigError("Private configuration paths must not contain symlinks.")
    return path


def save_private(data: dict, path: Path = CONFIG_PATH) -> None:
    path = _private_location(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    # Replace atomically; never leave a half-written token file behind.
    temporary = path.with_name(path.name + ".new")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        # Remove only a temporary file that we created, never a preexisting one.
        if "fd" in locals():
            try:
                temporary.unlink()
            except OSError:
                pass
        raise ConfigError("Could not save private configuration; check local permissions.") from None


def load_config(path: Path = CONFIG_PATH) -> Config:
    path = _private_location(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ConfigError("Private configuration must be owned by your Linux user.")
            if info.st_mode & 0o077:
                raise ConfigError("Private configuration needs chmod 600.")
            if info.st_size > 65536:
                raise ConfigError("Private configuration is too large.")
            data = json.load(stream)
    except FileNotFoundError:
        raise ConfigError("Configuration missing. Run ./setup.sh in a local terminal.") from None
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("Cannot read private configuration.") from None
    try:
        token = data["telegram_token"]
        owner = data["owner_id"]
        if not isinstance(token, str) or not token or type(owner) is not int or owner <= 0:
            raise ValueError
        if not isinstance(data["project_roots"], list) or any(not isinstance(p, str) for p in data["project_roots"]):
            raise ValueError
        roots = tuple(_project_directory(Path(p)) for p in data["project_roots"])
        if not roots or any(not root.is_dir() for root in roots):
            raise ValueError
        # Broad filesystem grants must be deliberately scoped, not / by accident.
        if any(root == Path("/") for root in roots):
            raise ConfigError("Use designated project roots, not the entire filesystem.")
        state = _private_location(Path(data.get("state_path", STATE_PATH)))
        raw_interval = data.get("monitor_interval", 5)
        if isinstance(raw_interval, bool) or not isinstance(raw_interval, (int, float)):
            raise ValueError
        interval = float(raw_interval)
        if not 2 <= interval <= 60:
            raise ValueError
        config = Config(
            token=token, owner_id=owner,
            project=_project_directory(Path(data["default_project"])),
            roots=roots, executable=data.get("executable", DEFAULT_EXECUTABLE),
            server_url=data.get("server_url", "auto"), server_auth=data.get("server_auth"),
            model=data.get("model"),
            agent=data.get("agent"), state_path=state, monitor_interval=interval,
            backend_mode=data.get("backend_mode", "desktop"),
            permission_mode=data.get("permission_mode", "ask"),
        )
        config.project_path(str(config.project))
        if any(private_path.is_relative_to(root) for root in roots for private_path in (path, state)):
            raise ConfigError("Project roots must not include private bridge configuration or state.")
        return config
    except ConfigError:
        raise
    except Exception:
        raise ConfigError("Invalid private configuration; rerun setup.") from None


__all__ = ["Config", "ConfigError", "CONFIG_PATH", "STATE_PATH", "PROJECT_ROOT", "load_config", "save_private"]
