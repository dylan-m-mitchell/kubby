"""Tests for package-manager detection, elevation wrapping, and install argv."""
from __future__ import annotations

import pytest

from kubby.installer import linux
from kubby.installer.tools import APT, DNF, PACMAN, ZYPPER


class TestPkgInstallArgv:
    @pytest.mark.parametrize(
        ("pm_key", "expected"),
        [
            (APT, ("apt-get", "install", "-y", "podman")),
            (DNF, ("dnf", "install", "-y", "podman")),
            (PACMAN, ("pacman", "-S", "--needed", "--noconfirm", "podman")),
            (ZYPPER, ("zypper", "--non-interactive", "install", "podman")),
        ],
    )
    def test_supported_package_managers(self, pm_key, expected):
        assert linux.pkg_install_argv(pm_key, "podman") == expected

    def test_unsupported_package_manager_raises(self):
        with pytest.raises(ValueError, match="does not know how to install"):
            linux.pkg_install_argv("brew", "podman")


def _which(*present):
    """shutil.which stand-in: found only for the named binaries."""
    return lambda name: f"/usr/bin/{name}" if name in present else None


class TestWrapElevated:
    def test_root_runs_unwrapped(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 0)
        assert linux.wrap_elevated(("apt-get", "install")) == ["apt-get", "install"]

    def test_prefers_pkexec_when_available(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(linux.shutil, "which", _which("pkexec"))
        assert linux.wrap_elevated(("apt-get",)) == ["pkexec", "apt-get"]

    def test_falls_back_to_sudo(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 1000)
        monkeypatch.setattr(linux.shutil, "which", _which())
        assert linux.wrap_elevated(("apt-get",)) == ["sudo", "apt-get"]

    def test_output_is_a_list(self, monkeypatch):
        monkeypatch.setattr(linux.os, "geteuid", lambda: 0)
        assert isinstance(linux.wrap_elevated(("x",)), list)


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
