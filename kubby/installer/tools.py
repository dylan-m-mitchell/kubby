"""Tool registry: declares which CLIs kubby *knows about*.

Each `Tool` carries:
- the binary name (`key`)
- friendly `label` and `description` (shown in the UI)
- argv that, when run as `<key> <version_args>`, prints the tool's version
- a `parse_version` callable that extracts a `MAJOR.MINOR[.PATCH]` string
- `website`, where the tool is documented and obtained

kubby does not install anything. The `install_script` / `pkg_name` fields
that used to live here are gone, along with the upstream shell snippets
they pointed at — holding a copy of someone else's install script is a
liability (helm's was fetched from a `main` branch, unpinned). `website` is
the single source of truth for "where do I get this", and the tools panel
and the preflight both point there.
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
    ),
    "helm": Tool(
        key="helm",
        label="helm",
        description="Kubernetes package manager.",
        website="https://helm.sh/",
        version_args=("version", "--short"),
        parse_version=_parse_semver_token(),
    ),
    "podman": Tool(
        key="podman",
        label="podman",
        description="Daemonless container engine (minikube driver).",
        website="https://podman.io/",
        version_args=("--version",),
        parse_version=_parse_semver_token("podman version "),
    ),
    "kubectl": Tool(
        key="kubectl",
        label="kubectl",
        description="Official Kubernetes CLI.",
        website="https://kubernetes.io/docs/reference/kubectl/",
        version_args=("version", "--client"),
        parse_version=_parse_kubectl,
    ),
}
