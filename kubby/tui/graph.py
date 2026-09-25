"""Rendering the cluster topology as a picture in the terminal.

The picture is a Mermaid flowchart handed to `termaid`, which lays it out
and draws it. This module owns three things and nothing else:

1. ``build_model``  — the cluster data becomes Mermaid source
2. ``render``       — Mermaid source becomes styled `rich` output
3. ``node_rects``   — where each box ended up, for the hjkl cursor

Keeping the Mermaid generation pure (no I/O, no termaid) is what makes the
picture testable: a golden string per cluster shape, reviewed in a diff.

Two termaid behaviours this module has to work around, both found by
spiking it against a live cluster rather than by reading its docs:

- A node label needs a ``\\n`` *escape* to become a second line. A real
  newline in the source is parsed as a second node, so half the graph
  silently disappears. Never emit a raw newline inside a label.
- A subgraph label wider than its box corrupts the border — it overwrites
  the top edge and steals a cell from a neighbouring subgraph. Hence
  ``_safe_label`` and the width guard in :func:`build_mermaid`.
"""

from __future__ import annotations

import re
from typing import Any

from rich.text import Text
from termaid import render_rich
from termaid.layout.grid import compute_layout
from termaid.parser import parse_flowchart

#: Widest node label we will emit. A Deployment name plus a ready count and
#: a port; longer names are truncated rather than allowed to break a box.
MAX_LABEL = 22

#: Subgraph labels are namespace names, and a namespace name can be longer
#: than the box it titles. Anything past this is cut with an ellipsis.
MAX_NS_LABEL = 18

#: Mermaid ids must be alphanumeric. Every real k8s name is already
#: dash-separated, so a bad id would mean a name we did not anticipate —
#: and a bad id corrupts the source rather than failing loudly.
_BAD_ID = re.compile(r"[^A-Za-z0-9_]")

#: Characters that would end a Mermaid label early or open a shape, and so
#: spill the rest of the name into the diagram as source. Dashes, dots,
#: slashes, colons, spaces and commas are all fine in a label and are kept:
#: sanitising a *label* with the id rule turns `kube-system` into
#: `kube_system` and `53, 9153` into `53__9153`, which is worse than
#: useless — it misreports the name.
_BAD_LABEL = re.compile(r'["\[\]{}()`|<>\\`]')


def _node_id(prefix: str, *parts: str) -> str:
    """A stable, Mermaid-safe id for a cluster object.

    Stable matters: the cursor maps rectangles back to cluster objects by id,
    so a random or position-dependent id would lose the selection on every
    refresh.
    """
    joined = "_".join(p for p in parts if p)
    return f"{prefix}_{_BAD_ID.sub('_', joined)}"


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _safe_label(text: str, limit: int) -> str:
    """Make *text* safe to use as one line of a Mermaid label.

    Only the characters that would break out of the label are removed. A
    mangled name beats a corrupt picture, but there is no reason to mangle
    a name that was already fine.
    """
    cleaned = _BAD_LABEL.sub("", str(text or "?")).strip()
    return _truncate(cleaned or "?", limit)


def _label(name: str, detail: str, limit: int = MAX_LABEL) -> str:
    """A two-line node label: name over detail, newline-escaped.

    Two lines rather than one because termaid hard-wraps a long single line
    inside its fixed box width, and a wrapped label visibly breaks the box
    borders. The escape is assembled *after* sanitising, so it is never
    stripped. See the module docstring.
    """
    return f"{_safe_label(name, limit)}\\n{_safe_label(detail, limit)}"


def _workload_label(workload: dict[str, Any], pods: list[dict[str, Any]]) -> str:
    """Label a workload box: its name, its ready count, and its port.

    The count comes from the pods rather than the workload's status, because
    the pods are always here while the status is only there for a workload
    kubby happens to know about — and a pod's own phase is the more direct
    answer to "is this thing up" anyway. A pod still pulling or crashlooping
    is exactly what makes a box red, so it must not be counted as ready.
    """
    ready = sum(1 for p in pods if p.get("phase") in ("Running", "Succeeded"))
    detail = f"{ready}/{len(pods)}"

    ports = sorted(
        {
            str(p.get("target") or p.get("port"))
            for svc in workload.get("services", [])
            for p in svc.get("ports", [])
            if p.get("port")
        }
    )
    if ports:
        detail = f"{detail}  {ports[0]}"
    return _label(workload["name"], detail)


