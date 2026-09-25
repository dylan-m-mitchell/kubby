"""Tests for `kubby.tui.graph` — Mermaid source and rendering.

The source generator is pure, so most of this is golden: a cluster shape in,
the exact diagram out. That catches the things that actually broke while
building it — a label escaping rule that turned `kube-system` into
`kube_system`, an edge emitted once per pod, a service declared nowhere
because its namespace was collapsed.
"""

from __future__ import annotations


from rich.text import Text

from fixtures_cluster_graph import realistic
from kubby.tui import graph


def _pod(name, ns="default", phase="Running", node="minikube", owner=("ReplicaSet", "w-1")):
    return {
        "name": name,
        "namespace": ns,
        "phase": phase,
        "node": node,
        "ip": "",
        "owner_kind": owner[0],
        "owner_name": owner[1],
        "workload": {
            "name": owner[1].rsplit("-", 1)[0] if owner[1] else name,
            "kind": owner[0],
            "inferred": False,
        },
    }


def _svc(name, ns="default", port=80, backing=(), has_selector=True):
    return {
        "name": name,
        "namespace": ns,
        "type": "ClusterIP",
        "cluster_ip": "10.96.0.1",
        "has_selector": has_selector,
        "ports": [{"port": port, "target": str(port), "node_port": None}],
        "backing_pods": [{"namespace": n, "pod": p} for n, p in backing],
    }


def _model(**over):
    base = {
        "available": True,
        "error": None,
        "nodes": [],
        "namespaces": [],
        "services": [],
        "ingresses": [],
        "pod_count": 0,
    }
    base.update(over)
    return base


# ---------------------------------------------------------------------------
# the architecture layer: what the cluster physically is
# ---------------------------------------------------------------------------


class TestArchitecture:
    def test_host_and_node_are_drawn(self):
        src = graph.build_mermaid(realistic())
        assert 'host_your_computer["your computer\\nminikube, docker driver"]' in src
        assert "host_your_computer --> node_minikube" in src

    def test_node_box_names_its_os_and_runtime(self):
        """A pod is a container on that runtime, not a VM in the abstract."""
        src = graph.build_mermaid(realistic())
        assert "Debian GNU/Linux 12 (bookworm)" in src
        assert "docker 29.2.1" in src

    def test_node_box_shows_allocatable_against_capacity(self):
        """The explanation for a Pod stuck Pending with 'insufficient cpu':
        the node can hand out 2 of the host's 16, and only the first number
        is what a Pod may ask for."""
        src = graph.build_mermaid(realistic())
        assert "2 of 16 cpu" in src
        assert "2Gi of 15.6Gi" in src

    def test_equal_capacity_and_allocatable_is_not_a_pointless_pair(self):
        model = realistic()
        for fact in model["node_facts"]:
            fact["allocatable_cpu"] = fact["capacity_cpu"]
            fact["allocatable_memory"] = fact["capacity_memory"]
        src = graph.build_mermaid(model)
        assert "16 cpu, 15.6Gi" in src
        assert "of 16 cpu" not in src
        assert "2Gi of" not in src

    def test_each_control_plane_component_gets_a_box_and_a_job(self):
        src = graph.build_mermaid(realistic())
        assert 'w_Pod_etcd["etcd\\nall cluster state lives here"]' in src
        assert "service names to addresses" in src

    def test_control_plane_boxes_carry_no_ready_count(self):
        """"kube-apiserver 1/1" tells a newcomer nothing; the job is the
        fact they came for."""
        src = graph.build_mermaid(realistic())
        assert "all cluster state lives here\\n" not in src
        assert "1/1\\n" not in src.split("w_Pod_etcd")[1][:60]

    def test_user_workloads_still_get_ready_counts(self):
        src = graph.build_mermaid(realistic())
        assert 'w_Deployment_web["web\\n3/3  80"]' in src
        assert 'w_Deployment_api["api\\n1/2  8080"]' in src

    def test_a_cluster_with_no_node_facts_still_renders(self):
        """A node object we could not read must not remove the picture."""
        model = realistic()
        model["node_facts"] = []
        src = graph.build_mermaid(model)
        assert "your computer" not in src
        assert "web-svc" in src

    def test_every_node_appears_as_a_box(self):
        src = graph.build_mermaid(realistic())
        assert "node_minikube[" in src and "node_worker_2[" in src


