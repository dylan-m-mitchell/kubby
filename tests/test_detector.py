"""Tests for tool detection (``kubby.installer.detector.detect``)."""
from __future__ import annotations

import subprocess

import pytest

from kubby.installer import detector
from kubby.installer.tools import TOOLS

KUBECTL = TOOLS["kubectl"]


def _run(returncode=0, stdout="", stderr="", exc=None):
    """Build a fake ``subprocess.run`` that optionally raises."""
    if exc is not None:
        def fake(*args, **kwargs):
            raise exc
        return fake

    def fake(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0], returncode=returncode, stdout=stdout, stderr=stderr
        )
    return fake


def _which(path):
    return lambda name: path


def test_missing_binary_is_not_installed(monkeypatch):
    monkeypatch.setattr(detector.shutil, "which", _which(None))
    assert detector.detect(KUBECTL) == (False, None, None)


def test_installed_with_parsed_version(monkeypatch):
    monkeypatch.setattr(detector.shutil, "which", _which("/usr/bin/kubectl"))
    monkeypatch.setattr(
        detector.subprocess, "run", _run(stdout="Client Version: v1.30.1\n")
    )
    assert detector.detect(KUBECTL) == (True, "1.30.1", "/usr/bin/kubectl")


def test_version_on_stderr_is_still_parsed(monkeypatch):
    monkeypatch.setattr(detector.shutil, "which", _which("/usr/bin/kubectl"))
    monkeypatch.setattr(
        detector.subprocess, "run", _run(stderr="Client Version: v1.29.0\n")
    )
    installed, version, _ = detector.detect(KUBECTL)
    assert installed is True
    assert version == "1.29.0"


def test_nonzero_exit_means_installed_without_version(monkeypatch):
    monkeypatch.setattr(detector.shutil, "which", _which("/usr/bin/kubectl"))
    monkeypatch.setattr(detector.subprocess, "run", _run(returncode=2, stdout="nope"))
    assert detector.detect(KUBECTL) == (True, None, "/usr/bin/kubectl")


def test_unparsable_version_means_installed_without_version(monkeypatch):
    monkeypatch.setattr(detector.shutil, "which", _which("/usr/bin/kubectl"))
    monkeypatch.setattr(detector.subprocess, "run", _run(stdout="weird output"))
    assert detector.detect(KUBECTL) == (True, None, "/usr/bin/kubectl")


@pytest.mark.parametrize(
    "exc",
    [
        subprocess.TimeoutExpired(cmd="kubectl", timeout=10),
        FileNotFoundError(),
        PermissionError(),
    ],
)
def test_unrunnable_binary_is_still_installed(monkeypatch, exc):
    monkeypatch.setattr(detector.shutil, "which", _which("/usr/bin/kubectl"))
    monkeypatch.setattr(detector.subprocess, "run", _run(exc=exc))
    assert detector.detect(KUBECTL) == (True, None, "/usr/bin/kubectl")
