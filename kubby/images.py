"""Image search utilities for kubby.

Public surface:
- ``search_local(query)`` — query local podman images
- ``search_ghcr(query, page, per_page)`` — search GitHub Container Registry
- ``get_ghcr_tags(owner, repo)`` — fetch tags for a GHCR image
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor


def search_local(query: str | None = None) -> list[dict]:
    """Return all local podman images, optionally filtered by ``query``.

    Returns an empty list if podman is not installed or fails.
    Each image dict has: name, tags, created, size, local=True.
    """
    try:
        result = subprocess.run(
            ["podman", "images", "--format", "json"],
            capture_output=True,
            text=True,
            timeout=15,
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


def get_ghcr_tags(owner: str, repo: str) -> list[str]:
    """Fetch tags for a public GHCR image at ``ghcr.io/{owner}/{repo}``.

    Uses the OCI distribution spec ``/v2/{name}/tags/list`` endpoint.
    Returns an empty list on any error (including auth-required images).
    """
    url = f"https://ghcr.io/v2/{owner}/{repo}/tags/list"
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "kubby")
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read())
    except Exception:
        return []

    tags = data.get("tags")
    if isinstance(tags, list):
        return tags
    return []


def search_ghcr(query: str, page: int = 1, per_page: int = 10) -> dict:
    """Search GitHub Container Registry for public container images.

    Uses the GitHub Search API with ``topic:container-image`` to find
    repositories that publish container images to GHCR. Unauthenticated
    — subject to 60 req/hr rate limit.

    Returns ``{"results": [...], "total_count": int, "has_more": bool}``.
    Each result has: name, owner, repo, description, stars, updated_at, tags, remote=True.
    """
    q = f"{query} topic:container-image"
    params = urllib.parse.urlencode(
        {"q": q, "sort": "stars", "order": "desc", "per_page": per_page, "page": page}
    )
    url = f"https://api.github.com/search/repositories?{params}"

    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("User-Agent", "kubby")

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {"results": [], "total_count": 0, "has_more": False}

    total = data.get("total_count", 0)
    items = data.get("items")
    if not isinstance(items, list):
        return {"results": [], "total_count": total, "has_more": False}

    # Filter valid items and collect (owner, repo, item) tuples for
    # concurrent tag fetching so we don't make sequential blocking HTTP calls.
    entries: list[tuple[str, str, dict]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        owner = (item.get("owner") or {}).get("login", "")
        repo = item.get("name", "")
        if not owner or not repo:
            continue
        entries.append((owner, repo, item))

    # Fetch tags concurrently via a bounded thread pool.  `get_ghcr_tags`
    # catches all errors internally, so exceptions will not propagate.
    with ThreadPoolExecutor(max_workers=4) as executor:
        all_tags = list(executor.map(lambda e: get_ghcr_tags(e[0], e[1]), entries))

    results: list[dict] = []
    for (owner, repo, item), tags in zip(entries, all_tags):
        results.append(
            {
                "name": f"ghcr.io/{owner}/{repo}",
                "owner": owner,
                "repo": repo,
                "description": item.get("description") or "",
                "stars": item.get("stargazers_count", 0),
                "updated_at": item.get("updated_at", ""),
                "tags": tags if tags else ["latest"],
                "remote": True,
            }
        )

    has_more = len(results) == per_page and (page * per_page) < total
    return {"results": results, "total_count": total, "has_more": has_more}


