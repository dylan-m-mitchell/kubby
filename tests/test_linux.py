"""Tests for host facts: package-manager detection and elevation description.

kubby no longer installs anything, so there is no argv building and nothing
is wrapped in pkexec/sudo to be tested. Both survivors are read-only
descriptions of the host, reported in the status line and `kubby --check`.
"""
from __future__ import annotations

import pytest

from kubby.installer import linux
from kubby.installer.tools import APT, DNF, PACMAN, ZYPPER


def _which(*present):
    """shutil.which stand-in: found only for the named binaries."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


class TestDescribeElevationMethod:
    def test_root(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 0)
        assert linux.describe_elevation_method() == "running as root"

    def test_pkexec(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(linux.shutil, "which", _which("pkexec"))
        assert "pkexec" in linux.describe_elevation_method()

    def test_sudo(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(linux.shutil, "which", _which())
        assert "sudo" in linux.describe_elevation_method()


class TestDetectPackageManager:
    @pytest.mark.parametrize(
        ("found", "expected"),
        [
            (["apt-get", "dnf"], (APT, "APT (Debian/Ubuntu)")),
            (["dnf"], (DNF, "DNF (Fedora/RHEL)")),
            (["pacman"], (PACMAN, "Pacman (Arch)")),
            (["zypper"], (ZYPPER, "zypper (openSUSE)")),
        ],
    )
    def test_first_match_wins_in_preference_order(self, monkeypatch, found, expected):
        monkeypatch.setattr(linux.shutil, "which", _which(*found))
        assert linux.detect_package_manager() == expected

    def test_no_supported_manager_raises(self, monkeypatch):
        monkeypatch.setattr(linux.shutil, "which", _which())
        with pytest.raises(RuntimeError, match="No supported package manager"):
            linux.detect_package_manager()
