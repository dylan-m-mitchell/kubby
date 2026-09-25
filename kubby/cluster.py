"""Cluster topology: turning raw ``kubectl -o json`` into a graph model.

Pure derivations, no I/O — :mod:`kubby.service` does the shelling out and
hands the parsed payloads here. Everything in this module is a function of
its arguments, so the whole model is testable without a cluster.

Why a separate module: the UI wants a *picture*, and the picture is only as
honest as the model underneath it. Keeping the derivations here, away from
both the subprocess layer and the renderer, is what makes that checkable.

What the model deliberately does **not** contain
-----------------------------------------------
Pod-to-pod traffic. It is not in the Kubernetes API and cannot be derived
from it — it needs CNI flow data or a metrics pipeline. The API tells us
what *is* wired, never what *did* talk. Drawing an inferred edge as though
it were observed is how a tool teaches someone something false, so the
picture shows real wiring and says in its legend that same-namespace
reachability is not shown.
"""

from __future__ import annotations

import re
from typing import Any

#: The label EndpointSlice carries naming the Service it backs. This is the
#: authoritative Service -> Pod link, and it is why the graph does not
#: reimplement Service selector matching (which silently disagrees with
#: reality whenever a selector is wrong or a manual Endpoint is added).
SERVICE_NAME_LABEL = "kubernetes.io/service-name"

#: Namespaces collapsed to a single box by default. On a fresh minikube the
#: control plane is six or more pods, which would otherwise be most of the
#: picture with the user's own application a small box off to one side.
COLLAPSED_NAMESPACES = frozenset({"kube-system", "kube-public", "kube-node-lease"})

#: ``web-7d764666f9-87k8q`` -> ``web``. Only a last resort: the ReplicaSet ->
#: Deployment link in ownerReferences is authoritative and is used when
#: available. Pods owned directly by a Node (the static control-plane pods)
#: have no such chain, and this recovers a readable name from them.
_HASH_SUFFIX = re.compile(r"-[a-z0-9]{6,10}(?:-[a-z0-9]{5})?$")


