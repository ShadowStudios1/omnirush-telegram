"""Pinned, rootless installation of the official OmniRush Linux native sidecar.

Only one regular sidecar member is copied; no archive paths are extracted and no
installer, shell script, sudo command or global package manager is ever run.
"""

from __future__ import annotations

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


PINNED_VERSION = "3.1.1"
RUNTIME_ROOT = Path.home() / ".local/share/omnirush-telegram-portable/runtimes"
PINNED_ASSETS = {
    "3.1.1": {
        "x64": (253016477, "fc23d76064f6cf6b626e5228585490701bbccd56889a55c6a1f11c6cd2198683"),
        "arm64": (251396210, "12122783e08fb020cd73aa683d7760a4d0cf15da0afe3ddb92e8241c9453607e"),
    },
}
MAX_ARCHIVE_BYTES = 300 * 1024 * 1024
MAX_EXECUTABLE_BYTES = 512 * 1024 * 1024
MAX_TAR_BYTES = 2 * 1024 * 1024 * 1024
API_URL = "https://api.github.com/repos/omnirush-ai/omnirush-gui/releases/tags/v{}"
CHUNK_BYTES = 1024 * 1024


class ReleaseError(RuntimeError):
    """Safe installer failure without URLs, headers or raw response bodies."""


def _architecture() -> tuple[str, str]:
    if platform.system() != "Linux" or platform.libc_ver()[0].lower() != "glibc":
        raise ReleaseError("The official runtime requires Linux with glibc.")
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64", "x86_64"
    if machine in ("aarch64", "arm64"):
        return "arm64", "aarch64"
    raise ReleaseError("The official runtime supports only Linux x64 and arm64.")


def _spec(version: str) -> tuple[str, str, int, str]:
    if not isinstance(version, str) or version not in PINNED_ASSETS:
        raise ReleaseError("This installer only supports explicitly pinned release 3.1.1.")
    architecture, native = _architecture()
    try:
        size, digest = PINNED_ASSETS[version][architecture]
    except (KeyError, TypeError, ValueError):
        raise ReleaseError("Release size or SHA-256 pin is missing or invalid.") from None
    if (type(size) is not int or not 0 < size <= MAX_ARCHIVE_BYTES
            or not isinstance(digest, str) or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)):
        raise ReleaseError("Release size or SHA-256 pin is missing or invalid.")
    return architecture, native, size, digest


def _safe_url(url: str, *, api: bool = False) -> str:
    try:
        parsed = urlsplit(url)
        allowed = {"api.github.com"} if api else {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
        if (parsed.scheme != "https" or parsed.hostname not in allowed
                or parsed.port not in (None, 443) or parsed.username is not None
                or parsed.password is not None or parsed.fragment
                or any(ord(c) < 33 for c in url)):
            raise ValueError
    except (ValueError, TypeError):
        raise ReleaseError("Refusing a non-official release URL.") from None
    return url


class _OfficialRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _safe_url(newurl, api=urlsplit(req.full_url).hostname == "api.github.com")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, *, api: bool = False):
    _safe_url(url, api=api)
    request = urllib.request.Request(url, headers={
        "User-Agent": "omnirush-telegram-portable/3.1.1",
        "Accept": "application/vnd.github+json" if api else "application/octet-stream",
        "Accept-Encoding": "identity",
    })
    try:
        # Ignore inherited proxies: release sources and redirects remain pinned.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _OfficialRedirect())
        response = opener.open(request, timeout=60)
        _safe_url(response.geturl(), api=api)
        if response.status != 200:
            response.close()
            raise ReleaseError("Official release download failed.")
        return response
    except (OSError, ValueError, urllib.error.URLError):
        raise ReleaseError("Cannot reach the official release source.") from None