def _is_healthy(pods: list[dict[str, Any]]) -> bool:
    return all(p.get("phase") in ("Running", "Succeeded") for p in pods)


def build_mermaid(cluster: dict[str, Any], direction: str = "TB") -> str:
    """Turn a cluster model into Mermaid flowchart source.

    Pure: the same model always produces the same source, so a golden test
    is a real regression test. Returns a minimal valid diagram (never an
    empty string) for an empty or unavailable cluster, because termaid
    renders nothing at all for an empty graph.

    *direction* is the caller's choice, not this module's: a tall narrow
    panel wants ``LR`` (it overflows downwards, which scrolls naturally) and
    a wide one wants ``TB``. On the same 13-node cluster, ``TB`` came out
    42 lines by 151 columns and ``LR`` 63 by 72 — neither fits a small
    panel, but a picture that is too *tall* is a far better failure than one
    that is too wide.
    """
    if not cluster.get("available", True):
        reason = _safe_label(cluster.get("error") or "no cluster", MAX_NS_LABEL * 2)
        return f"graph {direction}\n  NONE[\"{reason}\"]\n"

    cluster = _normalise(cluster)
    all_namespaces = cluster.get("namespaces") or []
    pods = [pod for ns in all_namespaces for pod in ns.get("pods") or []]
    if not all_namespaces and not pods:
        return f"graph {direction}\n  NONE[\"no workloads\"]\n"

    # Index pods by identity so a service's backing pods can find their box.
    pod_by_key = {(p["namespace"], p["name"]): p for p in pods}

    # Group pods into workloads, preserving first-seen order so the picture
    # is stable across refreshes rather than reshuffling on every R.
    workloads: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pod in pods:
        workload = pod["workload"]
        key = (pod["namespace"], workload["kind"], workload["name"])
        entry = workloads.setdefault(
            key,
            {
                "name": workload["name"],
                "kind": workload["kind"],
                "namespace": pod["namespace"],
                "pods": [],
                "services": [],
            },
        )
        entry["pods"].append(pod)

    # Attach each service to the workloads it actually backs.
    for svc in cluster.get("services") or []:
        for backing in svc.get("backing_pods") or []:
            pod = pod_by_key.get((backing["namespace"], backing["pod"]))
            if pod is None:
                continue
            workload = pod["workload"]
            key = (pod["namespace"], workload["kind"], workload["name"])
            if key in workloads:
                workloads[key]["services"].append(svc)

    lines: list[str] = [f"graph {direction}"]
    #: Every edge, deduplicated. termaid routes each parallel edge
    #: separately, so seven pods on one node meant seven near-identical
    #: lines strung across the diagram and a picture three screens tall.
    edges: list[str] = []
    seen_edges: set[str] = set()

    def edge(src: str, dst: str) -> None:
        if src == dst:
            return
        key = f"{src}->{dst}"
        if key not in seen_edges:
            seen_edges.add(key)
            edges.append(f"  {src} --> {dst}")

    # A namespace with no pods has no wiring, and the picture is about
    # wiring. Drawing an empty box for kube-public and kube-node-lease on
    # every fresh cluster is noise, and the tree still lists them.
    namespaces = [ns for ns in all_namespaces if ns.get("pods")]

    # The client. Traffic enters the cluster here, which is the single most
    # useful thing to show someone who has not seen one before.
    has_ingress = bool(cluster.get("ingresses"))
    if has_ingress:
        lines.append('  YOU["you\\n(browser)"]')

    service_ids: dict[tuple[str, str], str] = {}
    for svc in cluster.get("services") or []:
        service_ids[(svc["namespace"], svc["name"])] = _service_id(svc)

    # Same kind+name in two namespaces must not share a Mermaid id, or the
    # picture merges them into one box. The short id is kept when it is
    # unambiguous so the source stays readable (and stable for existing
    # goldens); only a real collision pays for the longer namespaced id.
    _kind_name_counts: dict[tuple[str, str], int] = {}
    for (_pns, _kind, _name) in workloads:
        _kind_name_counts[(_kind, _name)] = _kind_name_counts.get((_kind, _name), 0) + 1

    for ns in namespaces:
        name = ns["name"]
        ns_services = [
            svc for svc in (cluster.get("services") or []) if svc["namespace"] == name
        ]

        if ns.get("collapsed"):
            # A plain node, deliberately not a subgraph: the namespace is one
            # summary box, so the wrapper adds no structure — and an edge
            # crossing a one-box subgraph's label row corrupts its border.
            # Its services stay visible, because on a fresh cluster the DNS
            # Service is the spine everything else resolves through.
            count = ns.get("workload_count") or len(ns["pods"])
            box = _node_id("w", name, "collapsed")
            lines.append(f'  {box}["{_label(name, f"{count} workloads", 24)}"]:::manual')
            for svc in ns_services:
                _declare_service(lines, service_ids[(name, svc["name"])], svc, "  ")
            continue

        ns_id = _node_id("ns", name)
        lines.append(f"  subgraph {ns_id} [{_safe_label(name, MAX_NS_LABEL)}]")
        for (pod_ns, kind, wl_name), entry in workloads.items():
            if pod_ns != name:
                continue
            if _kind_name_counts.get((kind, wl_name), 1) > 1:
                box = _node_id("w", pod_ns, kind, wl_name)
            else:
                box = _node_id("w", kind, wl_name)
            entry["id"] = box
            cls = "ok" if _is_healthy(entry["pods"]) else "bad"
            lines.append(f'    {box}["{_workload_label(entry, entry["pods"])}"]:::{cls}')

        # Services live in their own namespace's box. Floating them at the
        # top level made every service-to-pod edge leave its namespace and
        # come back, which is what turned the routing into a rat's nest.
        for svc in ns_services:
            _declare_service(lines, service_ids[(name, svc["name"])], svc, "    ")
        lines.append("  end")

    # External traffic in, through the Ingress, to the service it names.
    for ing in cluster.get("ingresses") or []:
        ing_id = _node_id("ing", ing["namespace"], ing["name"])
        hosts = ", ".join(sorted({r["host"] for r in ing.get("rules") or []})) or "*"
        lines.append(f'  {ing_id}["{_label(ing["name"], hosts, 26)}"]:::ext')
        edge("YOU", ing_id)
        for backend in ing.get("backends") or []:
            sid = service_ids.get((backend["namespace"], backend["name"]))
            if sid is not None:
                edge(ing_id, sid)

    # Service -> the workload it actually backs.
    for svc in cluster.get("services") or []:
        if not svc.get("backing_pods"):
            continue
        sid = service_ids[(svc["namespace"], svc["name"])]
        for backing in svc["backing_pods"]:
            pod = pod_by_key.get((backing["namespace"], backing["pod"]))
            if pod is None:
                continue
            workload = pod["workload"]
            entry = workloads.get((pod["namespace"], workload["kind"], workload["name"]))
            if entry is None:
                continue
            if entry.get("id"):
                edge(sid, entry["id"])
            else:
                # Its namespace is collapsed, so the edge stops at the
                # namespace's summary box rather than pointing at nothing.
                edge(sid, _node_id("w", pod["namespace"], "collapsed"))

    # Node containment: which node actually runs what. One edge per
    # workload on that node, not one per pod.
    for node in cluster.get("nodes") or []:
        host = [p for p in pods if p.get("node") == node["name"]]
        if not host:
            continue
        node_id = _node_id("node", node["name"])
        lines.append(f'  {node_id}["{_label(node["name"], f"{len(host)} pods", 24)}"]')
        for pod in host:
            workload = pod["workload"]
            entry = workloads.get((pod["namespace"], workload["kind"], workload["name"]))
            target = entry.get("id") if entry and entry.get("id") else None
            if target is None:
                target = _node_id("w", pod["namespace"], "collapsed")
            edge(node_id, target)

    lines.extend(edges)
    _append_classes(lines)
    return "\n".join(lines) + "\n"