# ---------------------------------------------------------------------------
# degenerate inputs — these must never raise
# ---------------------------------------------------------------------------


class TestDegenerate:
    def test_unavailable_cluster_shows_the_reason(self):
        src = graph.build_mermaid({"available": False, "error": "connection refused"})
        assert 'NONE["connection refused"]' in src

    def test_empty_cluster_is_still_a_valid_diagram(self):
        # termaid renders *nothing* for an empty graph, which would leave a
        # blank panel with no explanation.
        assert 'NONE["no workloads"]' in graph.build_mermaid(_model())

    def test_rendering_an_empty_model_does_not_raise(self):
        text = graph.render(graph.build_mermaid(_model()), 60)
        assert "no workloads" in text.plain

    def test_malformed_source_renders_without_raising(self):
        text = graph.render("this is not mermaid at all {{{", 60)
        assert isinstance(text.plain, str)

    def test_node_rects_on_malformed_source_is_empty_not_an_exception(self):
        assert graph.node_rects("not mermaid {{{") == {}


# ---------------------------------------------------------------------------
# labels — where the escaping bugs lived
# ---------------------------------------------------------------------------


class TestLabels:
    def test_dashes_survive_in_labels(self):
        """Regression: sanitising labels with the id rule renamed
        `kube-system` to `kube_system` and `53, 9153` to `53__9153`."""
        src = graph.build_mermaid(
            _model(
                namespaces=[{"name": "kube-system", "pods": [_pod("coredns-1", "kube-system")],
                             "workload_count": 1, "collapsed": False}],
                services=[_svc("kube-dns", "kube-system", 53,
                               backing=[("kube-system", "coredns-1")])],
            )
        )
        # Ids are sanitised to underscores (they must be), but a *label*
        # must not be: `subgraph ns_kube_system [kube-system]` is correct,
        # `[kube_system]` is the regression.
        assert "subgraph ns_kube_system [kube-system]" in src
        assert 'svc_kube_system_kube_dns["kube-dns\\n53"]' in src

    def test_multiple_ports_keep_their_comma(self):
        svc = _svc("api", backing=[("default", "w-1")])
        svc["ports"] = [{"port": 53, "target": "53", "node_port": None},
                        {"port": 9153, "target": "9153", "node_port": None}]
        src = graph.build_mermaid(
            _model(namespaces=[{"name": "default", "pods": [_pod("w-1")],
                                "workload_count": 1, "collapsed": False}],
                    services=[svc])
        )
        assert "53, 9153" in src

    def test_line_break_is_an_escape_never_a_raw_newline(self):
        """A real newline in a label is parsed as a second node, which
        silently halves the diagram."""
        for line in graph.build_mermaid(realistic()).splitlines():
            if line.count('"') == 2 and '"' in line:
                assert "\n" not in line

    def test_labels_that_would_break_out_are_stripped(self):
        src = graph.build_mermaid(
            _model(namespaces=[{"name": 'ev"il[ns]', "pods": [_pod("p-1")],
                                "workload_count": 1, "collapsed": False}])
        )
        # The quotes and bracket that would end the label are gone, and the
        # name is still recognisable.
        assert 'evil' in src
        assert '"ev"il' not in src

    def test_overlong_namespace_label_is_truncated(self):
        long_ns = "a-really-quite-long-namespace-name-indeed"
        src = graph.build_mermaid(
            _model(namespaces=[{"name": long_ns, "pods": [_pod("p-1", long_ns)],
                                "workload_count": 1, "collapsed": False}])
        )
        assert long_ns not in src
        assert "…" in src

    def test_overlong_workload_label_is_truncated(self):
        pod = _pod("p-1", owner=("ReplicaSet", "an-extremely-long-deployment-name-v2"))
        src = graph.build_mermaid(
            _model(namespaces=[{"name": "default", "pods": [pod],
                                "workload_count": 1, "collapsed": False}])
        )
        assert "…" in src


# ---------------------------------------------------------------------------
# structure
# ---------------------------------------------------------------------------


