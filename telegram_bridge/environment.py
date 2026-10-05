"""Local-only Linux capability checks. Never probes cloud metadata endpoints."""
from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess


class EnvironmentError(RuntimeError):
    pass


def systemd_user_available() -> bool:
    if not shutil.which("systemctl"):
        return False
    try:
        result = subprocess.run(["systemctl", "--user", "show-environment"],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=5, check=False)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _read(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[:32768]
    except OSError:
        return ""


def distro_info():
    values = {}
    for line in _read("/etc/os-release").splitlines():
        if "=" in line and not line.startswith("#"):
            key, raw = line.split("=", 1)
            try:
                parts = shlex.split(raw)
                values[key] = " ".join(parts)
            except ValueError:
                continue
    return values


@dataclass(frozen=True)
class Environment:
    system: str
    distro: str
    arch: str
    libc: str
    wsl: bool
    ssh: bool
    rdp: bool
    container: bool
    systemd_user: bool

    def lines(self):
        yield f"{self.system} / {self.distro} / {self.arch} / {self.libc}"
        yield "Signals: " + ", ".join(label for label, enabled in (
            ("WSL", self.wsl), ("SSH", self.ssh), ("RDP", self.rdp),
            ("container", self.container)) if enabled) if any((self.wsl, self.ssh, self.rdp, self.container)) else "Signals: ordinary Linux host"
        yield "systemd user manager: " + ("reachable" if self.systemd_user else "unavailable")
        if self.wsl:
            yield "WSL stops when its VM/Windows sleeps or shuts down; this cannot guarantee 24/7 uptime."
        if self.container:
            yield "Container: use `python3 omnirush.py run` as your supervisor; durability depends on the container host."


def detect_environment() -> Environment:
    distro = distro_info()
    kernel = (_read("/proc/sys/kernel/osrelease") + platform.release()).lower()
    libc, version = platform.libc_ver()
    cgroup = _read("/proc/1/cgroup").lower()
    return Environment(
        platform.system(), distro.get("PRETTY_NAME", distro.get("ID", "unknown")),
        platform.machine(), f"{libc or 'unknown libc'} {version}".strip(),
        "microsoft" in kernel or "WSL_DISTRO_NAME" in os.environ,
        bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY")),
        any(os.environ.get(key) for key in ("XRDP_SESSION", "XRDP_SOCKET_PATH", "RDP_SESSION")),
        Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()
        or bool(os.environ.get("container")) or any(word in cgroup for word in ("docker", "kubepods", "lxc", "podman")),
        systemd_user_available(),
    )


def refuse_root():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise EnvironmentError("Do not run this bridge as root. Create/sign in as a dedicated unprivileged Linux user, then retry without sudo.")
    if platform.system() != "Linux":
        raise EnvironmentError("This portable launcher supports Linux, including WSL, only.")