def _normalise(cluster: dict[str, Any]) -> dict[str, Any]:
    """Fill in every field the renderer reads, so the rest can index freely.

    The model comes from `kubby.cluster`, but it also comes from test
    doubles and from callers holding a partial view of a cluster, and a
    picture that raises on a missing key is a refresh that takes the TUI
    down. One place that states the input contract beats thirty `.get()`
    calls that hide which fields actually matter.
    """
    def _pod(pod: dict[str, Any]) -> dict[str, Any]:
        workload = pod.get("workload") or {}
        return {
            **pod,
            "name": pod.get("name") or "?",
            "namespace": pod.get("namespace") or "default",
            "phase": pod.get("phase") or pod.get("status") or "Unknown",
            "node": pod.get("node") or "",
            "workload": {
                "name": workload.get("name") or pod.get("name") or "?",
                "kind": workload.get("kind") or "Pod",
                "inferred": bool(workload.get("inferred")),
            },
        }

    namespaces = []
    for ns in cluster.get("namespaces") or []:
        namespaces.append(
            {
                **ns,
                "name": ns.get("name") or "default",
                "pods": [_pod(p) for p in ns.get("pods") or []],
                "workload_count": ns.get("workload_count") or 0,
                "collapsed": bool(ns.get("collapsed")),
            }
        )

    services = [
        {
            **svc,
            "name": svc.get("name") or "?",
            "namespace": svc.get("namespace") or "default",
            "has_selector": bool(svc.get("has_selector")),
            "ports": svc.get("ports") or [],
            "backing_pods": svc.get("backing_pods") or [],
        }
        for svc in cluster.get("services") or []
    ]

    ingresses = [
        {
            **ing,
            "name": ing.get("name") or "?",
            "namespace": ing.get("namespace") or "default",
            "rules": ing.get("rules") or [],
            "backends": ing.get("backends") or [],
        }
        for ing in cluster.get("ingresses") or []
    ]

    return {
        **cluster,
        "nodes": cluster.get("nodes") or [],
        "namespaces": namespaces,
        "services": services,
        "ingresses": ingresses,
    }


