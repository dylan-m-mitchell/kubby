"""Tests for `kubby.cluster` — the pure derivations behind the graph.

No kubectl, no cluster: every payload here is a hand-written slice of the
real JSON, so the awkward shapes (a static control-plane pod owned by a
Node, a Service whose Endpoints have no targetRef, a ReplicaSet with no
Deployment above it) are pinned on purpose.
"""

from __future__ import annotations

import pytest

from kubby import cluster


def _item(kind: str, name: str, ns: str = "default", **spec) -> dict:
    """A minimal k8s object with the fields the parsers actually read."""
    meta = {"name": name, "namespace": ns}
    meta.update(spec.pop("metadata", {}))
    out = {"kind": kind, "metadata": meta}
    out.update(spec)
    return out


# ---------------------------------------------------------------------------
# pods
# ---------------------------------------------------------------------------


class TestParsePods:
    def test_reads_node_and_owner(self):
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "web-7d764666f9-87k8q",
                        "namespace": "default",
                        "ownerReferences": [
                            {"kind": "ReplicaSet", "name": "web-7d764666f9"}
                        ],
                    },
                    "spec": {"nodeName": "minikube", "podIP": "10.244.0.9"},
                    "status": {"phase": "Running"},
                }
            ]
        }
        (pod,) = cluster.parse_pods(payload)
        assert pod == {
            "name": "web-7d764666f9-87k8q",
            "namespace": "default",
            "phase": "Running",
            "node": "minikube",
            "ip": "10.244.0.9",
            "owner_kind": "ReplicaSet",
            "owner_name": "web-7d764666f9",
        }

    def test_defaults_namespace_and_phase(self):
        (pod,) = cluster.parse_pods({"items": [{"metadata": {"name": "lonely"}}]})
        assert pod["namespace"] == "default"
        assert pod["phase"] == "Unknown"
        assert pod["node"] == ""

    def test_skips_items_without_a_name(self):
        assert cluster.parse_pods({"items": [{"metadata": {}}]}) == []

    def test_missing_payload_is_empty_not_a_crash(self):
        assert cluster.parse_pods(None) == []
        assert cluster.parse_pods({}) == []


# ---------------------------------------------------------------------------
# workload resolution — the part that decides what a box is called
# ---------------------------------------------------------------------------


class TestResolveWorkload:
    def test_replicaset_resolves_to_its_deployment(self):
        pod = {
            "namespace": "default",
            "name": "web-7d764666f9-87k8q",
            "owner_kind": "ReplicaSet",
            "owner_name": "web-7d764666f9",
        }
        replica_sets = {("default", "web-7d764666f9"): {"kind": "Deployment", "name": "web"}}
        got = cluster.resolve_workload(pod, {}, replica_sets)
        assert got == {"name": "web", "kind": "Deployment", "inferred": False}

    def test_replicaset_with_no_deployment_keeps_its_own_name(self):
        # An orphaned ReplicaSet (scale-down, failed rollout) must not be
        # mislabelled as a Deployment.
        pod = {
            "namespace": "default",
            "name": "web-old-abc123",
            "owner_kind": "ReplicaSet",
            "owner_name": "web-old-abc123",
        }
        got = cluster.resolve_workload(pod, {}, {})
        assert got == {"name": "web-old", "kind": "ReplicaSet", "inferred": False}

    def test_deployment_owner_used_directly(self):
        pod = {"namespace": "d", "name": "p", "owner_kind": "Deployment", "owner_name": "api"}
        assert cluster.resolve_workload(pod, {}, {})["name"] == "api"

    def test_node_owned_pod_uses_its_own_name(self):
        # The static control-plane pattern: every one of these is owned by
        # the Node, so trusting the owner would name them all "minikube".
        for name, expected in (
            ("etcd-minikube", "etcd"),
            ("kube-apiserver-minikube", "kube-apiserver"),
            ("kube-scheduler-minikube", "kube-scheduler"),
        ):
            pod = {
                "namespace": "kube-system",
                "name": name,
                "owner_kind": "Node",
                "owner_name": "minikube",
            }
            got = cluster.resolve_workload(pod, {}, {})
            assert got["name"] == expected, name

    def test_node_owned_pods_do_not_collide(self):
        names = {
            cluster.resolve_workload(
                {
                    "namespace": "kube-system",
                    "name": f"{comp}-minikube",
                    "owner_kind": "Node",
                    "owner_name": "minikube",
                },
                {},
                {},
            )["name"]
            for comp in ("etcd", "kube-apiserver", "kube-scheduler")
        }
        assert len(names) == 3

    def test_orphan_pod_falls_back_to_its_own_name(self):
        pod = {"namespace": "d", "name": "standalone-xyz12", "owner_kind": "", "owner_name": ""}
        got = cluster.resolve_workload(pod, {}, {})
        assert got["name"] == "standalone-xyz12"
        assert got["inferred"] is True


