"""Tests for the tool registry: construction invariants and version parsers."""
from __future__ import annotations

import pytest

from kubby.installer.tools import (
    APT,
    DNF,
    PACMAN,
    ZYPPER,
    TOOLS,
    Tool,
    _parse_kubectl,
    _parse_semver_token,
)


def _tool(**overrides) -> Tool:
    """Build a minimally valid Tool; override any field per test."""
    fields = dict(
        key="demo",
        label="demo",
        description="A demo tool.",
        website="https://example.com/",
        version_args=("--version",),
        parse_version=_parse_semver_token(),
        install_script="true",
    )
    fields.update(overrides)
    return Tool(**fields)


class TestToolInvariants:
    def test_registry_keys_match_tool_keys(self):
        assert set(TOOLS) == {tool.key for tool in TOOLS.values()}

    def test_every_registry_tool_has_exactly_one_install_method(self):
        for key, tool in TOOLS.items():
            assert (tool.install_script is None) != (tool.pkg_name is None), key

    def test_registry_tools_have_metadata(self):
        for tool in TOOLS.values():
            assert tool.label
            assert tool.description
            assert tool.website.startswith("https://")
            assert tool.version_args

    def test_install_script_and_pkg_name_both_set_raises(self):
        with pytest.raises(ValueError, match="exactly one of"):
            _tool(install_script="true", pkg_name="demo")

    def test_neither_install_script_nor_pkg_name_raises(self):
        with pytest.raises(ValueError, match="exactly one of"):
            _tool(install_script=None, pkg_name=None)

    def test_pkg_name_only_is_valid(self):
        assert _tool(install_script=None, pkg_name="demo").pkg_name == "demo"


class TestSemverParser:
    def test_parses_v_prefixed_version(self):
        assert _parse_semver_token()("minikube version: v1.33.1") == "1.33.1"

    def test_parses_version_without_v_prefix(self):
        assert _parse_semver_token()("1.28.0") == "1.28.0"

    def test_parses_two_component_version(self):
        assert _parse_semver_token()("v1.4") == "1.4"

    def test_returns_none_when_absent(self):
        assert _parse_semver_token()("not installed") is None

    def test_does_not_match_a_bare_integer(self):
        assert _parse_semver_token()("build 20240101") is None

    def test_requires_prefix_when_one_is_given(self):
        parse = _parse_semver_token("podman version ")
        assert parse("podman version 5.1.0") == "5.1.0"
        assert parse("minikube version: v1.33.1") is None


class TestKubectlParser:
    def test_parses_client_version(self):
        out = "Client Version: v1.28.0\nKustomize Version: v5.0.4-1\n"
        assert _parse_kubectl(out) == "1.28.0"

    def test_parses_without_v_prefix(self):
        assert _parse_kubectl("Client Version: 1.29.2") == "1.29.2"

    def test_returns_none_when_no_client_version(self):
        assert _parse_kubectl("error: unknown flag") is None


def test_package_manager_constants():
    assert (APT, DNF, PACMAN, ZYPPER) == ("apt", "dnf", "pacman", "zypper")
