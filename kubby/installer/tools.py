"""Tool registry: declares which CLIs kubby manages and how to install them.

Each `Tool` carries:
- the binary name (`key`)
- friendly `label` and `description` (shown in the UI)
- argv that, when run as `<key> <version_args>`, prints the tool's version
- a `parse_version` callable that extracts a `MAJOR.MINOR[.PATCH]` string
- a dict mapping package-manager key → argv-tuple install commands

Notes on install commands:
- For apt-based systems we use the upstream-shipped shell scripts (minikube, helm)
  or the upstream kubectl direct-download. This avoids needing to add 3rd-party
  apt repos from the installer. If a user needs an airgapped repo-only setup,
  they can run the official installation manually.
- Each tuple in `install_commands[pm]` is run as a separate subprocess (so a
  failed `mkdir` won't hide a successful install that came after, etc.).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

APT = "apt"
DNF = "dnf"
PACMAN = "pacman"
ZYPPER = "zypper"
BREW = "brew"


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    description: str
    website: str
    version_args: tuple[str, ...]
    parse_version: Callable[[str], str | None]
    install_commands: dict[str, tuple[tuple[str, ...], ...]] = field(
        default_factory=dict
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
# Per-package-manager install command sets
# ---------------------------------------------------------------------------


# minikube is not in Debian/Fedora base repos; use the upstream installer script.
MINIKUBE_INSTALL: dict[str, tuple[tuple[str, ...], ...]] = {
    APT: (
        (
            "sh",
            "-c",
            "curl -fsSL https://minikube.sigs.k8s.io/scripts/install.sh | sh -",
        ),
    ),
    DNF: (
        (
            "sh",
            "-c",
            "curl -fsSL https://minikube.sigs.k8s.io/scripts/install.sh | sh -",
        ),
    ),
    BREW: (("brew", "install", "minikube"),),
}


HELM_INSTALL: dict[str, tuple[tuple[str, ...], ...]] = {
    APT: (
        (
            "sh",
            "-c",
            "curl -fsSL https://raw.githubusercontent.com/helm/helm/main/scripts/get-helm-3 | sh -",
        ),
    ),
    DNF: (("dnf", "install", "-y", "helm"),),
    PACMAN: (("pacman", "-S", "--noconfirm", "helm"),),
    BREW: (("brew", "install", "helm"),),
}


PODMAN_INSTALL: dict[str, tuple[tuple[str, ...], ...]] = {
    APT: (("apt-get", "install", "-y", "podman"),),
    DNF: (("dnf", "install", "-y", "podman"),),
    PACMAN: (("pacman", "-S", "--noconfirm", "podman"),),
    BREW: (("brew", "install", "podman"),),
}


# kubectl is installed to /usr/local/bin via the official Google download.
KUBECTL_INSTALL: dict[str, tuple[tuple[str, ...], ...]] = {
    APT: (
        (
            "sh",
            "-c",
            'set -e; '
            'KUBECTL_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt); '
            'curl -fsSLo /usr/local/bin/kubectl '
            '"https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; '
            'chmod +x /usr/local/bin/kubectl',
        ),
    ),
    DNF: (
        (
            "sh",
            "-c",
            'set -e; '
            'KUBECTL_VERSION=$(curl -fsSL https://dl.k8s.io/release/stable.txt); '
            'curl -fsSLo /usr/local/bin/kubectl '
            '"https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"; '
            'chmod +x /usr/local/bin/kubectl',
        ),
    ),
    BREW: (("brew", "install", "kubernetes-cli"),),
}


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
        install_commands=MINIKUBE_INSTALL,
    ),
    "helm": Tool(
        key="helm",
        label="helm",
        description="Kubernetes package manager.",
        website="https://helm.sh/",
        version_args=("version", "--short"),
        parse_version=_parse_semver_token(),
        install_commands=HELM_INSTALL,
    ),
    "podman": Tool(
        key="podman",
        label="podman",
        description="Daemonless container engine (minikube driver).",
        website="https://podman.io/",
        version_args=("--version",),
        parse_version=_parse_semver_token("podman version "),
        install_commands=PODMAN_INSTALL,
    ),
    "kubectl": Tool(
        key="kubectl",
        label="kubectl",
        description="Official Kubernetes CLI.",
        website="https://kubernetes.io/docs/reference/kubectl/",
        version_args=("version", "--client"),
        parse_version=_parse_kubectl,
        install_commands=KUBECTL_INSTALL,
    ),
}