# ---------------------------------------------------------------------------
# workloads / replica sets
# ---------------------------------------------------------------------------


class TestParseWorkloads:
    @pytest.mark.parametrize(
        ("kind", "status", "ready", "desired"),
        [
            ("Deployment", {"readyReplicas": 3, "replicas": 3}, 3, 3),
            ("StatefulSet", {"readyReplicas": 1, "replicas": 2}, 1, 2),
            ("DaemonSet", {"numberReady": 1, "desiredNumberScheduled": 1}, 1, 1),
        ],
    )
    def test_ready_counts_per_kind(self, kind, status, ready, desired):
        payload = {"items": [_item(kind, "w", status=status)]}
        got = cluster.parse_workloads(payload)
        assert got[("default", "w")] == {"kind": kind, "ready": ready, "desired": desired}

    def test_unknown_kind_ignored(self):
        assert cluster.parse_workloads({"items": [_item("Secret", "s")]}) == {}

    def test_string_numbers_are_coerced(self):
        payload = {"items": [_item("Deployment", "w", status={"readyReplicas": "2", "replicas": "2"})]}
        assert cluster.parse_workloads(payload)[("default", "w")]["ready"] == 2


class TestParseReplicaSets:
    def test_follows_the_deployment_owner(self):
        payload = {
            "items": [
                _item(
                    "ReplicaSet",
                    "web-7d764666f9",
                    metadata={
                        "ownerReferences": [{"kind": "Deployment", "name": "web"}]
                    },
                )
            ]
        }
        assert cluster.parse_replica_sets(payload)[("default", "web-7d764666f9")] == {
            "kind": "Deployment",
            "name": "web",
        }

    def test_ownerless_replicaset_reports_itself(self):
        payload = {"items": [_item("ReplicaSet", "web-stale")]}
        assert cluster.parse_replica_sets(payload)[("default", "web-stale")]["kind"] == "ReplicaSet"


# ---------------------------------------------------------------------------
# services + endpoint slices — the authoritative Service -> Pod link
# ---------------------------------------------------------------------------


class TestParseServices:
    def test_reads_type_ports_and_selector_presence(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "web-svc", "namespace": "default"},
                    "spec": {
                        "type": "NodePort",
                        "clusterIP": "10.96.0.20",
                        "selector": {"app": "web"},
                        "ports": [{"port": 80, "targetPort": 8080, "nodePort": 30080}],
                    },
                }
            ]
        }
        (svc,) = cluster.parse_services(payload)
        assert svc["type"] == "NodePort"
        assert svc["has_selector"] is True
        assert svc["ports"] == [{"port": 80, "target": "8080", "node_port": 30080}]

    def test_selectorless_service_is_flagged_not_assumed_broken(self):
        # The API server's own `kubernetes` Service has no selector. That is
        # not the same failure as a selector matching nothing.
        payload = {
            "items": [
                {
                    "metadata": {"name": "kubernetes", "namespace": "default"},
                    "spec": {"type": "ClusterIP", "ports": [{"port": 443}]},
                }
            ]
        }
        (svc,) = cluster.parse_services(payload)
        assert svc["has_selector"] is False


