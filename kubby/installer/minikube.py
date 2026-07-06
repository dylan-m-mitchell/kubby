"""Translate user settings into ``minikube`` CLI argv.

Pure builder function — no subprocess calls, no disk I/O, no side effects.
Isolated from ``kubby.app`` so the translation from settings → flags can
be unit-tested without any fixture setup.
"""
from __future__ import annotations

from typing import Any


def start_args(settings: dict[str, Any]) -> list[str]:
    """Return argv for ``minikube start`` reflecting ``settings``.

    Only flags with non-empty values are appended — minikube's own defaults
    apply otherwise, so the user starts with minimal noise.
    """
    args: list[str] = ["minikube", "start"]

    driver = (settings.get("driver") or "").strip()
    if driver:
        args.append(f"--driver={driver}")

    if settings.get("rootless"):
        args.append("--rootless")

    cpus = (settings.get("cpus") or "").strip()
    if cpus:
        args.append(f"--cpus={cpus}")

    memory = (settings.get("memory") or "").strip()
    if memory:
        args.append(f"--memory={memory}")

    k8s_version = (settings.get("kubernetes_version") or "").strip()
    if k8s_version:
        args.append(f"--kubernetes-version={k8s_version}")

    addons = settings.get("addons") or []
    if isinstance(addons, list) and addons:
        cleaned = [str(a).strip() for a in addons if str(a).strip()]
        if cleaned:
            # `--addons` accepts a comma-separated list; passing it once is
            # cheaper than passing every addon as its own flag (which minikube
            # would treat as toggling separately).
            args.append(f"--addons={','.join(cleaned)}")

    return args


def stop_args() -> list[str]:
    """Argv for ``minikube stop``."""
    return ["minikube", "stop"]


def delete_args() -> list[str]:
    """Argv for ``minikube delete``."""
    return ["minikube", "delete"]
