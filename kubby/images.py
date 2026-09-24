"""Image search utilities for kubby.

Public surface:
- ``search_local(query, env)`` — query local podman images

The GHCR/registry search that used to live here went away with the web
GUI: the TUI shows local podman images only.
"""

from __future__ import annotations

import json
import subprocess


def search_local(
    query: str | None = None, env: dict[str, str] | None = None
) -> list[dict]:
    """Return all local podman images, optionally filtered by ``query``.

    Returns an empty list if podman is not installed or fails.
    Each image dict has: name, tags, created, size, local=True.

    *env* is forwarded to ``subprocess.run`` so callers can sanitize the
    subprocess environment (e.g. strip PyInstaller's bundled
    ``LD_LIBRARY_PATH``).
    """
    try:
        result = subprocess.run(
            ["podman", "images", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    try:
        images = json.loads(result.stdout) if result.stdout.strip() else []
    except json.JSONDecodeError:
        return []

    if not isinstance(images, list):
        return []

    out: list[dict] = []
    for img in images:
        if not isinstance(img, dict):
            continue
        names = img.get("Names") or img.get("names") or []
        name = names[0] if names else (img.get("Id", "") or "")[:12]
        tags = img.get("Tags") or img.get("tags") or []
        # When the top-level Tags list is absent (newer podman JSON format),
        # try the per-image Tag field, then fall back to extracting the tag
        # from Names[0] (e.g. "nginx:alpine") so we never report "latest"
        # for an image that has a real tag.
        if not tags:
            explicit_tag = img.get("Tag") or img.get("tag")
            if explicit_tag and explicit_tag != "<none>":
                tags = [explicit_tag]
            elif name and ":" in name and "/" not in name.rsplit(":", 1)[-1]:
                tags = [name.rsplit(":", 1)[-1]]
        created = img.get("Created") or img.get("created") or ""
        size = img.get("Size") or img.get("size") or ""

        # Filter by query if provided
        if query and query.strip():
            q = query.strip().lower()
            matches = q in name.lower() or any(q in t.lower() for t in tags)
            if not matches:
                continue

        out.append(
            {
                "name": name,
                "tags": tags if tags else ["latest"],
                "created": created,
                "size": size,
                "local": True,
            }
        )

    return out