class TestParseEndpointSlices:
    def test_targetref_names_the_backing_pod(self):
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "web-svc-abc12",
                        "namespace": "default",
                        "labels": {"kubernetes.io/service-name": "web-svc"},
                    },
                    "endpoints": [
                        {
                            "addresses": ["10.244.0.9"],
                            "targetRef": {
                                "kind": "Pod",
                                "name": "web-7d764666f9-87k8q",
                                "namespace": "default",
                            },
                        }
                    ],
                }
            ]
        }
        got = cluster.parse_endpoint_slices(payload)
        assert got == {
            ("default", "web-svc"): [
                {"pod": "web-7d764666f9-87k8q", "namespace": "default"}
            ]
        }

    def test_sliceless_targetref_keeps_addresses_for_ip_matching(self):
        payload = {
            "items": [
                {
                    "metadata": {
                        "name": "kubernetes",
                        "namespace": "default",
                        "labels": {"kubernetes.io/service-name": "kubernetes"},
                    },
                    "endpoints": [{"addresses": ["192.168.49.2"]}],
                }
            ]
        }
        got = cluster.parse_endpoint_slices(payload)
        assert got[("default", "kubernetes")] == [
            {"pod": "", "namespace": "default", "addresses": ["192.168.49.2"]}
        ]

    def test_slice_without_the_service_label_is_ignored(self):
        payload = {"items": [_item("EndpointSlice", "orphan", metadata={"labels": {}})]}
        assert cluster.parse_endpoint_slices(payload) == {}


class TestParseIngress:
    def test_reads_host_to_backend_service(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "web", "namespace": "ingress-nginx"},
                    "spec": {
                        "ingressClassName": "nginx",
                        "rules": [
                            {
                                "host": "shop.example",
                                "http": {
                                    "paths": [
                                        {
                                            "path": "/",
                                            "backend": {
                                                "service": {
                                                    "name": "web-svc",
                                                    "namespace": "default",
                                                }
                                            },
                                        }
                                    ]
                                },
                            }
                        ],
                    },
                }
            ]
        }
        (ing,) = cluster.parse_ingress(payload)
        assert ing["class"] == "nginx"
        assert ing["rules"] == [
            {"host": "shop.example", "service": "web-svc", "namespace": "default"}
        ]

    def test_default_backend_is_picked_up(self):
        payload = {
            "items": [
                {
                    "metadata": {"name": "root", "namespace": "ingress-nginx"},
                    "spec": {
                        "defaultBackend": {
                            "service": {"name": "web-svc", "namespace": "default"}
                        }
                    },
                }
            ]
        }
        (ing,) = cluster.parse_ingress(payload)
        assert ing["backends"] == [{"name": "web-svc", "namespace": "default"}]

    def test_backend_without_a_namespace_means_the_ingresses_own(self):
        # Kubernetes resolves a namespaceless backend service in the
        # Ingress's own namespace, so `ingress-nginx/web-svc` is right and
        # silently looking in `default` would not be.
        payload = {
            "items": [
                {
                    "metadata": {"name": "root", "namespace": "ingress-nginx"},
                    "spec": {"defaultBackend": {"service": {"name": "web-svc"}}},
                }
            ]
        }
        (ing,) = cluster.parse_ingress(payload)
        assert ing["backends"] == [{"name": "web-svc", "namespace": "ingress-nginx"}]


# ---------------------------------------------------------------------------
# the assembled model
# ---------------------------------------------------------------------------


def _model(**overrides):
    base = dict(
        nodes=[{"name": "minikube", "status": "Ready", "roles": ["control-plane"]}],
        namespaces=["default", "kube-system"],
        pods=[],
        workloads={},
        replica_sets={},
        services=[],
        endpoint_slices={},
        ingresses=[],
    )
    base.update(overrides)
    return cluster.build_model(**base)


