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

#: Widest workload label we will emit. A Deployment name plus a ready count
#: and a port; longer names are truncated rather than allowed to break a box.
MAX_LABEL = 22

#: A wider budget for the architecture boxes — the node's OS image, a
#: control-plane component's job. These are fixed strings of known length
#: rather than user-supplied names, so truncating them only ever loses
#: information ("Debian GNU/Linux 12 (book…" tells you less than the whole
#: thing) and the picture scrolls anyway.
MAX_DETAIL = 34

#: Subgraph labels are namespace names, and a namespace name can be longer
#: than the box it titles. Anything past this is cut with an ellipsis.
MAX_NS_LABEL = 18

#: Infrastructure namespaces still drawn box by box. The control plane is
#: the answer to "what am I looking at" — apiserver, etcd, scheduler and
#: controller-manager each say something a summary box cannot — so it is
#: the one place the picture is allowed to be tall. Other infrastructure
#: namespaces (the ingress controller, chiefly) stay collapsed, because
#: they are a consequence of a setup choice rather than something to learn.
EXPANDED_NAMESPACES = frozenset({"kube-system"})

#: Infrastructure namespaces left out of the picture entirely.
#:
#: The ingress controller's namespace is the machinery behind a *setup
#: choice* — enabling the addon — not part of the cluster's story. The part
#: that teaches something, the Ingress rule and the host it answers to, is
#: already drawn in the application's own namespace. Its three boxes and two
#: admission jobs were otherwise most of the picture.
#:
#: This is a judgement about what is worth showing, not a fact kubby can
#: establish: nothing in the API distinguishes this namespace from one
#: holding someone's application.
OMITTED_NAMESPACES = frozenset({"ingress-nginx"})

#: Services whose box is not drawn. The DNS Service is the only one: its box
#: and its edge say "DNS exists", which coredns's own label — "service names
#: to addresses" — already says better, in the same words, without spending
#: a box and an edge on it.
#:
#: The *Ingress* Service is a different matter and is never omitted: it is
#: where external traffic visibly lands.
UNSHOWN_SERVICES = frozenset({"kube-dns"})

#: Mermaid ids must be alphanumeric. Every real k8s name is already
#: dash-separated, so a bad id would mean a name we did not anticipate —
#: and a bad id corrupts the source rather than failing loudly.
_BAD_ID = re.compile(r"[^A-Za-z0-9_]")