class TestStructure:
    def test_ingress_chains_client_to_service(self):
        model = realistic()
        src = graph.build_mermaid(model)
        assert "YOU --> ing_ingress_nginx_shop" in src
        assert "ing_ingress_nginx_shop --> svc_default_web_svc" in src

    def test_service_points_at_the_workload_backing_it(self):
        src = graph.build_mermaid(realistic())
        assert "svc_default_web_svc --> w_Deployment_web" in src

    def test_service_with_no_endpoints_is_red(self):
        src = graph.build_mermaid(realistic())
        assert 'svc_default_db_svc["db-svc\\n0 endpoints"]:::bad' in src

    def test_selectorless_service_is_not_reported_as_broken(self):
        """The API server's own Service has no selector. Flagging it would
        condemn the healthiest thing in the cluster."""
        src = graph.build_mermaid(realistic())
        assert "svc_default_kubernetes" in src
        assert "svc_default_kubernetes[" in src
        assert 'kubernetes\\n443"]:::bad' not in src
        assert 'kubernetes\\n443"]:::manual' in src

    def test_control_plane_is_drawn_one_box_per_component(self):
        """The control plane is the reason to look at this picture at all:
        "kube-apiserver 1/1" teaches nothing, "every request passes
        through" does."""
        src = graph.build_mermaid(realistic())
        assert 'w_Pod_etcd["etcd\\nall cluster state lives here"]' in src
        assert 'w_Deployment_coredns["coredns\\nservice names to addresses"]' in src

    def test_empty_namespaces_are_left_out(self):
        """kube-public and kube-node-lease have no wiring; a box each is
        noise on every fresh cluster. The tree still lists them."""
        src = graph.build_mermaid(
            _model(namespaces=[{"name": "kube-public", "pods": [], "workload_count": 0,
                                "collapsed": True},
                               {"name": "default", "pods": [_pod("w-1")],
                                "workload_count": 1, "collapsed": False}])
        )
        assert "kube-public" not in src and "kube_public" not in src

    def test_each_node_workload_edge_appears_once(self):
        """Regression: one edge per *pod* meant seven near-identical lines
        strung across the diagram and a picture three screens tall."""
        src = graph.build_mermaid(realistic())
        assert src.count("node_minikube --> w_Deployment_coredns") == 1

    def test_pods_spread_over_two_nodes_each_get_one_edge(self):
        src = graph.build_mermaid(realistic())
        assert "node_minikube --> w_Deployment_web" in src
        assert "node_worker_2 --> w_StatefulSet_postgres" in src

    def test_no_duplicate_edges_at_all(self):
        edges = [
            line.strip()
            for line in graph.build_mermaid(realistic()).splitlines()
            if " --> " in line
        ]
        assert len(edges) == len(set(edges))

    def test_services_are_declared_inside_their_namespace(self):
        src = graph.build_mermaid(realistic())
        default_block = src.split("subgraph ns_default")[1].split("end")[0]
        assert "svc_default_web_svc[" in default_block

    def test_ready_counts_come_from_pod_phases(self):
        src = graph.build_mermaid(realistic())
        assert 'web\\n3/3' in src
        # one of the two api pods is still Pending
        assert 'api\\n1/2' in src
        # the worker's only pod is crashlooping
        assert 'worker\\n0/1' in src

    def test_direction_is_honoured(self):
        assert graph.build_mermaid(realistic(), direction="LR").startswith("graph LR")
        assert graph.build_mermaid(realistic(), direction="TB").startswith("graph TB")

    def test_same_workload_name_in_two_namespaces_stays_two_boxes(self):
        """Regression, caught in review: `Deployment/web` in `shop` and
        `Deployment/web` in `admin` produced the same Mermaid id, so the
        picture merged them into a single box and one namespace's workload
        silently vanished. An entirely ordinary cluster shape."""
        model = _model(
            namespaces=[
                {"name": "shop", "pods": [_pod("web-1", "shop")],
                 "workload_count": 1, "collapsed": False},
                {"name": "admin", "pods": [_pod("web-1", "admin")],
                 "workload_count": 1, "collapsed": False},
            ]
        )
        for ns in model["namespaces"]:
            ns["pods"][0]["workload"] = {
                "name": "web", "kind": "Deployment", "inferred": False
            }
        src = graph.build_mermaid(model)
        boxes = [
            line.strip().split("[")[0]
            for line in src.splitlines()
            if line.strip().startswith("w_") or " w_" in line
        ]
        boxes = [b for b in boxes if b.startswith("w_")]
        assert len(boxes) == 2
        assert len(set(boxes)) == 2

    def test_unambiguous_workload_ids_stay_short(self):
        """Only a real collision should pay for a namespaced id; otherwise
        the source (and every golden string) churns for nothing."""
        src = graph.build_mermaid(realistic())
        assert "w_Deployment_web[" in src
        assert "w_default_Deployment_web[" not in src

    def test_source_is_deterministic(self):
        """A stable diagram matters: the cursor maps rectangles to objects by
        id, and a picture that reshuffles on every refresh loses the
        selection."""
        assert graph.build_mermaid(realistic()) == graph.build_mermaid(realistic())


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


