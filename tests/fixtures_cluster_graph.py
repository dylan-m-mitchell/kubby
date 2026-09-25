"""A realistic cluster model, for looking at the picture.

Not a test: this is the shape a developer's cluster actually has — an
Ingress fronting a Service, a Deployment behind it, a second Service with a
selector that matches nothing, a StatefulSet, and pods spread over two
nodes. It exists so the graph can be *looked at* while it is being built,
including the cases that are awkward to arrange on a live cluster.

Usage:  uv run python tests/fixtures_cluster_graph.py
"""

from __future__ import annotations

import sys

from kubby.tui import graph


def _pod(name, ns, phase="Running", node="minikube", ip="", owner=("ReplicaSet", "")):
    return {
        "name": name,
        "namespace": ns,
        "phase": phase,
        "node": node,
        "ip": ip,
        "owner_kind": owner[0],
        "owner_name": owner[1],
        "workload": {
            "name": owner[1].rsplit("-", 1)[0] if owner[1] else name,
            "kind": owner[0],
            "inferred": False,
        },
    }


def realistic() -> dict:
    """A two-node cluster with an ingress, three services, four workloads."""
    pods = [
        # web: 3 healthy replicas
        _pod("web-7d4f9c-abc", "default", ip="10.244.0.5", owner=("ReplicaSet", "web-7d4f9c")),
        _pod("web-7d4f9c-def", "default", ip="10.244.1.5", owner=("ReplicaSet", "web-7d4f9c")),
        _pod("web-7d4f9c-ghi", "default", ip="10.244.2.5", owner=("ReplicaSet", "web-7d4f9c")),
        # api: 1 of 2 ready, the other still pulling
        _pod("api-6b8c7d-aaa", "default", ip="10.244.0.7", owner=("ReplicaSet", "api-6b8c7d")),
        _pod("api-6b8c7d-bbb", "default", phase="Pending", ip="", owner=("ReplicaSet", "api-6b8c7d")),
        # worker: crashlooping
        _pod("worker-5f7a9b-zzz", "default", phase="CrashLoopBackOff",
             node="worker-2", ip="10.244.3.9", owner=("ReplicaSet", "worker-5f7a9b")),
        # postgres: a StatefulSet, on the other node
        _pod("postgres-0", "data", node="worker-2", ip="10.244.3.4",
             owner=("StatefulSet", "postgres")),
        # the control plane, collapsed away in the picture
        _pod("coredns-77d-xyz", "kube-system", ip="10.244.0.4",
             owner=("ReplicaSet", "coredns-77d")),
        _pod("etcd-minikube", "kube-system", owner=("Node", "minikube")),
    ]

    def svc(name, ns, port, backing, has_selector=True, stype="ClusterIP"):
        return {
            "name": name,
            "namespace": ns,
            "type": stype,
            "cluster_ip": f"10.96.0.{20 + len(backing)}",
            "has_selector": has_selector,
            "ports": [{"port": port, "target": str(port), "node_port": None}],
            "backing_pods": [{"namespace": p[0], "pod": p[1]} for p in backing],
        }

    return {
        "available": True,
        "error": None,
        "context": "minikube",
        "version": "v1.35.1",
        "nodes": [
            {"name": "minikube", "status": "Ready", "roles": ["control-plane"]},
            {"name": "worker-2", "status": "Ready", "roles": []},
        ],
        "namespaces": [
            {"name": "default", "pods": pods[:6], "workload_count": 3, "collapsed": False},
            {"name": "data", "pods": pods[6:7], "workload_count": 1, "collapsed": False},
            {"name": "kube-system", "pods": pods[7:], "workload_count": 2, "collapsed": True},
        ],
        "services": [
            svc("web-svc", "default", 80, [("default", "web-7d4f9c-abc"),
                                           ("default", "web-7d4f9c-def"),
                                           ("default", "web-7d4f9c-ghi")]),
            svc("api-svc", "default", 8080, [("default", "api-6b8c7d-aaa")]),
            # The classic beginner mistake: a selector that matches nothing.
            svc("db-svc", "default", 5432, []),
            svc("postgres", "data", 5432, [("data", "postgres-0")]),
            # Selectorless, so not reported as broken.
            svc("kubernetes", "default", 443, [], has_selector=False),
            svc("kube-dns", "kube-system", 53, [("kube-system", "coredns-77d-xyz")]),
        ],
        "ingresses": [
            {
                "name": "shop",
                "namespace": "ingress-nginx",
                "class": "nginx",
                "rules": [{"host": "shop.example", "service": "web-svc",
                           "namespace": "default"}],
                "backends": [{"name": "web-svc", "namespace": "default"}],
            }
        ],
        "pod_count": len(pods),
    }


def main() -> int:
    model = realistic()
    source = graph.build_mermaid(model)
    if "--source" in sys.argv:
        print(source)
    else:
        print(source)
        print("=" * 78)
        print(graph.render(source, 72))
        print("=" * 78)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