class TestBuildModel:
    def test_service_backed_by_targetref_gets_its_pod(self):
        model = _model(
            pods=[
                {
                    "name": "web-1",
                    "namespace": "default",
                    "phase": "Running",
                    "node": "minikube",
                    "ip": "10.244.0.9",
                    "owner_kind": "ReplicaSet",
                    "owner_name": "web-7d",
                }
            ],
            services=[
                {
                    "name": "web-svc",
                    "namespace": "default",
                    "type": "ClusterIP",
                    "cluster_ip": "10.96.0.20",
                    "has_selector": True,
                    "ports": [{"port": 80, "target": "8080", "node_port": None}],
                }
            ],
            endpoint_slices={
                ("default", "web-svc"): [
                    {"pod": "web-1", "namespace": "default"}
                ]
            },
        )
        (svc,) = model["services"]
        assert svc["backing_pods"] == [{"namespace": "default", "pod": "web-1"}]

    def test_address_only_slice_resolves_through_pod_ip(self):
        model = _model(
            pods=[
                {
                    "name": "dns-1",
                    "namespace": "kube-system",
                    "phase": "Running",
                    "node": "minikube",
                    "ip": "10.244.0.4",
                    "owner_kind": "ReplicaSet",
                    "owner_name": "coredns-7d",
                }
            ],
            services=[
                {
                    "name": "kube-dns",
                    "namespace": "kube-system",
                    "type": "ClusterIP",
                    "cluster_ip": "10.96.0.10",
                    "has_selector": True,
                    "ports": [{"port": 53, "target": "53", "node_port": None}],
                }
            ],
            endpoint_slices={
                ("kube-system", "kube-dns"): [
                    {"pod": "", "namespace": "kube-system", "addresses": ["10.244.0.4"]}
                ]
            },
        )
        (svc,) = model["services"]
        assert svc["backing_pods"] == [{"namespace": "kube-system", "pod": "dns-1"}]

    def test_service_with_no_endpoints_reports_none(self):
        model = _model(
            services=[
                {
                    "name": "db-svc",
                    "namespace": "default",
                    "type": "ClusterIP",
                    "cluster_ip": "10.96.0.30",
                    "has_selector": True,
                    "ports": [{"port": 5432, "target": "5432", "node_port": None}],
                }
            ]
        )
        (svc,) = model["services"]
        assert svc["backing_pods"] == []

    def test_duplicate_backing_pods_are_collapsed(self):
        # Two slices can name the same pod (a rolling update leaves both).
        model = _model(
            services=[
                {
                    "name": "web-svc",
                    "namespace": "default",
                    "type": "ClusterIP",
                    "cluster_ip": "",
                    "has_selector": True,
                    "ports": [],
                }
            ],
            endpoint_slices={
                ("default", "web-svc"): [
                    {"pod": "web-1", "namespace": "default"},
                    {"pod": "web-1", "namespace": "default"},
                ]
            },
        )
        (svc,) = model["services"]
        assert svc["backing_pods"] == [{"namespace": "default", "pod": "web-1"}]

    def test_namespaces_carry_pods_and_a_distinct_workload_count(self):
        model = _model(
            namespaces=["default"],
            pods=[
                {
                    "name": "web-1",
                    "namespace": "default",
                    "phase": "Running",
                    "node": "minikube",
                    "ip": "",
                    "owner_kind": "ReplicaSet",
                    "owner_name": "web-7d",
                },
                {
                    "name": "web-2",
                    "namespace": "default",
                    "phase": "Running",
                    "node": "minikube",
                    "ip": "",
                    "owner_kind": "ReplicaSet",
                    "owner_name": "web-7d",
                },
            ],
            replica_sets={("default", "web-7d"): {"kind": "Deployment", "name": "web"}},
        )
        (ns,) = model["namespaces"]
        assert len(ns["pods"]) == 2
        assert ns["workload_count"] == 1  # two pods, one workload
        assert ns["collapsed"] is False

    def test_control_plane_namespaces_are_marked_collapsed(self):
        model = _model(namespaces=["default", "kube-system", "kube-public", "kube-node-lease"])
        collapsed = {ns["name"] for ns in model["namespaces"] if ns["collapsed"]}
        assert collapsed == {"kube-system", "kube-public", "kube-node-lease"}

    def test_empty_cluster_is_a_valid_model(self):
        model = _model()
        assert model["namespaces"] == [
            {"name": "default", "pods": [], "workload_count": 0, "collapsed": False},
            {"name": "kube-system", "pods": [], "workload_count": 0, "collapsed": True},
        ]
        assert model["pod_count"] == 0
