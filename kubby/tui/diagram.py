"""What the picture shows, as data.

A step up from Mermaid source. The builder used to emit a flowchart string
and hand it to a library, which meant the only description of the picture
was a string meant for something else. This is the picture's own model:
boxes with labels and meanings, edges between them, and the grouping the
layout needs. It is pure, so it is directly testable, and it is the only
place that decides *what* is drawn — :mod:`kubby.tui.place` decides where.

Why not a library's layout: the graph here is mostly *disconnected*. With
the control plane's components unconnected to each other and the node's
edges gone, it is around fifteen separate components, and a layered layout
puts each one in its own column — 301 columns wide for twenty boxes. A
general-purpose layered algorithm is the wrong tool for a picture that is
really a handful of small groups.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: What a box *is*, which decides its colour and its border weight. Ordered
#: by how early it should appear in the picture: the reader should meet the
#: machine, then what runs on it, then their own workloads.
#:
#: There is deliberately no "client" role. The picture represents what is
#: running on the cluster, and a browser on the other end of a request is
#: not that — drawing it made the picture an application architecture
#: diagram, which is a different thing and a misleading one here.
ROLES = ("host", "node", "infra", "app")

ROLE_STYLE = {
    "host": "#d2a8ff",
    "node": "#8b949e",
    "infra": "#8b949e",
    "app": "",
}

#: Control-plane components and what each one is for. A beginner looking at
#: `kube-apiserver` and `etcd` for the first time is looking at two names
#: with no meaning, and the meaning is the reason to draw them at all.
INFRA_ROLES = {
    "kube-apiserver": "every request passes through",
    "etcd": "all cluster state lives here",
    "kube-scheduler": "picks the node for a pod",
    "kube-controller-manager": "keeps reality as you asked",
    "kube-proxy": "programs the pod network",
    "coredns": "service names to addresses",
    "storage-provisioner": "creates volumes on demand",
}

#: Namespaces that are cluster infrastructure rather than anyone's
#: application. A naming convention, not a fact the API establishes —
#: nothing marks a namespace as infrastructure.
INFRA_NAMESPACES = frozenset(
    {"kube-system", "kube-public", "kube-node-lease", "ingress-nginx"}
)

#: Where the control plane actually lives. The apiserver, etcd, scheduler
#: and controller-manager run as *static pods* on the node, managed by the
#: kubelet rather than by a Deployment or a ReplicaSet — which is why they
#: have no replicas to count and why grouping them with ordinary workloads
#: would be misleading. Their mirror pods appear in kube-system, so kubby
#: finds them there, but they are not part of what that namespace is for.
CONTROL_PLANE_NAMESPACE = frozenset({"kube-system"})

#: Namespaces left out of the picture entirely. Only the ingress controller's:
#: see the note where the namespaces are filtered.
OMITTED_NAMESPACES = frozenset({"ingress-nginx"})


@dataclass
class Node:
    """One box."""

    id: str
    lines: list[str]
    role: str
    group: str
    #: False when the workload is not actually serving. Decided on container
    #: readiness, not pod phase: a container in CrashLoopBackOff keeps its
    #: pod in phase `Running` until it finally gives up, and a broken box
    #: drawn healthy is the one thing the picture must never do.
    healthy: bool = True
    #: Set for a Service whose selector matched nothing — the single most
    #: common beginner mistake, and the one worth colouring red.
    broken_service: bool = False


@dataclass
class Container:
    """Something that *contains* other things, drawn as a box around them.

    The distinction from an edge is the whole point. A namespace contains its
    workloads, so it is a box they sit inside; a Service points at the
    workload it backs, so it is a line between them. Drawing containment as
    adjacency — a heading above a scatter of boxes — makes the two look like
    the same kind of fact, and makes the picture sprawl: a reader has to
    infer the grouping from proximity.

    Containers nest, and the nesting is the point. From the minikube
    documentation: minikube creates a VM or container *on your machine*, and
    that VM *is* the node; everything else runs inside it. So the shape is
    your computer → minikube → control plane and namespaces, and not three
    things side by side at the top level.
    """

    id: str
    label: str
    members: list[str] = field(default_factory=list)
    #: The container this one sits inside, or None for a top-level one.
    parent: str | None = None
    #: Reading order among siblings, lowest first.
    rank: int = 0
    #: A second line under the label in the frame's top edge — the sort of
    #: thing a box is too narrow to hold without truncating it.
    detail: str = ""


@dataclass
class Diagram:
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    containers: list[Container] = field(default_factory=list)
    _seen: set[tuple[str, str]] = field(default_factory=set, repr=False)

    def add(self, node: Node) -> None:
        self.nodes[node.id] = node

    def contain(self, container: Container) -> None:
        self.containers.append(container)

    def container_of(self, node_id: str) -> Container | None:
        for container in self.containers:
            if node_id in container.members:
                return container
        return None

    def link(self, source: str, target: str) -> None:
        """Record an edge, once.

        Deduplicated because a Service with three replicas links to the same
        workload three times — they all resolve to one Deployment — and two
        arrows drawn on top of each other read as a heavier arrow rather
        than as the mistake it is.
        """
        if source not in self.nodes or target not in self.nodes or source == target:
            return
        if (source, target) in self._seen:
            return
        self._seen.add((source, target))
        self.edges.append((source, target))

    # ----- shape -------------------------------------------------------

    def groups(self) -> list[tuple[str, list[str]]]:
        """Boxes by the container they are inside, in reading order."""
        return [(c.label, list(c.members)) for c in self.ordered_containers()]

    def children_of(self, container_id: str | None) -> list[Container]:
        return sorted(
            (c for c in self.containers if c.parent == container_id),
            key=lambda c: (c.rank, c.label),
        )

    def ordered_containers(self) -> list[Container]:
        """Top-level containers, in reading order: the machine, then what
        runs on it.

        Ties break on the label so the order is stable across refreshes — a
        picture that reshuffles every `R` is impossible to build a mental
        model of.
        """
        return self.children_of(None)

    def components(self) -> list[list[str]]:
        """Weakly-connected sets, used for ordering boxes within a
        container."""
        parent = {node: node for node in self.nodes}

        def find(node: str) -> str:
            while parent[node] != node:
                parent[node] = parent[parent[node]]
                node = parent[node]
            return node

        for source, target in self.edges:
            a, b = find(source), find(target)
            if a != b:
                parent[a] = b

        buckets: dict[str, list[str]] = {}
        for node in self.nodes:
            buckets.setdefault(find(node), []).append(node)
        return list(buckets.values())

    def role_of(self, node: str) -> str:
        return self.nodes[node].role


def _infra_name(name: str) -> str | None:
    for prefix in INFRA_ROLES:
        if name == prefix or name.startswith(f"{prefix}-"):
            return prefix
    return None


def build_diagram(cluster: dict[str, Any]) -> Diagram:
    """Turn a cluster model into the boxes and edges to draw.

    Order of business, and it is the order the picture reads in: the machine
    you are on, the machine minikube made on it, what runs on that, and then
    your own workloads and the services in front of them.
    """
    from kubby.cluster import human_memory

    diagram = Diagram()
    if not cluster.get("available", True):
        return diagram

    namespaces = [
        ns
        for ns in (cluster.get("namespaces") or [])
        # An empty namespace has no wiring to show, and the ingress
        # controller's namespace is the machinery behind enabling an addon
        # rather than part of the cluster's story — four boxes, three of them
        # admission webhooks, and the part that teaches something (the rule
        # and the host it answers to) is already in the app's own group.
        if ns.get("pods") and ns.get("name") not in OMITTED_NAMESPACES
    ]
    pods = [pod for ns in namespaces for pod in ns.get("pods") or []]
    if not namespaces and not pods and not cluster.get("services") and not cluster.get("ingresses"):
        return diagram

    # --- the host, then minikube, then the node ---------------------------
    # From the minikube documentation: `minikube start` creates a VM or a
    # container *on your machine*, and that VM *is* the cluster's node.
    # Everything else — the control plane, DNS, your own workloads — runs
    # inside it. So the nesting is
    #
    #     your computer  ->  minikube  ->  control plane, namespaces
    #
    # and not three things side by side. minikube and the node are one box
    # because they are one machine; the node is simply the Kubernetes name
    # for the thing minikube made.
    facts = cluster.get("node_facts") or []
    driver = str(cluster.get("driver") or "").strip()
    driver_text = f"{driver} driver" if driver else "auto driver"

    diagram.contain(
        Container(
            id="computer",
            label="your computer",
            detail=driver_text,
            rank=0,
        )
    )

    if facts:
        detail = str(cluster.get("version") or "")
        if facts[0].get("internal_ip"):
            detail = f"{detail} · {facts[0]['internal_ip']}".strip(" ·")

        # The machine's own details go in a box inside the frame, not in the
        # frame's title. A frame is only as wide as what it contains, and a
        # 68-character title forced minikube out to 104 columns — wider than
        # the panel it was supposed to fit in.
        machine: list[str] = []
        for fact in facts:
            lines = [str(fact.get("name") or "the machine")]
            for extra in (
                fact.get("os_image"),
                fact.get("runtime"),
                _capacity_text(fact, human_memory),
                f"pod addresses from {fact['pod_cidr']}" if fact.get("pod_cidr") else "",
            ):
                if extra:
                    lines.append(str(extra))
            node_id = _node_id(str(fact.get("name") or "?"))
            diagram.add(Node(node_id, lines, "node", "minikube"))
            machine.append(node_id)

        # One node *is* minikube, so its box is a loose box of the minikube
        # frame. With several it needs a frame of its own — otherwise the
        # boxes would sit alongside the namespaces and look like peers of
        # them, which they are not.
        diagram.contain(
            Container(
                id="minikube",
                label="minikube",
                parent="computer",
                detail=detail,
                rank=0,
                members=machine if len(machine) == 1 else [],
            )
        )
        if len(machine) > 1:
            diagram.contain(
                Container(
                    id="nodes",
                    label="the nodes",
                    members=machine,
                    parent="minikube",
                    rank=0,
                )
            )

    # --- workloads, grouped into namespaces -----------------------------
    workloads: dict[tuple[str, str, str], dict[str, Any]] = {}
    for pod in pods:
        workload = pod.get("workload") or {}
        name = str(workload.get("name") or pod.get("name") or "?")
        kind = str(workload.get("kind") or "Pod")
        key = (str(pod.get("namespace") or "default"), kind, name)
        entry = workloads.setdefault(
            key,
            {"name": name, "kind": kind, "namespace": key[0], "pods": [], "infra": None},
        )
        entry["pods"].append(pod)
        if entry["infra"] is None:
            entry["infra"] = _infra_name(name)

    for (namespace, kind, name), entry in workloads.items():
        pods_in = entry["pods"]
        infra = entry["infra"]
        if infra:
            # Infrastructure is labelled with what it does, not a ready count.
            # "kube-apiserver 1/1" teaches nothing; "every request passes
            # through" is the fact the reader came for.
            lines = [infra, INFRA_ROLES[infra]]
            role = "infra"
        else:
            ready = sum(1 for p in pods_in if _ready(p))
            lines = [name, f"{ready}/{len(pods_in)}"]
            role = "infra" if namespace in INFRA_NAMESPACES else "app"
        diagram.add(
            Node(
                _workload_id(namespace, kind, name),
                lines,
                role,
                namespace,
                healthy=all(_ready(p) for p in pods_in),
            )
        )
        # No edge from the node to the control plane, even though the
        # connection is real. Seven of them became a picket fence down one
        # lane, and the reading order already says it: the node's box, then
        # the control plane directly beneath it.

    # --- services, in front of the workloads they back --------------------
    # Scoped to the namespaces that survived the filter above: a Service in
    # an omitted namespace has no pods left to point at, so drawing it would
    # put a box in the picture that leads nowhere.
    kept = {
        str(ns.get("name") or "default")
        for ns in (cluster.get("namespaces") or [])
        if str(ns.get("name") or "default") not in OMITTED_NAMESPACES
    }
    for svc in cluster.get("services") or []:
        if str(svc.get("namespace") or "default") not in kept:
            continue
        if svc.get("name") == "kube-dns":
            # coredns's own label already says what DNS does, in the same
            # words. This box and its edge would say it less well.
            continue
        namespace = str(svc.get("namespace") or "default")
        ports = sorted({str(p.get("port")) for p in svc.get("ports") or [] if p.get("port")})
        backing = svc.get("backing_pods") or []
        if backing:
            detail = ", ".join(ports) if ports else "no ports"
            healthy = True
        elif svc.get("has_selector"):
            # A selector that matched nothing. The red box worth having.
            detail, healthy = "0 endpoints", False
        else:
            # No selector is not broken: the API server's own Service has
            # none, and so does one fronting hand-managed Endpoints.
            detail, healthy = (", ".join(ports) if ports else "no ports"), True
        svc_id = _service_id(namespace, str(svc.get("name") or "?"))
        diagram.add(
            Node(
                svc_id,
                [str(svc.get("name") or "?"), detail],
                "infra" if namespace in INFRA_NAMESPACES else "app",
                namespace,
                healthy=healthy,
                broken_service=not backing and bool(svc.get("has_selector")),
            )
        )
        for pod_ref in backing:
            pod = next(
                (
                    p
                    for p in pods
                    if p.get("name") == pod_ref.get("pod")
                    and p.get("namespace") == pod_ref.get("namespace")
                ),
                None,
            )
            if pod is None:
                continue
            workload = pod.get("workload") or {}
            diagram.link(
                svc_id,
                _workload_id(
                    str(pod.get("namespace") or "default"),
                    str(workload.get("kind") or "Pod"),
                    str(workload.get("name") or pod.get("name") or "?"),
                ),
            )

    # --- ingress, which is a resource in the cluster like any other -------
    # An Ingress is a real object with a real backend, so it is drawn and
    # linked to the Service it routes to — that relationship is cluster
    # wiring. What is *not* drawn is the client on the other end: it is not
    # part of the cluster, and a picture of the cluster is not a picture of
    # an application architecture.
    for ing in cluster.get("ingresses") or []:
        hosts = ", ".join(sorted({r["host"] for r in ing.get("rules") or []})) or "*"
        namespace = str(ing.get("namespace") or "default")
        ing_id = _ingress_id(namespace, str(ing["name"]))
        diagram.add(
            Node(
                ing_id,
                [str(ing["name"]), hosts],
                "infra" if namespace in INFRA_NAMESPACES else "app",
                namespace,
            )
        )
        for backend in ing.get("backends") or []:
            diagram.link(
                ing_id,
                _service_id(
                    str(backend.get("namespace") or "default"),
                    str(backend.get("name") or ""),
                ),
            )

    # --- namespaces, as boxes inside minikube ----------------------------
    # Rank 1 for the control plane, then 2 for the cluster's own machinery,
    # then 3 for the reader's namespaces, so the picture reads top-down:
    # what makes the decisions, then what runs the cluster, then their code.
    for name in sorted(kept):
        members = sorted(
            node_id
            for node_id, node in diagram.nodes.items()
            if node.group == name
        )
        if not members:
            continue
        if name in CONTROL_PLANE_NAMESPACE:
            rank, parent = 1, "minikube"
        else:
            rank, parent = (2 if name in INFRA_NAMESPACES else 3), "minikube"
        diagram.contain(
            Container(
                id=f"ns:{name}",
                label=name,
                members=members,
                parent=parent,
                rank=rank,
                detail="static pods" if name in CONTROL_PLANE_NAMESPACE else "",
            )
        )

    return diagram


def _ready(pod: dict[str, Any]) -> bool:
    if "ready" in pod:
        return bool(pod["ready"])
    return pod.get("phase") in ("Running", "Succeeded")


def _capacity_text(fact: dict[str, Any], human_memory: Any) -> str:
    """``2 of 16 cpu, 2Gi of 15.6Gi`` — the share, and the whole.

    Only the share is what a Pod may ask for, and on a 2-CPU minikube on a
    16-CPU laptop the two differ sharply. Showing the host's figure alone is
    how someone concludes the cluster is starved when it is not. Equal
    values collapse rather than repeat.
    """
    def pair(allocatable: str, capacity: str, suffix: str = "") -> str:
        if not allocatable and not capacity:
            return ""
        if allocatable == capacity:
            return f"{allocatable}{suffix}"
        return f"{allocatable or '?'} of {capacity or '?'}{suffix}"

    parts = [
        text
        for text in (
            pair(str(fact.get("allocatable_cpu") or ""), str(fact.get("capacity_cpu") or ""), " cpu"),
            pair(
                human_memory(fact.get("allocatable_memory") or ""),
                human_memory(fact.get("capacity_memory") or ""),
            ),
        )
        if text
    ]
    return ", ".join(parts)


def _safe(text: str) -> str:
    return "".join(c for c in str(text or "?") if c not in '"\\[]{}()|<>`')


def _node_id(name: str) -> str:
    return "node_" + _safe(name).replace("-", "_").replace(".", "_")


def _workload_id(namespace: str, kind: str, name: str) -> str:
    return f"w_{_safe(namespace)}_{_safe(kind)}_{_safe(name)}".replace(" ", "_")


def _service_id(namespace: str, name: str) -> str:
    return f"svc_{_safe(namespace)}_{_safe(name)}".replace(" ", "_")


def _ingress_id(namespace: str, name: str) -> str:
    return f"ing_{_safe(namespace)}_{_safe(name)}".replace(" ", "_")