class TestRender:
    def test_produces_rich_text(self):
        from rich.text import Text

        assert isinstance(graph.render(graph.build_mermaid(realistic()), 60), Text)

    def test_compacts_to_fit_a_generous_width(self):
        src = graph.build_mermaid(realistic())
        text = graph.render(src, 200)
        assert graph._max_width(text) <= 200

    def test_a_tight_width_still_renders_something(self):
        text = graph.render(graph.build_mermaid(realistic()), 20)
        assert text.plain.strip()

    def test_falls_back_rather_than_raising_on_bad_source(self):
        # A render that raised would take the TUI down on a refresh.
        assert graph.render("}{ nonsense", 60).plain is not None


class TestRenderBest:
    def test_picks_the_direction_that_fits_better(self):
        """A panel-shaped region is wider than tall, so an aspect-ratio
        heuristic chose TB where LR overflowed less. Asserted against the
        *other* direction rather than a magic number: what matters is that
        the choice is the better of the two, not what the answer happens to
        be today."""
        source = graph.build_mermaid(realistic())
        width, height = 62, 24
        chosen, direction = graph.render_best(source, width, height)
        other = graph.render(graph._retarget(source, "LR" if direction == "TB" else "TB"),
                             width)
        assert graph._overflow(chosen, width, height) <= graph._overflow(
            other, width, height
        )

    def test_never_raises_on_malformed_source(self):
        text, direction = graph.render_best("}{ nonsense", 60, 20)
        assert isinstance(text.plain, str)
        assert direction == "TB"

    def test_returns_a_direction_the_caller_can_keep(self):
        _, direction = graph.render_best(graph.build_mermaid(realistic()), 200, 200)
        assert direction in ("TB", "LR")


class TestCentre:
    def test_a_narrow_picture_is_centred(self):
        """Left-padded by half the slack on every line.

        Padding only the left is the right contract: the widget fills the
        rest of the row, and per-line centring would misalign the boxes,
        which is the one thing a drawing must not do.
        """
        picture = Text("hi\nthere")
        centred = graph.center(picture, 20)
        plain = centred.plain.splitlines()
        # widest line is "there" (5), so 7 spaces of slack either side
        assert plain == [" " * 7 + "hi", " " * 7 + "there"]

    def test_odd_slack_rounds_down(self):
        centred = graph.center(Text("abc"), 10)
        assert centred.plain == " " * 3 + "abc"

    def test_a_picture_wider_than_the_panel_is_left_alone(self):
        """Centring an over-wide picture would push its left edge past the
        scroll origin, where it cannot be scrolled back to."""
        wide = Text("x" * 40)
        assert graph.center(wide, 20) is wide

    def test_centre_is_a_no_op_on_an_exact_fit(self):
        exact = Text("y" * 10)
        assert graph.center(exact, 10) is exact


class TestNodeRects:
    def test_every_drawn_box_gets_a_rectangle(self):
        rects = graph.node_rects(graph.build_mermaid(realistic()))
        assert "YOU" in rects
        assert "svc_default_web_svc" in rects
        # x, y, w, h — all four present and positive
        for rect in rects.values():
            assert len(rect) == 4
            assert rect[2] > 0 and rect[3] > 0

    def test_rects_are_within_the_rendered_picture(self):
        source = graph.build_mermaid(realistic())
        rects = graph.node_rects(source)
        height = len(graph.render(source, 200).plain.splitlines())
        assert all(y < height for _, y, _, _ in rects.values())