def _asset(version: str, architecture: str, size: int, digest: str) -> dict:
    with _open(API_URL.format(version), api=True) as response:
        body = response.read(2 * 1024 * 1024 + 1)
    if len(body) > 2 * 1024 * 1024:
        raise ReleaseError("Official release metadata exceeds the safety limit.")
    try:
        release = json.loads(body)
        if (release["tag_name"] != "v" + version or release.get("draft") is not False
                or release.get("prerelease") is not False or not release.get("published_at")):
            raise ValueError
        filename = f"omnirush-linux-{architecture}-{version}.tar.gz"
        matches = [asset for asset in release["assets"] if asset.get("name") == filename]
        if len(matches) != 1:
            raise ValueError
        asset = matches[0]
        expected_url = f"https://github.com/omnirush-ai/omnirush-gui/releases/download/v{version}/{filename}"
        if (type(asset["size"]) is not int or asset["size"] != size
                or asset.get("digest") != "sha256:" + digest
                or asset["browser_download_url"] != expected_url):
            raise ValueError
        _safe_url(asset["browser_download_url"])
        return asset
    except (ValueError, TypeError, KeyError, AttributeError, RecursionError):
        raise ReleaseError("Official release metadata does not match the pinned asset and digest.") from None


def _download(asset: dict, destination: Path, size: int, digest: str, progress) -> None:
    hashed = hashlib.sha256()
    count = 0
    with _open(asset["browser_download_url"]) as response, destination.open("xb") as output:
        os.chmod(destination, 0o600)
        length = response.headers.get("Content-Length")
        if length is not None and (not length.isdecimal() or int(length) != size):
            raise ReleaseError("Release download size does not match its pin.")
        while True:
            block = response.read(min(CHUNK_BYTES, size - count + 1))
            if not block:
                break
            count += len(block)
            if count > size or count > MAX_ARCHIVE_BYTES:
                raise ReleaseError("Release download exceeds its pinned size.")
            output.write(block)
            hashed.update(block)
            if progress is not None:
                progress(count, size)
        output.flush()
        os.fsync(output.fileno())
    if count != size or hashed.hexdigest() != digest:
        raise ReleaseError("Release checksum verification failed; nothing was installed.")


class _BoundedReader:
    def __init__(self, stream):
        self.stream = stream
        self.count = 0

    def read(self, size=-1):
        size = min(size if size >= 0 else CHUNK_BYTES, MAX_TAR_BYTES - self.count + 1)
        block = self.stream.read(size)
        self.count += len(block)
        if self.count > MAX_TAR_BYTES:
            raise ReleaseError("Release archive expands beyond the safety limit.")
        return block


def _extract(archive: Path, destination: Path, native: str) -> str:
    selected = False
    hashed = hashlib.sha256()
    expected = ("resources", "sidecars", f"opencode-{native}-unknown-linux-gnu")
    try:
        with gzip.open(archive, "rb") as uncompressed:
            with tarfile.open(fileobj=_BoundedReader(uncompressed), mode="r|") as members:
                for index, member in enumerate(members):
                    name = member.name
                    parts = PurePosixPath(name).parts
                    if (index > 200000 or not parts or name.startswith("/")
                            or "\\" in name or "\x00" in name or ":" in name
                            or ".." in name.split("/") or member.issym() or member.islnk()
                            or not (member.isfile() or member.isdir())):
                        raise ReleaseError("Unsafe release archive member; nothing was installed.")
                    if tuple(parts[-3:]) != expected:
                        continue
                    if selected or not member.isfile() or not 20 <= member.size <= MAX_EXECUTABLE_BYTES:
                        raise ReleaseError("Release sidecar member is missing, duplicated or invalid.")
                    selected = True
                    source = members.extractfile(member)
                    if source is None:
                        raise ReleaseError("Release sidecar cannot be read.")
                    with source, destination.open("xb") as output:
                        os.chmod(destination, 0o700)
                        remaining = member.size
                        first = True
                        while remaining:
                            block = source.read(min(remaining, CHUNK_BYTES))
                            if not block:
                                raise ReleaseError("Release sidecar was truncated.")
                            if first:
                                machine = 62 if native == "x86_64" else 183
                                if (len(block) < 20 or block[:6] != b"\x7fELF\x02\x01"
                                        or int.from_bytes(block[18:20], "little") != machine):
                                    raise ReleaseError("Release sidecar is not the expected native Linux executable.")
                                first = False
                            remaining -= len(block)
                            output.write(block)
                            hashed.update(block)
                        output.flush()
                        os.fsync(output.fileno())
    except (OSError, EOFError, tarfile.TarError):
        raise ReleaseError("Release archive is unreadable; nothing was installed.") from None
    if not selected:
        raise ReleaseError("Official archive does not contain the native sidecar.")
    return hashed.hexdigest()


