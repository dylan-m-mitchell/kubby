"""Linux host facts: package-manager detection + how the host elevates.

kubby does not install anything, so it never elevates itself — the
pkexec/sudo wrappers that used to live here are gone. What remains is
read-only host context for the status line and ``kubby --check``:
`detect_package_manager` and `describe_elevation_method` both *describe*
the machine rather than acting on it.
"""
from __future__ import annotations

import os
import shutil
from typing import Tuple

# Re-export the package-manager constants for convenience.
from kubby.installer.tools import APT, DNF, PACMAN, ZYPPER  # noqa: F401


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
    """Describe how *this host* would prompt for root.

    kubby never elevates itself — nothing here runs privileged. This is
    reported as context, because minikube commonly needs a password for
    some drivers, and knowing which prompt to expect saves a confusing
    stall.
    """
    if os.geteuid() == 0:
        return "running as root"
    if shutil.which("pkexec"):
        return "pkexec (graphical polkit prompt)"
    return "sudo (terminal password prompt)"
