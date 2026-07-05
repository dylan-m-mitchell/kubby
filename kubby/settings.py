"""Persisted user settings for kubby.

Stored as JSON at ``~/.config/kubby/settings.json`` (XDG-style). Created
lazily on first write; read errors fall back to defaults so the app never
crashes on a corrupted or missing config.

Currently only the ``minikube`` section is exposed in the UI. Future helpers
(load/save other sections) should reuse ``_deep_merge`` with new defaults.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from pathlib import Path
from typing import Any

CONFIG_DIR = Path(os.path.expanduser("~/.config/kubby"))
CONFIG_FILE = CONFIG_DIR / "settings.json"


# Minikube settings exposed in the UI. These correspond 1:1 with the form
# fields in the settings modal — when adding a new field, update both this
# dict and the JS form, OR values saved from older versions will be dropped.
DEFAULT_MINIKUBE: dict[str, Any] = {
    # "" means "let minikube auto-detect". "docker"/"podman"/"kvm2"/"none"
    # force a driver. The UI offers a dropdown.
    "driver": "",
    # `--rootless`. Only meaningful with drivers that support it (podman,
    # kvm2 in some configs, minikube's built-in `rootless` driver). Inert
    # otherwise — the user just learns "it didn't help" if incompatible.
    "rootless": False,
    # `--cpus=<n>`. Stored as a string so the form input round-trips blanks.
    "cpus": "2",
    # `--memory=<n>`. Plain string ("2g", "4g", "4096mb"). Minikube parses it.
    "memory": "2g",
    # `--kubernetes-version=<v>`. Empty = minikube picks the default.
    "kubernetes_version": "",
    # `--addons=<comma,list>`. The literal token "default" enables minikube's
    # default addons (storage-provisioner, default-storageclass, etc.).
    # Other useful choices: ingress, dashboard, metrics-server.
    "addons": ["default"],
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursive dict merge; ``override`` values win for non-dict leaves."""
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load() -> dict[str, Any]:
    """Return current settings, merged on top of defaults.

    Always returns a fresh, fully-populated dict — callers may mutate freely.
    Missing file, OSError, or JSONDecodeError all collapse to "all defaults".
    """
    defaults = {"minikube": copy.deepcopy(DEFAULT_MINIKUBE)}
    if not CONFIG_FILE.exists():
        return defaults
    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return defaults
    if not isinstance(data, dict):
        return defaults
    return _deep_merge(defaults, data)


def save(settings: dict[str, Any]) -> None:
    """Atomically write ``settings`` to ``CONFIG_FILE``.

    Validates that the minikube section is a dict so a corrupt IPC payload
    can't clobber the file. Raises ``OSError`` on filesystem errors.

    Atomicity: write to a temp file in the same directory, then
    ``os.replace`` over the real path. Avoids a partial-file window if
    the process is killed mid-write.
    """
    if not isinstance(settings, dict):
        raise ValueError("settings must be a JSON object")
    mk = settings.get("minikube")
    if not isinstance(mk, dict):
        mk = copy.deepcopy(DEFAULT_MINIKUBE)
        settings = {**settings, "minikube": mk}

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, prefix=".settings-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2, sort_keys=True)
        os.replace(tmp_path, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