def _service_id(svc: dict[str, Any]) -> str:
    return _node_id("svc", svc["namespace"], svc["name"])


def _declare_service(
    lines: list[str], sid: str, svc: dict[str, Any], indent: str
) -> None:
    """Emit one service's box, coloured by what its endpoints say.

    Three cases, and the difference matters: a service with backing pods is
    fine, one with a selector that matched nothing is the classic beginner
    mistake, and one with no selector at all is usually the API server's own
    Service or a hand-managed Endpoints — healthy, and reporting it as
    broken would condemn the healthiest thing in the cluster.
    """
    ports = sorted({str(p.get("port")) for p in svc.get("ports") or [] if p.get("port")})
    port_text = ", ".join(ports) if ports else "no ports"
    if svc.get("backing_pods"):
        body, cls = port_text, "ok"
    elif svc.get("has_selector"):
        body, cls = "0 endpoints", "bad"
    else:
        body, cls = port_text, "manual"
    lines.append(f'{indent}{sid}["{_label(svc["name"], body, 24)}"]:::{cls}')


def _append_classes(lines: list[str]) -> None:
    """Append the class definitions, reusing kubby's palette.

    Colours come from the same family the rest of the UI uses, so a healthy
    workload looks healthy everywhere in the app.
    """
    lines.extend(
        [
            "  classDef ok stroke:#3fb950,color:#3fb950",
            "  classDef bad stroke:#f85149,color:#f85149",
            "  classDef ext stroke:#58a6ff,color:#58a6ff",
            "  classDef manual stroke:#768390,color:#768390",
        ]
    )