def _location(version: str, architecture: str, native: str) -> Path:
    return RUNTIME_ROOT.expanduser().absolute() / version / architecture / f"opencode-{native}-unknown-linux-gnu"


def _check_private(path: Path, *, directory: bool = False) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReleaseError("Private runtime paths must not contain symlinks.")
    try:
        info = path.stat(follow_symlinks=False)
        valid = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not valid or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReleaseError("Installed runtime files must be private and user-owned.")
    except OSError:
        raise ReleaseError("Cannot read the private runtime installation.") from None


def _installed(version: str, architecture: str, native: str, digest: str) -> Path | None:
    executable = _location(version, architecture, native)
    if not executable.parent.exists() and not executable.parent.is_symlink():
        return None
    for directory in (RUNTIME_ROOT.expanduser().absolute(), executable.parent.parent, executable.parent):
        _check_private(directory, directory=True)
    _check_private(executable)
    manifest_path = executable.parent / "manifest.json"
    _check_private(manifest_path)
    try:
        if manifest_path.stat().st_size > 4096:
            raise ValueError
        manifest = json.loads(manifest_path.read_bytes())
        if (manifest.get("version") != version or manifest.get("architecture") != architecture
                or manifest.get("archive_sha256") != digest
                or not os.access(executable, os.X_OK)
                or not 20 <= executable.stat().st_size <= MAX_EXECUTABLE_BYTES):
            raise ValueError
        hashed = hashlib.sha256()
        with executable.open("rb") as stream:
            while block := stream.read(CHUNK_BYTES):
                hashed.update(block)
        if hashed.hexdigest() != manifest.get("executable_sha256"):
            raise ValueError
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise ReleaseError("Installed runtime failed integrity verification; it was not overwritten.") from None
    return executable


def installed_runtime(version: str = PINNED_VERSION) -> Path | None:
    """Read and verify the pinned private runtime, with no filesystem writes."""
    architecture, native, _size, digest = _spec(version)
    return _installed(version, architecture, native, digest)


def ensure_runtime(version: str = PINNED_VERSION, progress=None) -> Path:
    """Install exactly the pinned release; progress receives (downloaded, total).

    Existing installations are verified and returned without a network request.
    Versions live in separate directories and are never upgraded or overwritten.
    """
    architecture, native, size, digest = _spec(version)
    if progress is not None and not callable(progress):
        raise ReleaseError("Download progress must be a callback.")
    existing = _installed(version, architecture, native, digest)
    if existing is not None:
        return existing
    from .runtime import private_directory
    try:
        root = private_directory(RUNTIME_ROOT.expanduser().absolute().parent)
        root = private_directory(root / RUNTIME_ROOT.name)
    except RuntimeError:
        raise ReleaseError("Cannot create the private runtime installation directory.") from None
    descriptor = None
    try:
        descriptor = os.open(root / ".install.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ReleaseError("Runtime installation lock is not a private user-owned file.")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        existing = _installed(version, architecture, native, digest)
        if existing is not None:
            return existing
        asset = _asset(version, architecture, size, digest)
        with tempfile.TemporaryDirectory(prefix=".install-", dir=root) as temporary:
            staging = Path(temporary)
            archive = staging / "release.tar.gz"
            _download(asset, archive, size, digest, progress)
            install = staging / "native"
            install.mkdir(mode=0o700)
            executable = install / f"opencode-{native}-unknown-linux-gnu"
            executable_digest = _extract(archive, executable, native)
            manifest_path = install / "manifest.json"
            with manifest_path.open("x", encoding="utf-8") as stream:
                os.chmod(manifest_path, 0o600)
                json.dump({"version": version, "architecture": architecture,
                           "archive_sha256": digest, "executable_sha256": executable_digest}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            destination = _location(version, architecture, native)
            private_directory(destination.parent.parent)
            if destination.parent.exists() or destination.parent.is_symlink():
                raise ReleaseError("Runtime destination appeared during installation; it was not overwritten.")
            os.rename(install, destination.parent)
            directory_fd = os.open(destination.parent.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        return destination
    except BlockingIOError:
        raise ReleaseError("Another runtime installation is in progress.") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise ReleaseError("Runtime installation failed safely; no global files were changed.") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


__all__ = ["ReleaseError", "PINNED_VERSION", "ensure_runtime", "installed_runtime"]