#: Characters stripped from a label. Deliberately tiny: a quoted Mermaid
#: label was tested against quotes, brackets, braces, parens, pipes and
#: angle brackets, and termaid renders every one of them correctly. Only the
#: double quote (which could close the label) and the backslash (which is
#: the escape this module itself uses for line breaks) are removed.
#:
#: Stripping more than this is actively harmful. An earlier version also
#: removed brackets and parens, which turned a real OS string into
#: "Debian GNU/Linux 12 bookworm" — mangling the name to defend against
#: input that RFC 1123 makes impossible in the first place.
_BAD_LABEL = re.compile(r'["\\]')


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

    A control-plane component is labelled with what it *does* instead of a
    ready count. "kube-apiserver 1/1" tells a newcomer nothing; "every
    request goes through here" is the fact they came for.
    """
    entry = _control_plane_entry(pods[0]) if pods else None
    if entry is not None:
        name, role = entry
        return _label(name, role, MAX_DETAIL)

    ready = _ready_count(pods)
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
    """True when every pod in the workload is actually serving.

    Readiness, not phase: a container in ``CrashLoopBackOff`` or
    ``RunContainerError`` keeps its pod in phase ``Running`` until it gives
    up, so judging on phase paints a broken workload green. Pods with no
    ``ready`` key (a test double, or an older payload) fall back to the
    phase, which is the best that can be said about them.
    """
    for pod in pods:
        if "ready" in pod:
            if not pod["ready"]:
                return False
        elif pod.get("phase") not in ("Running", "Succeeded"):
            return False
    return True


def _ready_count(pods: list[dict[str, Any]]) -> int:
    return sum(
        1
        for p in pods
        if (p["ready"] if "ready" in p else p.get("phase") in ("Running", "Succeeded"))
    )


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
    #
    # The panel holds roughly ten boxes before the layout stops being
    # readable (measured: 9 boxes fit 59 columns, 24 came out at 151). So
    # namespaces are dropped deliberately, in the order of least value:
    # empty ones, then our own setup artefacts.
    namespaces = [
        ns
        for ns in all_namespaces
        if ns.get("pods") and ns["name"] not in OMITTED_NAMESPACES
    ]

    # The client. Traffic enters the cluster here, which is the single most
    # useful thing to show someone who has not seen one before.
    has_ingress = bool(cluster.get("ingresses"))
    if has_ingress:
        lines.append('  YOU["you\\n(browser)"]')

    service_ids: dict[tuple[str, str], str] = {}
    shown_services = [
        svc
        for svc in (cluster.get("services") or [])
        if svc["name"] not in UNSHOWN_SERVICES
    ]
    for svc in shown_services:
        service_ids[(svc["namespace"], svc["name"])] = _service_id(svc)

    # Same kind+name in two namespaces must not share a Mermaid id, or the
    # picture merges them into one box. The short id is kept when it is
    # unambiguous so the source stays readable (and stable for existing
    # goldens); only a real collision pays for the longer namespaced id.
    _kind_name_counts: dict[tuple[str, str], int] = {}
    for (_pns, _kind, _name) in workloads:
        _kind_name_counts[(_kind, _name)] = _kind_name_counts.get((_kind, _name), 0) + 1

    # The host and the node, above everything else. Emitted first so the
    # picture reads top-down: your computer, the machine minikube made on
    # it, and only then what is running inside.
    node_ids: dict[str, str] = {}
    _architecture_lines(lines, cluster, node_ids)

    for ns in namespaces:
        name = ns["name"]
        ns_services = [svc for svc in shown_services if svc["namespace"] == name]

        if ns.get("collapsed") and name not in EXPANDED_NAMESPACES:
            # Infrastructure, not the user's application. Drawn in full it is
            # most of the picture — an ingress controller alone is three
            # boxes and two admission jobs nobody asked about.
            count = ns.get("workload_count") or len(ns["pods"])
            box = _node_id("w", name, "collapsed")
            lines.append(f'  {box}["{_label(name, f"{count} workloads", MAX_DETAIL)}"]:::infra')
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
            entry["healthy"] = _is_healthy(entry["pods"])
            lines.append(f'    {box}["{_workload_label(entry, entry["pods"])}"]')

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
        lines.append(f'  {ing_id}["{_label(ing["name"], hosts, MAX_DETAIL)}"]:::ext')
        edge("YOU", ing_id)
        for backend in ing.get("backends") or []:
            sid = service_ids.get((backend["namespace"], backend["name"]))
            if sid is not None:
                edge(ing_id, sid)

    # Service -> the workload it actually backs.
    for svc in shown_services:
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

    # Node containment: which node actually runs what.
    #
    # Only drawn when there is more than one node. With a single node the
    # answer is the same for every workload, so seventeen edges from the
    # node box carry no information at all — they are pure layout cost, and
    # on a real cluster they were what turned the picture 385 columns wide.
    nodes = cluster.get("nodes") or []
    if len(nodes) > 1:
        for node in nodes:
            node_id = node_ids.get(node["name"])
            if node_id is None:
                continue
            for pod in pods:
                if pod.get("node") != node["name"]:
                    continue
                workload = pod["workload"]
                entry = workloads.get(
                    (pod["namespace"], workload["kind"], workload["name"])
                )
                if entry and entry.get("id"):
                    edge(node_id, entry["id"])

    lines.extend(edges)
    _colour_by_health(lines, workloads)
    _append_classes(lines)
    return "\n".join(lines) + "\n"


def _colour_by_health(
    lines: list[str], workloads: dict[tuple[str, str, str], dict[str, Any]]
) -> None:
    """Tag each workload box healthy or not, in place.

    Separate from emission because a workload's health is only known once
    every namespace has been walked, and a ``classDef`` is clearer applied
    as a second pass than threaded through the emit loop.
    """
    for entry in workloads.values():
        if "id" not in entry or "healthy" not in entry:
            continue
        cls = "ok" if entry["healthy"] else "bad"
        for index, line in enumerate(lines):
            if line.strip().startswith(f'{entry["id"]}["'):
                lines[index] = line.rstrip() + f":::{cls}"
                break


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
        out = {
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
        # Absent rather than defaulted to False: a pod dict with no `ready`
        # key is one we did not measure, and inventing `False` would paint
        # every such workload red. `_is_healthy` falls back to the phase.
        if "ready" in pod:
            out["ready"] = bool(pod["ready"])
        return out

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


#: Control-plane components, and what each one is *for*. The picture shows
#: one box per component; a beginner looking at ``kube-apiserver`` and
#: ``etcd`` for the first time is looking at two names with no meaning, and
#: the meaning is the whole reason to draw them separately rather than as
#: one "control plane" box.
CONTROL_PLANE_ROLES = {
    "kube-apiserver": "every request passes through",
    "etcd": "all cluster state lives here",
    "kube-scheduler": "picks the node for a pod",
    "kube-controller-manager": "keeps reality as you asked",
    "kube-proxy": "programs the pod network",
    "coredns": "service names to addresses",
    "storage-provisioner": "creates volumes on demand",
}


def _control_plane_entry(pod: dict[str, Any]) -> tuple[str, str] | None:
    """Classify a kube-system pod as a control-plane component, or ``None``.

    Matched on a name prefix rather than a label, because the static pods
    that make up a control plane are owned by the ``Node`` object and carry
    no label saying so. ``kindnet``-style network plugins would land here
    too, which is the intent: it *is* infrastructure, just not control
    plane.
    """
    workload = pod.get("workload") or {}
    name = str(workload.get("name") or pod.get("name") or "")
    for prefix in CONTROL_PLANE_ROLES:
        if name == prefix or name.startswith(f"{prefix}-"):
            return prefix, CONTROL_PLANE_ROLES[prefix]
    return None


def _architecture_lines(
    lines: list[str],
    cluster: dict[str, Any],
    node_ids: dict[str, str],
) -> None:
    """Draw the host and the node the workloads actually run inside.

    This is the part that answers "what am I even looking at". A cluster
    is not abstract: it is a machine, on your machine, running a real
    operating system with a fixed slice of CPU and memory, and everything
    the user deploys is a process inside it. Drawing that makes the
    resource limits and the ``Pending`` pods that follow from them legible
    instead of mysterious.
    """
    facts = cluster.get("node_facts") or []
    if not facts:
        return

    host_id = "host_your_computer"
    # The driver is how minikube runs that machine. "auto" is a real
    # setting and is labelled as such rather than silently dropped.
    driver = str(cluster.get("driver") or "").strip()
    driver_text = f"minikube, {driver} driver" if driver else "minikube, auto driver"
    lines.append(f'  {host_id}["{_label("your computer", driver_text, MAX_DETAIL)}"]:::host')

    for fact in facts:
        node_id = _node_id("node", fact["name"])
        node_ids[fact["name"]] = node_id
        # Four lines: the node's name, the OS it runs, the container runtime
        # its pods are processes on, and what it can actually hand out. A
        # Pod is a container on that runtime, not a VM in the abstract, and
        # this is the box that says so. The capacity line lives *in* the
        # node rather than in a box of its own, because a separate box
        # costs two more nodes and two more edges for a fact that belongs to
        # the node — and the edges were making the routing unreadable.
        label = _safe_label(fact["name"], MAX_LABEL)
        for extra in (
            fact.get("os_image"),
            fact.get("runtime"),
            _capacity_text(fact),
        ):
            if extra:
                label += f"\\n{_safe_label(extra, MAX_DETAIL)}"
        lines.append(f'  {node_id}["{label}"]:::infra')

    for fact in facts:
        node_id = node_ids.get(fact["name"])
        if node_id:
            lines.append(f"  {host_id} --> {node_id}")


def _capacity_text(fact: dict[str, Any]) -> str:
    """``2 of 16 cpu, 2Gi of 15.6Gi`` — the allocatable share, and the whole.

    Both halves matter when they differ, and on a 2-CPU minikube on a
    16-CPU laptop they do: capacity reports 16 and allocatable reports 2,
    and only the second is what a Pod can ask for. Showing just the host's
    number is how people conclude the cluster is starved when it is not.

    When the two are equal the "of" form is noise, so it collapses to the
    single value.
    """
    from kubby.cluster import human_memory

    def _pair(allocatable: str, capacity: str, suffix: str = "") -> str:
        if not allocatable and not capacity:
            return ""
        if allocatable == capacity:
            return f"{allocatable}{suffix}"
        return f"{allocatable or '?'} of {capacity or '?'}{suffix}"

    parts = [
        text
        for text in (
            _pair(str(fact.get("allocatable_cpu") or ""), str(fact.get("capacity_cpu") or ""), " cpu"),
            _pair(
                human_memory(fact.get("allocatable_memory") or ""),
                human_memory(fact.get("capacity_memory") or ""),
            ),
        )
        if text
    ]
    return ", ".join(parts)


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
    lines.append(f'{indent}{sid}["{_label(svc["name"], body, MAX_DETAIL)}"]:::{cls}')


def _append_classes(lines: list[str]) -> None:
    """Append the class definitions, reusing kubby's palette.

    Colours come from the same family the rest of the UI uses, so a healthy
    workload looks healthy everywhere in the app. ``host`` and ``infra`` are
    deliberately dim: the host and the machinery are context for the user's
    own workloads, and should not compete with them for attention.
    """
    lines.extend(
        [
            "  classDef ok stroke:#3fb950,color:#3fb950",
            "  classDef bad stroke:#f85149,color:#f85149",
            "  classDef ext stroke:#58a6ff,color:#58a6ff",
            "  classDef manual stroke:#768390,color:#768390",
            "  classDef host stroke:#d2a8ff,color:#d2a8ff",
            "  classDef infra stroke:#8b949e,color:#8b949e",
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


def center(picture: Text, width: int) -> Text:
    """Centre a picture narrower than the space it has.

    Only when it actually fits: a picture wider than the panel is already
    scrolling, and centring it would push its left edge off the scroll
    origin, where it cannot be scrolled back to.

    Pads each line rather than wrapping in ``rich.align.Align``, so the
    result is still a ``Text`` like everything else here and can be measured
    and asserted on.
    """
    widest = _max_width(picture)
    if widest >= width:
        return picture
    pad = " " * ((width - widest) // 2)
    out = Text()
    for index, line in enumerate(picture.split(allow_blank=True)):
        if index:
            out.append("\n")
        out.append(pad)
        out.append_text(line)
    return out


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