#: Compactness steps, tried in order until the picture fits *width*. Only
#: width: termaid's own auto-fit is width-only and reads the terminal size
#: from a tty, which is never true inside a Textual widget, so the same
#: knobs are driven here instead.
_COMPACTION = (
    {"gap": 2, "padding_x": 2, "padding_y": 0},
    {"gap": 1, "padding_x": 1, "padding_y": 0},
    {"gap": 1, "padding_x": 0, "padding_y": 0},
)


def render(source: str, width: int) -> Text:
    """Render Mermaid *source* to fit *width* columns, as styled Rich text.

    Falls back to the least compact rendering rather than raising: a picture
    that overflows its panel is a cosmetic problem, and a refresh that
    raises would take the whole TUI down with it.

    Width is the only thing compaction can help with — when the overflow is
    caused by many boxes side by side rather than by generous spacing, the
    result is still too wide. The panel scrolls for that.
    """
    best: Text | None = None
    for options in _COMPACTION:
        try:
            candidate = render_rich(source, **options)
        except Exception:  # noqa: BLE001 - never let a render kill the TUI
            break
        best = candidate
        if _max_width(candidate) <= width:
            return candidate
    if best is None:
        return Text("could not draw the cluster graph", style="dim")
    return best


def _retarget(source: str, direction: str) -> str:
    """The same diagram with its direction swapped.

    The direction is the only thing that changes between the two variants,
    so swapping the first line is enough — no need to keep two sources or
    re-run the generator.
    """
    lines = source.splitlines()
    if not lines or not lines[0].startswith("graph "):
        return source
    lines[0] = f"graph {direction}"
    return "\n".join(lines)


def _overflow(text: Text, width: int, height: int) -> tuple[int, int, int]:
    """Rank a rendering: fewer lines over, then fewer columns over, then
    smaller area over."""
    lines = text.plain.splitlines() or [""]
    over_w = max(0, _max_width(text) - width)
    over_h = max(0, len(lines) - height)
    return (int(over_w > 0) + int(over_h > 0), over_w + over_h, len(lines) * over_w)


def render_best(source: str, width: int, height: int) -> tuple[Text, str]:
    """Render both directions and keep whichever fits *width* x *height*.

    An earlier version picked the direction from the panel's aspect ratio,
    which sounds reasonable and is wrong: on a real 13-node cluster the
    panel-shaped region (about 62x24) is *wider* than it is tall, so the
    heuristic chose ``TB`` — 42 lines by 151 columns — when ``LR`` came out
    63 by 72. Neither fits, but LR overflows by far less, and what it does
    overflow is the axis that scrolls.

    Trying both costs two layouts (~150ms). That is invisible on a manual
    refresh and worth paying for a picture that fits, on a cluster whose
    size is not known in advance.
    """
    best: tuple[tuple[int, int, int], Text, str] | None = None
    for direction in ("TB", "LR"):
        try:
            candidate = render(_retarget(source, direction), width)
        except Exception:  # noqa: BLE001
            continue
        score = _overflow(candidate, width, height)
        if best is None or score < best[0]:
            best = (score, candidate, direction)
    if best is None:
        return Text("could not draw the cluster graph", style="dim"), "TB"
    return best[1], best[2]


def _max_width(text: Text) -> int:
    lines = text.split("\n")
    return max((len(line) for line in lines), default=0)


def node_rects(source: str) -> dict[str, tuple[int, int, int, int]]:
    """Where each box landed: ``{node_id: (x, y, width, height)}``.

    termaid has already done the layout work, so the hjkl cursor does not
    need a layout engine of its own — it needs this mapping and a "nearest
    box in that direction" search over it.
    """
    try:
        graph = parse_flowchart(source)
        layout = compute_layout(graph, 1, 0, 1)
    except Exception:  # noqa: BLE001
        return {}
    return {
        node_id: (p.draw_x, p.draw_y, p.draw_width, p.draw_height)
        for node_id, p in layout.placements.items()
    }