def _int_or(value: Any, default: int = 0) -> int:
    """Coerce a JSON number that might be a string or absent."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def human_memory(quantity: str) -> str:
    """``16313348Ki`` -> ``15.6Gi``.

    Kubernetes reports memory in binary units with no parsing helper
    anywhere in the API, and nobody reads raw Ki. Everything is converted
    through bytes first — scaling by the suffix alone gets the exponent
    wrong (``16313348Ki`` is 15.6Gi, not 15931Gi, which is what dividing
    once by 1024 and labelling it Gi gives you).
    """
    text = str(quantity or "").strip()
    if not text:
        return ""
    scales = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "Ti": 1024**4}
    for suffix, scale in scales.items():
        if not text.endswith(suffix):
            continue
        try:
            value = float(text[: -len(suffix)])
        except ValueError:
            return text
        total = value * scale
        for unit, size in (("Ti", 1024**4), ("Gi", 1024**3), ("Mi", 1024**2)):
            if total >= size:
                # `2Gi` not `2.0Gi`: a trailing zero is noise on a box label.
                return f"{total / size:.1f}{unit}".replace(".0", "")
        return f"{total:g}B"
    return text


def parse_node_facts(payload: Any) -> list[dict[str, Any]]:
    """Extract what a node *is*, as opposed to what runs on it.

    A person looking at a cluster for the first time wants to know that the
    node is a whole machine with its own OS, its own kernel, its own disk
    and a fixed share of CPU and memory — and that the share is usually far
    smaller than the host's. That is the explanation for a Pod stuck in
    ``Pending`` that says "insufficient cpu", and it is invisible in a list
    of pods.

    ``capacity`` vs ``allocatable`` is the pair worth keeping: on a 2-CPU
    minikube on a 16-CPU laptop, capacity reports 16 and allocatable
    reports 2, and only the second one is what a Pod can ask for.
    """
    facts: list[dict[str, Any]] = []
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name")
        if not name:
            continue
        status = item.get("status", {}) or {}
        info = status.get("nodeInfo", {}) or {}
        capacity = status.get("capacity", {}) or {}
        allocatable = status.get("allocatable", {}) or {}

        internal_ip = ""
        for address in status.get("addresses", []) or []:
            if address.get("type") == "InternalIP":
                internal_ip = address.get("address") or ""
                break

        roles: list[str] = []
        for label in meta.get("labels", {}) or {}:
            if label.startswith("node-role.kubernetes.io/"):
                role = label.split("/", 1)[1]
                if role:
                    roles.append(role)

        ready = "Unknown"
        for condition in status.get("conditions", []) or []:
            if condition.get("type") == "Ready":
                ready = "Ready" if condition.get("status") == "True" else "NotReady"

        facts.append(
            {
                "name": name,
                "status": ready,
                "roles": roles,
                "internal_ip": internal_ip,
                "os_image": info.get("osImage") or "",
                "kernel": info.get("kernelVersion") or "",
                "architecture": info.get("architecture") or "",
                "kubelet_version": info.get("kubeletVersion") or "",
                # e.g. "docker://29.2.1" -> the runtime minikube is using.
                "runtime": (info.get("containerRuntimeVersion") or "").replace(
                    "://", " "
                ).strip(),
                "pod_cidr": (item.get("spec", {}) or {}).get("podCIDR") or "",
                "capacity_cpu": capacity.get("cpu") or "",
                "allocatable_cpu": allocatable.get("cpu") or "",
                "capacity_memory": capacity.get("memory") or "",
                "allocatable_memory": allocatable.get("memory") or "",
            }
        )
    return facts


def parse_pods(payload: Any) -> list[dict[str, Any]]:
    """Extract the pod fields the graph needs, including node and owner.

    The tree's pod fetch does not need these, so they are parsed here rather
    than widening that payload for everyone.
    """
    pods: list[dict[str, Any]] = []
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        spec = item.get("spec", {})
        name = meta.get("name")
        if not name:
            continue
        owners = meta.get("ownerReferences") or []
        # Prefer the most specific owner. A pod can carry several; the
        # controller that actually manages it is the one worth naming.
        owner = owners[0] if owners else {}
        # `status` is what the tree has always called it; accept either so
        # the inventory's pods can be reused as they are. A status dict
        # without a phase string is "Unknown", never the dict itself.
        status_val = item.get("status")
        if isinstance(status_val, dict):
            phase = status_val.get("phase")
            phase = phase if isinstance(phase, str) and phase else "Unknown"
        elif isinstance(status_val, str) and status_val:
            phase = status_val
        else:
            phase = "Unknown"
        pods.append(
            {
                "name": name,
                "namespace": meta.get("namespace") or "default",
                "phase": phase,
                "node": spec.get("nodeName") or "",
                "ip": (spec.get("podIP") or ""),
                "owner_kind": owner.get("kind") or "",
                "owner_name": owner.get("name") or "",
            }
        )
    return pods


def parse_workloads(payload: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Index Deployments / StatefulSets / DaemonSets by ``(ns, name)``.

    Pods name a ReplicaSet as their owner, never a Deployment, so this
    table is what turns ``web-7d764666f9`` back into ``web`` in the picture.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        kind = item.get("kind") or ""
        status = item.get("status", {}) or {}
        if kind == "Deployment":
            ready, want = status.get("readyReplicas"), status.get("replicas")
        elif kind == "StatefulSet":
            ready, want = status.get("readyReplicas"), status.get("replicas")
        elif kind == "DaemonSet":
            ready = status.get("numberReady")
            want = status.get("desiredNumberScheduled")
        else:
            continue
        name = meta.get("name")
        if not name:
            continue
        out[(meta.get("namespace") or "default", name)] = {
            "kind": kind,
            "ready": _int_or(ready),
            "desired": _int_or(want),
        }
    return out


def parse_replica_sets(payload: Any) -> dict[tuple[str, str], dict[str, Any]]:
    """Index ReplicaSets by ``(ns, name)`` with the workload each fronts.

    A ReplicaSet owned by a Deployment carries that Deployment's name; one
    left over from a scale-down or a bad rollout is owned by nothing, and is
    reported as a bare ReplicaSet rather than being mislabelled.
    """
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name")
        if not name:
            continue
        owner = next(
            (
                o
                for o in (meta.get("ownerReferences") or [])
                if o.get("kind") in ("Deployment", "StatefulSet")
            ),
            None,
        )
        out[(meta.get("namespace") or "default", name)] = {
            "kind": owner.get("kind") if owner else "ReplicaSet",
            "name": owner.get("name") if owner else name,
        }
    return out


def resolve_workload(
    pod: dict[str, Any],
    workloads: dict[tuple[str, str], dict[str, Any]],
    replica_sets: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    """Name the thing that owns *pod*, in a form a human would recognise.

    Cases, in order of authority:
      1. The owner is a ReplicaSet and we know its Deployment -> that.
      2. The owner is a Deployment / StatefulSet / DaemonSet -> that.
      3. The owner is the **Node** -> the pod's own name. This is the
         static control-plane pattern: ``etcd``, ``kube-apiserver`` and
         friends are each owned by the Node object, so the owner says
         "minikube" about all of them and four unrelated pods would end up
         sharing one name.
      4. Anything else -> the owner name with its generated hash suffix
         stripped, so ``kube-apiserver-minikube`` does not become
         ``kube-apiserver-minikube-f7c9d``.
    """
    ns = pod.get("namespace") or "default"
    kind = pod.get("owner_kind") or ""
    owner = pod.get("owner_name") or ""

    if kind == "Node":
        return {
            "name": _strip_hash(str(pod.get("name") or owner or "?")),
            "kind": "Pod",
            "inferred": False,
        }

    if kind == "ReplicaSet" and owner:
        target = replica_sets.get((ns, owner))
        if target:
            return {"name": target["name"], "kind": target["kind"], "inferred": False}
        return {"name": _strip_hash(owner), "kind": "ReplicaSet", "inferred": False}

    if kind and owner:
        return {"name": owner, "kind": kind, "inferred": False}

    if owner:
        return {"name": _strip_hash(owner), "kind": "Pod", "inferred": True}
    return {"name": str(pod.get("name") or "?"), "kind": "Pod", "inferred": True}


def _strip_hash(name: str) -> str:
    """Drop a ReplicaSet/pod hash suffix, leaving a readable stem."""
    return _HASH_SUFFIX.sub("", name) or name


def parse_services(payload: Any) -> list[dict[str, Any]]:
    """Extract Service name, type, ports and whether it has a selector.

    A Service with no selector is not necessarily broken: the API server's
    own ``kubernetes`` Service is one, and so is any Service fronting
    manually-managed Endpoints. The flag lets the picture tell those apart
    from the case that actually matters — a selector that matches nothing.
    """
    services: list[dict[str, Any]] = []
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name")
        if not name:
            continue
        spec = item.get("spec", {}) or {}
        ports = [
            {
                "port": _int_or(p.get("port")),
                "target": str(p.get("targetPort") or ""),
                "node_port": _int_or(p.get("nodePort"), 0) or None,
            }
            for p in (spec.get("ports") or [])
        ]
        services.append(
            {
                "name": name,
                "namespace": meta.get("namespace") or "default",
                "type": spec.get("type") or "ClusterIP",
                "cluster_ip": spec.get("clusterIP") or "",
                "has_selector": bool(spec.get("selector")),
                "ports": ports,
            }
        )
    return services


def parse_endpoint_slices(
    payload: Any,
) -> dict[tuple[str, str], list[dict[str, str]]]:
    """Map ``(namespace, service)`` to the pods actually backing it.

    Keyed off ``EndpointSlice.metadata.labels["kubernetes.io/service-name"]``
    and resolved through ``endpoints[].targetRef``, which names the pod
    directly. Addresses are the fallback for a slice with no targetRef (the
    API server's own Service has none), matched against pod IPs by the
    caller.

    Returns ``{(ns, svc): [{"pod": name, "namespace": ns}, ...]}``.
    """
    out: dict[tuple[str, str], list[dict[str, str]]] = {}
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        svc = (meta.get("labels") or {}).get(SERVICE_NAME_LABEL)
        if not svc:
            continue
        key = (meta.get("namespace") or "default", svc)
        targets = out.setdefault(key, [])
        for endpoint in item.get("endpoints", []) or []:
            ref = endpoint.get("targetRef") or {}
            if ref.get("kind") == "Pod" and ref.get("name"):
                entry = {
                    "pod": ref["name"],
                    "namespace": ref.get("namespace") or key[0],
                }
            else:
                # No targetRef: keep the addresses so the caller can match
                # them against pod IPs itself.
                entry = {
                    "pod": "",
                    "namespace": key[0],
                    "addresses": list(endpoint.get("addresses") or []),
                }
            if entry not in targets:
                targets.append(entry)
    return out


def parse_ingress(payload: Any) -> list[dict[str, Any]]:
    """Extract Ingress rules as host -> backend service references.

    Only the backend *service* is meaningful to the picture; the path and
    port details belong in a detail pane, not a box.
    """
    ingresses: list[dict[str, Any]] = []
    for item in (payload or {}).get("items", []):
        meta = item.get("metadata", {})
        name = meta.get("name")
        if not name:
            continue
        spec = item.get("spec", {}) or {}
        rules: list[dict[str, str]] = []
        backends: list[dict[str, str]] = []

        default = ((spec.get("defaultBackend") or {}).get("service") or {})
        if default.get("name"):
            # A backend with no namespace of its own lives in the Ingress's
            # namespace, which is what Kubernetes resolves it to. The rules
            # path below honours an explicit namespace, so this one must too.
            backends.append(
                {
                    "name": default["name"],
                    "namespace": default.get("namespace")
                    or meta.get("namespace")
                    or "default",
                }
            )

        for rule in spec.get("rules", []) or []:
            host = rule.get("host") or "*"
            http = (rule.get("http") or {}).get("paths") or []
            for path in http:
                svc = ((path.get("backend") or {}).get("service") or {})
                if not svc.get("name"):
                    continue
                backend_ns = svc.get("namespace") or meta.get("namespace") or "default"
                rules.append(
                    {"host": host, "service": svc["name"], "namespace": backend_ns}
                )
                backends.append({"name": svc["name"], "namespace": backend_ns})
        ingresses.append(
            {
                "name": name,
                "namespace": meta.get("namespace") or "default",
                "class": (spec.get("ingressClassName") or "") or "",
                "rules": rules,
                "backends": backends,
            }
        )
    return ingresses


def build_model(
    *,
    nodes: list[dict[str, Any]],
    namespaces: list[str],
    pods: list[dict[str, Any]],
    workloads: dict[tuple[str, str], dict[str, Any]],
    replica_sets: dict[tuple[str, str], dict[str, Any]],
    services: list[dict[str, Any]],
    endpoint_slices: dict[tuple[str, str], list[dict[str, str]]],
    ingresses: list[dict[str, Any]],
    node_facts: list[dict[str, Any]] | None = None,
    driver: str = "",
) -> dict[str, Any]:
    """Assemble the whole picture's data from the parsed pieces.

    Produces the node/namespace/workload/service/ingress shape the renderer
    walks. Deliberately a plain dict with lists, matching the rest of
    kubby's service payloads: the renderer reads it, tests assert on it, and
    neither needs to know a class exists.
    """
    pods_by_ns: dict[str, list[dict[str, Any]]] = {}
    for pod in pods:
        resolved = resolve_workload(pod, workloads, replica_sets)
        entry = dict(pod)
        entry["workload"] = resolved
        entry.setdefault("namespace", "default")
        pods_by_ns.setdefault(entry.get("namespace") or "default", []).append(entry)

    # Address fallback: a slice without targetRef still points at pods by IP.
    pods_by_ip = {p.get("ip"): p for p in pods if p.get("ip")}

    services_out: list[dict[str, Any]] = []
    for svc in services:
        key = (svc["namespace"], svc["name"])
        backing: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for target in endpoint_slices.get(key, []):
            if target.get("pod"):
                ident = (target["namespace"], target["pod"])
            else:
                pod = next(
                    (
                        pods_by_ip[addr]
                        for addr in target.get("addresses") or []
                        if addr in pods_by_ip
                    ),
                    None,
                )
                if pod is None:
                    continue
                ident = (pod["namespace"], pod["name"])
            if ident in seen:
                continue
            seen.add(ident)
            backing.append({"namespace": ident[0], "pod": ident[1]})
        services_out.append({**svc, "backing_pods": backing})

    namespaces_out: list[dict[str, Any]] = []
    for name in namespaces:
        ns_pods = pods_by_ns.get(name, [])
        namespaces_out.append(
            {
                "name": name,
                "pods": ns_pods,
                "workload_count": len({(p["workload"]["name"], p["workload"]["kind"]) for p in ns_pods}),
                "collapsed": name in COLLAPSED_NAMESPACES,
            }
        )

    return {
        "nodes": nodes,
        "node_facts": node_facts or [],
        "driver": driver,
        "namespaces": namespaces_out,
        "services": services_out,
        "ingresses": ingresses,
        "pod_count": len(pods),
    }
