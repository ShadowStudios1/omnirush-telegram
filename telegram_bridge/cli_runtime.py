"""Install the official npm OmniRush CLI without invoking npm.

The Telegram bridge is deliberately stdlib-only.  This module therefore
downloads the two npm tarballs that the official package would install, checks
their pinned npm integrity values, and extracts only regular files into a
private, versioned directory.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import stat
import tarfile
import tempfile
from urllib.parse import urlsplit
import urllib.error
import urllib.request


VERSION = "2.1.2"
APP_DATA = Path.home() / ".local/share/omnirush-telegram-portable"
INSTALL_ROOT = APP_DATA / "official-cli"
AUTH_DIR = INSTALL_ROOT
OFFICIAL_AUTH_PATH = AUTH_DIR / "auth.json"
LAUNCHER_PATH = Path.home() / ".local/bin/omnirush"
REGISTRY = "https://registry.npmjs.org"
TOP_LEVEL_PACKAGE = "omnirush"
MAX_TOP_LEVEL_BYTES = 25 * 1024 * 1024
MAX_PLATFORM_BYTES = 300 * 1024 * 1024
MAX_UNPACKED_BYTES = 800 * 1024 * 1024
MAX_FILES = 50_000
CHUNK_BYTES = 1024 * 1024

# These are npm's exact integrity pins for the release, not hashes calculated
# after a download.  The arm64 value is included so architecture selection
# cannot silently turn into a metadata/latest lookup.
PINNED_PACKAGES = {
    "omnirush": {
        "version": VERSION,
        "integrity": "sha512-sM7t/TOaPvRYjjV53Lc+BiTgIjAKztjiXyjPfjA60h5YO5faxp/qHTiqpOjSJnz/vwJEqfbdOGtvl07wq+tX8Q==",
        "max_bytes": MAX_TOP_LEVEL_BYTES,
    },
    "@omnirush-ai/cli-linux-x64": {
        "version": VERSION,
        "integrity": "sha512-1zMi3Wd/PUApdTboibpSKNxiEunO5kiAtPkP1Kn7PZLQ9HlhbM2FcbfnvRyt+ORlsFnNHoMp6X6hhl277tYC8A==",
        "max_bytes": MAX_PLATFORM_BYTES,
    },
    "@omnirush-ai/cli-linux-arm64": {
        "version": VERSION,
        "integrity": "sha512-M2koEmb5jl2UHbZleNt9JDUp46lLyhggLN9PDcr3W2d7H7fQgDoo0C1OkHUOCtFoh+GEf4BqQzKI3RrxHlCUXg==",
        "max_bytes": MAX_PLATFORM_BYTES,
    },
}


class CliRuntimeError(RuntimeError):
    """Safe installer failure; never includes response bodies or credentials."""


@dataclass(frozen=True)
class OfficialCliRuntime:
    root: Path
    engine: Path
    bun: Path
    auth_dir: Path

    def __iter__(self):
        # Convenient for callers that only need the two executable paths.
        yield self.engine
        yield self.bun


def _architecture() -> tuple[str, str]:
    if platform.system() != "Linux" or platform.libc_ver()[0].lower() != "glibc":
        raise CliRuntimeError("The official OmniRush CLI requires Linux with glibc.")
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64", "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64", "aarch64"
    raise CliRuntimeError("The official OmniRush CLI supports only Linux x64 and arm64.")


def _package_name(architecture: str) -> str:
    return "@omnirush-ai/cli-linux-" + architecture


def _tarball_url(package: str) -> str:
    # npm's scoped URL spelling is stable and avoids a metadata request.
    encoded = package.replace("/", "%2f")
    return f"{REGISTRY}/{encoded}/-/{package.rsplit('/', 1)[-1]}-{VERSION}.tgz"


def _safe_registry_url(url: str) -> str:
    try:
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname != "registry.npmjs.org"
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment
                or any(ord(char) < 33 for char in url)):
            raise ValueError
    except (TypeError, ValueError):
        raise CliRuntimeError("Refusing a non-official npm package URL.") from None
    return url


class _RegistryRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_registry_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str):
    _safe_registry_url(url)
    request = urllib.request.Request(url, headers={
        "Accept": "application/octet-stream",
        "Accept-Encoding": "identity",
        "User-Agent": f"omnirush-telegram-portable/{VERSION}",
    })
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _RegistryRedirect())
        response = opener.open(request, timeout=60)
        _safe_registry_url(response.geturl())
        status = int(response.getcode())
        if status != 200:
            response.close()
            raise CliRuntimeError("Official npm package download failed.")
        return response
    except CliRuntimeError:
        raise
    except (OSError, ValueError, urllib.error.URLError):
        raise CliRuntimeError("Cannot reach the official npm package source.") from None


def _integrity_bytes(integrity: str) -> bytes:
    try:
        algorithm, encoded = integrity.split("-", 1)
        if algorithm != "sha512":
            raise ValueError
        return base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise CliRuntimeError("The pinned npm integrity value is invalid.") from None


def _download(package: str, destination: Path, progress=None) -> int:
    spec = PINNED_PACKAGES[package]
    limit = spec["max_bytes"]
    expected = _integrity_bytes(spec["integrity"])
    count = 0
    digest = hashlib.sha512()
    try:
        with _open(_tarball_url(package)) as response:
            length = response.headers.get("Content-Length")
            if length is not None and (not str(length).isdecimal() or int(length) > limit):
                raise CliRuntimeError("Official npm package exceeds its safety limit.")
            with destination.open("xb") as output:
                while True:
                    block = response.read(min(CHUNK_BYTES, limit - count + 1))
                    if not block:
                        break
                    count += len(block)
                    if count > limit:
                        raise CliRuntimeError("Official npm package exceeds its safety limit.")
                    digest.update(block)
                    output.write(block)
                    if progress is not None:
                        progress(package, count, limit)
                output.flush()
                os.fsync(output.fileno())
    except CliRuntimeError:
        raise
    except (OSError, ValueError):
        raise CliRuntimeError("Could not save the official npm package safely.") from None
    if count == 0 or digest.digest() != expected:
        raise CliRuntimeError("Official npm package integrity verification failed; nothing was installed.")
    return count


def _private_tree(path: Path, *, create: bool = False) -> Path:
    path = Path(path).expanduser().absolute()
    if ".." in path.parts:
        raise CliRuntimeError("Official CLI paths are unsafe.")
    try:
        if create:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        for component in (path, *path.parents):
            if component.is_symlink():
                raise CliRuntimeError("Official CLI paths must not contain symlinks.")
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
            raise CliRuntimeError("Official CLI directories must be private user-owned directories.")
        path.chmod(0o700)
    except CliRuntimeError:
        raise
    except OSError:
        raise CliRuntimeError("Cannot create the private official CLI directory.") from None
    return path


def _safe_member(name: str) -> tuple[str, ...]:
    if (not isinstance(name, str) or not name or name.startswith("/")
            or "\\" in name or "\x00" in name or ":" in name):
        raise CliRuntimeError("Unsafe npm package archive member; nothing was installed.")
    parts = PurePosixPath(name).parts
    if not parts or parts[0] != "package" or any(part in ("", ".", "..") for part in parts):
        raise CliRuntimeError("Unsafe npm package archive member; nothing was installed.")
    return parts[1:]


def _extract(archive: Path, destination: Path, package: str) -> None:
    files = 0
    unpacked = 0
    try:
        with archive.open("rb") as compressed, gzip.GzipFile(fileobj=compressed, mode="rb") as stream:
            with tarfile.open(fileobj=stream, mode="r|") as members:
                for member in members:
                    files += 1
                    if files > MAX_FILES or member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                        raise CliRuntimeError("Unsafe npm package archive member; nothing was installed.")
                    relative = _safe_member(member.name)
                    if not relative:
                        continue
                    target = destination.joinpath(*relative)
                    if member.isdir():
                        target.mkdir(mode=0o700, parents=True, exist_ok=True)
                        continue
                    if member.size < 0 or member.size > MAX_UNPACKED_BYTES - unpacked:
                        raise CliRuntimeError("Official npm package expands beyond the safety limit.")
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    if any(part.is_symlink() for part in (target.parent, *target.parent.parents)):
                        raise CliRuntimeError("Unsafe npm package archive member; nothing was installed.")
                    source = members.extractfile(member)
                    if source is None:
                        raise CliRuntimeError("Official npm package archive is unreadable.")
                    mode = 0o700 if member.mode & 0o111 else 0o600
                    with source, target.open("xb") as output:
                        remaining = member.size
                        while remaining:
                            block = source.read(min(CHUNK_BYTES, remaining))
                            if not block:
                                raise CliRuntimeError("Official npm package archive is truncated.")
                            output.write(block)
                            remaining -= len(block)
                            unpacked += len(block)
                            if unpacked > MAX_UNPACKED_BYTES:
                                raise CliRuntimeError("Official npm package expands beyond the safety limit.")
                        output.flush()
                        os.fsync(output.fileno())
                    os.chmod(target, mode)
    except CliRuntimeError:
        raise
    except (OSError, EOFError, gzip.BadGzipFile, tarfile.TarError):
        raise CliRuntimeError("Official npm package archive is unreadable; nothing was installed.") from None


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK_BYTES):
            digest.update(block)
    return base64.b64encode(digest.digest()).decode("ascii")


def _private_file(path: Path, *, executable: bool = False) -> None:
    try:
        info = path.stat(follow_symlinks=False)
        modes = (0o700,) if executable else (0o600, 0o700)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) not in modes):
            raise CliRuntimeError("Installed official CLI files must be private regular files.")
    except CliRuntimeError:
        raise
    except OSError:
        raise CliRuntimeError("Installed official CLI files are incomplete or unreadable.") from None


def _runtime_at(root: Path, architecture: str, manifest: dict) -> OfficialCliRuntime:
    top = root / "node_modules" / "omnirush"
    platform_dir = root / "node_modules" / "@omnirush-ai" / f"cli-linux-{architecture}"
    engine = platform_dir / "bin" / "omnirush"
    bun = platform_dir / "bin" / "bun"
    for directory in (root, root / "node_modules", top, platform_dir, engine.parent):
        _private_tree(directory)
    _private_file(engine, executable=True)
    _private_file(bun, executable=True)
    _private_file(top / "package.json")
    _private_file(top / "src" / "bin.js")
    _private_file(platform_dir / "package.json")
    try:
        top_manifest = json.loads((top / "package.json").read_text(encoding="utf-8"))
        platform_manifest = json.loads((platform_dir / "package.json").read_text(encoding="utf-8"))
        if (top_manifest.get("name") != TOP_LEVEL_PACKAGE or top_manifest.get("version") != VERSION
                or platform_manifest.get("name") != f"@omnirush-ai/cli-linux-{architecture}"
                or platform_manifest.get("version") != VERSION):
            raise ValueError
        if (manifest.get("version") != VERSION or manifest.get("architecture") != architecture
                or manifest.get("engine_sha512") != _sha512(engine)
                or manifest.get("bun_sha512") != _sha512(bun)):
            raise ValueError
    except (OSError, UnicodeError, ValueError, TypeError, AttributeError, RecursionError):
        raise CliRuntimeError("Installed official CLI failed integrity verification; it was not overwritten.") from None
    return OfficialCliRuntime(root, engine, bun, AUTH_DIR)


def installed_cli() -> OfficialCliRuntime | None:
    architecture, _native = _architecture()
    final = INSTALL_ROOT / VERSION
    if not final.exists() and not final.is_symlink():
        return None
    manifest_path = final / "manifest.json"
    try:
        _private_file(manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError, RecursionError):
        raise CliRuntimeError("Installed official CLI failed integrity verification; it was not overwritten.") from None
    return _runtime_at(final, architecture, manifest)


def ensure_official_cli(progress=None) -> OfficialCliRuntime:
    """Install or verify exactly version 2.1.2 and return its engine and Bun."""
    existing = installed_cli()
    if existing is not None:
        return existing
    architecture, _native = _architecture()
    root = _private_tree(INSTALL_ROOT, create=True)
    lock_path = root / ".install.lock"
    try:
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            existing = installed_cli()
            if existing is not None:
                return existing
            temporary = Path(tempfile.mkdtemp(prefix=".install-", dir=root))
            try:
                _private_tree(temporary)
                top_archive = temporary / "top.tgz"
                platform_archive = temporary / "platform.tgz"
                _download(TOP_LEVEL_PACKAGE, top_archive, progress)
                platform_name = _package_name(architecture)
                _download(platform_name, platform_archive, progress)
                top = temporary / "node_modules" / "omnirush"
                platform_dir = temporary / "node_modules" / "@omnirush-ai" / f"cli-linux-{architecture}"
                _extract(top_archive, top, TOP_LEVEL_PACKAGE)
                _extract(platform_archive, platform_dir, platform_name)
                manifest = {
                    "version": VERSION,
                    "architecture": architecture,
                    "packages": {TOP_LEVEL_PACKAGE: PINNED_PACKAGES[TOP_LEVEL_PACKAGE]["integrity"],
                                 platform_name: PINNED_PACKAGES[platform_name]["integrity"]},
                    "engine_sha512": _sha512(platform_dir / "bin" / "omnirush"),
                    "bun_sha512": _sha512(platform_dir / "bin" / "bun"),
                }
                manifest_path = temporary / "manifest.json"
                manifest_path.write_text(json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
                os.chmod(manifest_path, 0o600)
                # Validate before publishing the directory.
                _runtime_at(temporary, architecture, manifest)
                final = root / VERSION
                if final.exists() or final.is_symlink():
                    raise CliRuntimeError("An existing official CLI installation was not overwritten.")
                os.replace(temporary, final)
                temporary = None
                return _runtime_at(final, architecture, manifest)
            finally:
                if temporary is not None:
                    import shutil
                    shutil.rmtree(temporary, ignore_errors=True)
        finally:
            os.close(fd)
    except CliRuntimeError:
        raise
    except (OSError, ValueError, TypeError):
        raise CliRuntimeError("Could not install the private official CLI; previous versions were preserved.") from None


def _shell_quote(value: Path | str) -> str:
    return "'" + str(value).replace("'", "'\\''") + "'"


def launcher_contents(runtime: OfficialCliRuntime) -> str:
    """Return the real official launcher, retaining all command-line arguments."""
    return ("#!/bin/sh\n"
            "# Managed by omnirush-telegram-portable; official OmniRush CLI.\n"
            "set -eu\n"
            f"OMNIRUSH_DIR={_shell_quote(runtime.auth_dir)}\n"
            "export OMNIRUSH_DIR\n"
            f"exec {_shell_quote(runtime.bun)} {_shell_quote(runtime.root / 'node_modules/omnirush/src/bin.js')} \"$@\"\n")


def install_launcher(runtime: OfficialCliRuntime, destination: Path = LAUNCHER_PATH) -> Path:
    """Install a private atomic launcher, refusing unrelated files and symlinks."""
    destination = Path(destination).expanduser().absolute()
    parent = _private_tree(destination.parent, create=True)
    if destination.is_symlink():
        raise CliRuntimeError("Refusing to replace an existing symlink at the OmniRush launcher path.")
    if destination.exists():
        try:
            if not destination.is_file() or not destination.read_text(encoding="utf-8").startswith("#!/bin/sh\n# Managed by omnirush-telegram-portable;"):
                raise CliRuntimeError("An unrelated file exists at the OmniRush launcher path; it was preserved.")
        except UnicodeError:
            raise CliRuntimeError("An unrelated file exists at the OmniRush launcher path; it was preserved.") from None
    temporary = parent / (destination.name + ".new")
    if temporary.exists() or temporary.is_symlink():
        raise CliRuntimeError("A temporary OmniRush launcher file already exists; it was preserved.")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(launcher_contents(runtime))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o700)
        os.replace(temporary, destination)
    except CliRuntimeError:
        raise
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        raise CliRuntimeError("Could not install the private OmniRush launcher; the old file was preserved.") from None
    return destination


def launcher_path_in_path(path_value: str | None = None) -> bool:
    values = (path_value if path_value is not None else os.environ.get("PATH", "")).split(os.pathsep)
    return str(LAUNCHER_PATH.parent) in values


# Short aliases make the public purpose obvious to callers and tests.
ensure_runtime = ensure_official_cli


__all__ = ["VERSION", "INSTALL_ROOT", "AUTH_DIR", "OFFICIAL_AUTH_PATH", "LAUNCHER_PATH",
           "PINNED_PACKAGES", "CliRuntimeError", "OfficialCliRuntime", "installed_cli",
           "ensure_official_cli", "ensure_runtime", "launcher_contents", "install_launcher",
           "launcher_path_in_path"]
