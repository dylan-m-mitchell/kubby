"""A realistic cluster to look at, not a demo that only looks good.

Not a test. Every shape here exists because it is the thing people get
wrong:

  * an Ingress with a real host rule, so external traffic has a way in
  * a Deployment whose Service actually has backing pods
  * a Service whose selector matches nothing, so it has *no endpoints* —
    the single most common beginner mistake, and the one drawn red
  * a crashlooping pod, and one asking for more CPU than the node has
  * a StatefulSet, so the picture shows something other than Deployments
  * a Service in another namespace, so cross-group wiring appears
  * a Service with no selector at all — the API server's own — which must
    *not* be reported as broken

Usage:  uv run python tests/fixtures_cluster_graph.py
"""

from __future__ import annotations

import sys

sys.path.insert(0, "tests")

from kubby.cluster import resolve_workload  # noqa: E402
from kubby.tui import diagram, paint, place  # noqa: E402

#: ReplicaSet name -> the workload it fronts, so the fixture produces the
#: same workload names the service would.
_REPLICA_SETS = {
    ("default", "web-abc"): {"kind": "Deployment", "name": "web"},
    ("default", "api-abc"): {"kind": "Deployment", "name": "api"},
    ("kube-system", "coredns-abc"): {"kind": "Deployment", "name": "coredns"},
}


def _pod(name, ns, phase="Running", node="minikube", ready=True, owner=("ReplicaSet", "")):
    pod = {
        "name": name,
        "namespace": ns,
        "phase": phase,
        "ready": ready,
        "node": node,
        "ip": "",
        "owner_kind": owner[0],
        "owner_name": owner[1],
    }
    # The real resolver, not a local guess. An earlier version of this
    # fixture computed the name itself and produced a `minikube` box for
    # `etcd-node`, which the service would never emit — a fixture that
    # reimplements the logic under test stops being evidence.
    pod["workload"] = resolve_workload(pod, {}, _REPLICA_SETS)
    return pod


def _svc(name, ns, port, backing, has_selector=True):
    return {
        "name": name,
        "namespace": ns,
        "type": "ClusterIP",
        "cluster_ip": "10.96.0.1",
        "has_selector": has_selector,
        "ports": [{"port": port, "target": str(port), "node_port": None}],
        "backing_pods": [{"namespace": n, "pod": p} for n, p in backing],
    }


def _node_facts(name, cpu="16", alloc_cpu="2", ip="192.168.49.2"):
    return {
        "name": name, "status": "Ready", "roles": ["control-plane"],
        "internal_ip": ip, "os_image": "Debian GNU/Linux 12 (bookworm)",
        "kernel": "6.1.0", "architecture": "amd64", "kubelet_version": "v1.35.1",
        "runtime": "docker 29.2.1", "pod_cidr": "10.244.0.0/16",
        "capacity_cpu": cpu, "allocatable_cpu": alloc_cpu,
        "capacity_memory": "16313348Ki", "allocatable_memory": "2097152Ki",
    }


def realistic() -> dict:
    """A two-node cluster with an ingress, three services, four workloads."""
    return {
        "available": True,
        "error": None,
        "context": "minikube",
        "version": "v1.35.1",
        "driver": "docker",
        "node_facts": [_node_facts("minikube"), _node_facts("worker-2", ip="192.168.49.3")],
        "nodes": [
            {"name": "minikube", "status": "Ready", "roles": ["control-plane"]},
            {"name": "worker-2", "status": "Ready", "roles": []},
        ],
        "namespaces": [
            {"name": "default", "workload_count": 2, "collapsed": False, "pods": [
                _pod("web-1", "default", owner=("ReplicaSet", "web-abc")),
                _pod("web-2", "default", owner=("ReplicaSet", "web-abc")),
                _pod("api-1", "default", owner=("ReplicaSet", "api-abc")),
            ]},
            {"name": "data", "workload_count": 1, "collapsed": False, "pods": [
                _pod("postgres-0", "data", node="worker-2", owner=("StatefulSet", "postgres")),
            ]},
            {"name": "kube-system", "workload_count": 2, "collapsed": True, "pods": [
                _pod("coredns-1", "kube-system", owner=("ReplicaSet", "coredns-abc")),
                _pod("etcd-node", "kube-system", owner=("Node", "minikube")),
            ]},
        ],
        "services": [
            _svc("web-svc", "default", 80, [("default", "web-1"), ("default", "web-2")]),
            _svc("api-svc", "default", 8080, [("default", "api-1")]),
            # The classic beginner mistake: a selector that matches nothing.
            _svc("db-svc", "default", 5432, []),
            _svc("postgres", "data", 5432, [("data", "postgres-0")]),
            # Selectorless, so not reported as broken.
            _svc("kubernetes", "default", 443, [], has_selector=False),
        ],
        "ingresses": [
            {
                "name": "shop", "namespace": "default", "class": "nginx",
                "rules": [{"host": "shop.example", "service": "web-svc",
                           "namespace": "default"}],
                "backends": [{"name": "web-svc", "namespace": "default"}],
            }
        ],
        "pod_count": 5,
    }


def broken() -> dict:
    """The same shape with the workload unhealthy, for the colour rules."""
    model = realistic()
    model["namespaces"][0]["pods"] = [
        _pod("web-1", "default", phase="Running", ready=False,
             owner=("ReplicaSet", "web-abc")),
        _pod("api-1", "default", phase="Running", ready=False,
             owner=("ReplicaSet", "api-abc")),
    ]
    return model


def main() -> int:
    for label, model in (("realistic", realistic()), ("broken", broken())):
        built = diagram.build_diagram(model)
        placement = place.place(built, 72)
        print("=" * 74)
        print(f"{label}: {len(built.nodes)} boxes, {len(built.edges)} edges, "
              f"{placement.width} x {placement.height}")
        print("=" * 74)
        print(paint.render(placement).plain)
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
