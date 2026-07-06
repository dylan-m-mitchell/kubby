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
# fields in the settings modal — when adding a new field, both this dict
# AND the JS form should be updated, OR values saved from older versions
# will be silently dropped on next load+save.
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

# Per-key expected Python ``isinstance`` type for the ``minikube`` section.
# Derived from ``DEFAULT_MINIKUBE`` so adding a key to defaults
# automatically extends the coercion schema — no risk of a parallel dict
# drifting out of sync.
#
# NB: We use per-key concrete types (one ``isinstance`` call per check),
# not multi-type unions, so the fact that ``bool`` is a subclass of ``int``
# is irrelevant here — each check resolves against exactly one concrete
# ``type`` and never visits the subclass relation.
_MINIKUBE_KEY_TYPES: dict[str, type] = {
    k: type(v) for k, v in DEFAULT_MINIKUBE.items()
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


def _try_auto_coerce(key: str, value: Any) -> Any | None:
    """Best-effort coerce ``value`` to the schema-expected type for ``key``.

    Returns the coerced value on success, or ``None`` if no safe coercion
    exists — in which case the caller should fall back to the field's
    default. Safe coercions are deliberately conservative (they must
    preserve user intent, not silently substitute something surprising):

    - ``cpus``/``memory``/``driver``/``kubernetes_version`` (str):
      ``int → str`` (``4 → "4"``) and ``int-valued float → str``
      (``4.0 → "4"`` to avoid minikube misparsing ``"4.0"``).
      ``bool → str`` and ``float-with-fractional → str`` are NOT
      coerced — they almost always indicate a broken config.
    - ``addons`` (list): ``str → list`` splits on commas
      (``"default,ingress" → ["default", "ingress"]``,
      ``"default" → ["default"]``; ``""`` or ``","`` return ``None`` so
      the caller falls back to the default).
    - ``rootless`` (bool): we do NOT auto-coerce 0/1 — too easy to
      silently accept malformed input.

    The caller (``_coerce_minikube_keys``) restricts ``key`` to entries
    in ``_MINIKUBE_KEY_TYPES``; an unknown key would ``KeyError`` here.
    That's intentional — we don't want an unvalidated schema field to
    slip through into a coerced config.
    """
    typ = _MINIKUBE_KEY_TYPES[key]
    if isinstance(value, typ):
        return value
    if typ is str:
        if isinstance(value, bool):
            return None  # bool → str is rarely what the user meant
        if isinstance(value, int):
            return str(value)
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return None
    if typ is list:
        if isinstance(value, str):
            parts = [s.strip() for s in value.split(",") if s.strip()]
            return parts if parts else None
        return None
    # typ is bool: no auto-coerce.
    return None


def _coerce_minikube_keys(mk: dict[str, Any]) -> None:
    """Reset any minikube field whose value can't be coerced to its schema type.

    Mutates ``mk`` in place. First tries a safe auto-coercion (so e.g.
    ``"cpus": 4`` becomes ``"4"`` instead of getting reset to ``"2"``),
    then falls back to a deepcopy of the field's default when no coercion
    is sensible.

    Without this, ``minikube.start_args(...)`` in
    ``kubby.installer.minikube`` would either crash on a surprising shape
    (e.g. ``"addons": 42``) or silently emit the wrong CLI flags
    (e.g. ``"addons": "default"`` str → no ``--addons`` flag at all).
    """
    for key, default in DEFAULT_MINIKUBE.items():
        if key not in mk:
            mk[key] = copy.deepcopy(default)
            continue
        coerced = _try_auto_coerce(key, mk[key])
        if coerced is not None:
            mk[key] = coerced
        else:
            mk[key] = copy.deepcopy(default)


def _coerce_minikube_in_settings(settings: dict[str, Any]) -> None:
    """Make ``settings["minikube"]`` a valid dict with coerced inner keys.

    Replaces the entire section with deepcopy defaults if it isn't a dict;
    otherwise coerces each key in place but on a SHALLOW COPY of the
    caller's block — so a caller holding a separate reference to their
    own ``settings["minikube"]`` isn't surprised by in-place mutation.
    The caller still sees the coerced result via ``settings["minikube"]``.
    """
    mk = settings.get("minikube")
    if not isinstance(mk, dict):
        mk = copy.deepcopy(DEFAULT_MINIKUBE)
        settings["minikube"] = mk
        return
    coerced = {**mk}
    _coerce_minikube_keys(coerced)
    settings["minikube"] = coerced


def load() -> dict[str, Any]:
    """Return current settings, coerced to a sane schema on top of defaults.

    Always returns a fresh, fully-populated dict — callers may mutate freely.
    Missing file, OSError, or JSONDecodeError collapse to "all defaults".

    ``copy.deepcopy`` on the defaults container ensures the returned
    config (and its caller-mutable ``minikube`` dict) cannot leak into
    ``DEFAULT_MINIKUBE`` if the caller e.g. appends to ``addons``.

    Inner-key coercion also runs here, not just on ``save()``: a user may
    read settings (e.g. ``KubbyAPI.get_minikube_settings``) without ever
    persisting them, and we don't want a corrupt on-disk payload to reach
    ``minikube.start_args(...)`` in that read-without-write window.
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
    result = _deep_merge(defaults, data)
    # Defense in depth: even though save() also coerces, callers may read
    # without writing (e.g. prerequisites check before user-saves form).
    _coerce_minikube_in_settings(result)
    return result


def save(settings: dict[str, Any]) -> None:
    """Atomically write ``settings`` to ``CONFIG_FILE``.

    Coerces the minikube section to a valid shape (dict of correctly-typed
    keys) so a corrupt IPC payload can't clobber the file with
    half-broken data. The minikube block is replaced (via a shallow
    copy) rather than mutated in place, so callers holding their own
    reference to ``settings["minikube"]`` see their original object
    preserved verbatim.

    Raises ``OSError`` on filesystem errors and ``ValueError`` for
    non-dict inputs.

    Atomicity: write to a temp file in the same directory, then
    ``os.replace`` over the real path. Avoids a partial-file window if
    the process is killed mid-write.
    """
    if not isinstance(settings, dict):
        raise ValueError("settings must be a JSON object")
    _coerce_minikube_in_settings(settings)

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
