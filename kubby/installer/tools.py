"""Tool registry: declares which CLIs kubby manages and how to install them.

Each `Tool` carries:
- the binary name (`key`)
- friendly `label` and `description` (shown in the UI)
- argv that, when run as `<key> <version_args>`, prints the tool's version
- a `parse_version` callable that extracts a `MAJOR.MINOR[.PATCH]` string
- exactly one of:
    * `install_script` — a `sh -c` snippet that installs the tool via its
      official upstream installer (minikube, helm, kubectl).
    * `pkg_name` — the host package-manager name, for tools that ship in
      the OS repo and have no upstream shell installer (podman).

We deliberately avoid host-PM-keyed dicts of install commands now that the
delivery is binary rather than .deb: the upstream scripts are PM-agnostic
on Linux, and the one tool that needs the PM (podman) only ships there.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable

APT = "apt"
DNF = "dnf"
PACMAN = "pacman"
ZYPPER = "zypper"


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    description: str
    website: str
    version_args: tuple[str, ...]
    parse_version: Callable[[str], str | None]
    # `sh -c` snippet that installs the tool, run with elevation. Exactly
    # one of `install_script` / `pkg_name` should be set per tool.
    install_script: str | None = None
    # Host-PM package name (used for tools with no upstream shell installer,
    # e.g. podman). `linux.pkg_install_argv()` turns this into argv.
    pkg_name: str | None = None

    def __post_init__(self) -> None:
        # Enforce the "exactly one of install_script/pkg_name" invariant at
        # construction so a future Tool entry can't accidentally have both
        # (silent script-wins) or neither (silent install failure).
        if (self.install_script is None) == (self.pkg_name is None):
            raise ValueError(
                f"Tool {self.key!r} must set exactly one of install_script "
                f"or pkg_name (got install_script={self.install_script!r}, "
                f"pkg_name={self.pkg_name!r})"
            )


# ---------------------------------------------------------------------------
# Version parsers
# ---------------------------------------------------------------------------


def _parse_semver_token(prefix: str = "") -> Callable[[str], str | None]:
    """Match a `vMAJOR.MINOR[.PATCH]` token, optionally preceded by `prefix`."""
    rx = re.compile(rf"(?<!\d){re.escape(prefix)}v?(\d+\.\d+(?:\.\d+)?)")

    def _parse(out: str) -> str | None:
        m = rx.search(out)
        return m.group(1) if m else None

    return _parse


def _parse_kubectl(out: str) -> str | None:
    """kubectl version --client prints `Client Version: v1.28.0 ...` on stdout+stderr."""
    m = re.search(r"Client Version:\s*v?(\d+\.\d+(?:\.\d+)?)", out)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Install scripts (run via `sh -c` with elevation)
# ---------------------------------------------------------------------------


# minikube ships an official shell installer at minikube.sigs.k8s.io.
MINIKUBE_INSTALL_SCRIPT = (
    "curl -fsSL https://minikube.sigs.k8s.io/scripts/install.sh | sh -"
)


# helm's official installer is the get-helm-3 script.
HELM_INSTALL_SCRIPT = (
    "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | sh -"
)


# kubectl is distributed as a single static binary; download the latest
# stable version to /usr/local/bin and chmod +x. Uses the official
# dl.k8s.io URLs.
KUBECTL_INSTALL_SCRIPT = (
    "set -e; "
    "KUBECTL_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt); "
    "curl -fsSLo /usr/local/bin/kubectl "
    "\"https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl\"; "
    "chmod +x /usr/local/bin/kubectl"
)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


TOOLS: dict[str, Tool] = {
    "minikube": Tool(
        key="minikube",
        label="minikube",
        description="Local Kubernetes cluster for development.",
        website="https://minikube.sigs.k8s.io/",
        version_args=("version", "--short"),
        parse_version=_parse_semver_token(),
        install_script=MINIKUBE_INSTALL_SCRIPT,
    ),
    "helm": Tool(
        key="helm",
        label="helm",
        description="Kubernetes package manager.",
        website="https://helm.sh/",
        version_args=("version", "--short"),
        parse_version=_parse_semver_token(),
        install_script=HELM_INSTALL_SCRIPT,
    ),
    "podman": Tool(
        key="podman",
        label="podman",
        description="Daemonless container engine (minikube driver).",
        website="https://podman.io/",
        version_args=("--version",),
        parse_version=_parse_semver_token("podman version "),
        pkg_name="podman",
    ),
    "kubectl": Tool(
        key="kubectl",
        label="kubectl",
        description="Official Kubernetes CLI.",
        website="https://kubernetes.io/docs/reference/kubectl/",
        version_args=("version", "--client"),
        parse_version=_parse_kubectl,
        install_script=KUBECTL_INSTALL_SCRIPT,
    ),
}
