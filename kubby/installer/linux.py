"""Linux-specific installer logic: package-manager detection + privilege escalation.

Privilege escalation order (first match wins):
1. **root** — run command as-is.
2. **pkexec** — polkit's GUI auth prompt. Best UX in a desktop session.
3. **sudo** — last-resort fallback; on a tty-less subprocess it will fail
   immediately with a useful error if the user can't authenticate.

"""
from __future__ import annotations

import os
import shutil
import subprocess
from typing import Tuple

# Re-export the package-manager constants for convenience.
from kubby.installer.tools import APT, DNF, PACMAN, ZYPPER  # noqa: F401

_INSTALL_TIMEOUT_S = 600


def detect_package_manager() -> Tuple[str, str]:
    """Return `(key, human_label)` for the host's package manager.

    Preference order: apt → dnf → pacman → zypper.

    Raises RuntimeError if no supported manager is on PATH.
    """
    if shutil.which("apt-get"):
        return APT, "APT (Debian/Ubuntu)"
    if shutil.which("dnf"):
        return DNF, "DNF (Fedora/RHEL)"
    if shutil.which("pacman"):
        return PACMAN, "Pacman (Arch)"
    if shutil.which("zypper"):
        return ZYPPER, "zypper (openSUSE)"
    raise RuntimeError(
        "No supported package manager found. kubby supports apt, dnf, pacman, "
        "or zypper on Linux."
    )


def describe_elevation_method() -> str:
    """Return a human-readable description of how `run_elevated` will operate."""
    if os.geteuid() == 0:
        return "running as root"
    if shutil.which("pkexec"):
        return "pkexec (graphical polkit prompt)"
    return "sudo (terminal password prompt)"


def wrap_elevated(cmd: tuple[str, ...]) -> list[str]:
    """Return `cmd` prepended with pkexec or sudo if not root.

    The returned list is suitable for ``subprocess.Popen`` or any other
    API that takes an argv list (as opposed to a single string).
    """
    argv = list(cmd)
    if os.geteuid() == 0:
        return argv
    if shutil.which("pkexec"):
        return ["pkexec", *argv]
    return ["sudo", *argv]


def run_elevated(cmd: tuple[str, ...]) -> subprocess.CompletedProcess:
    """Execute `cmd` with privilege sufficient to install system packages.

    Streams are captured so callers can show them in the UI log. The returned
    `CompletedProcess` has `.stdout`, `.stderr`, `.returncode`.

    Raises `FileNotFoundError` if the underlying elevator (`pkexec`, `sudo`)
    is missing on PATH. `subprocess.TimeoutExpired` if the install hangs.
    """
    argv = list(cmd)

    if os.geteuid() == 0:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=_INSTALL_TIMEOUT_S
        )

    if shutil.which("pkexec"):
        return subprocess.run(
            ["pkexec", *argv],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_S,
        )

    # Last resort: plain sudo. Likely to fail without a tty; the error will
    # surface in the UI log with enough context for the user to act on.
    return subprocess.run(
        ["sudo", *argv],
        capture_output=True,
        text=True,
        timeout=_INSTALL_TIMEOUT_S,
    )


def pkg_install_argv(pm_key: str, pkg_name: str) -> tuple[str, ...]:
    """Build the argv tuple to install `pkg_name` via the host package manager.

    Used for tools that have no upstream shell installer (e.g. podman) and
    must be pulled from the OS repo. Returns the argv as a tuple suitable
    for `subprocess.run(...)` / `linux.run_elevated(...)`. Raises
    `ValueError` for unsupported PMs — callers should display the error in
    the UI log.
    """
    if pm_key == APT:
        return ("apt-get", "install", "-y", pkg_name)
    if pm_key == DNF:
        return ("dnf", "install", "-y", pkg_name)
    if pm_key == PACMAN:
        return ("pacman", "-S", "--needed", "--noconfirm", pkg_name)
    if pm_key == ZYPPER:
        return ("zypper", "--non-interactive", "install", pkg_name)
    raise ValueError(
        f"kubby does not know how to install via package manager: {pm_key!r}"
    )
