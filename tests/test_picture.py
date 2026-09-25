"""Tests for the canvas, the placement and the painting.

Three of these assertions are the ones that would have caught the problems
that prompted this rewrite, and they are written as properties of the
finished picture rather than as expected output, so they keep holding as the
layout changes:

* the picture never exceeds the width it was asked for
* nothing is drawn inside a box except the box's own text
* a box's padding is symmetric
"""

from __future__ import annotations

import sys

import pytest

sys.path.insert(0, "tests")

from fixtures_cluster_graph import broken, realistic  # noqa: E402
from kubby.tui import diagram, draw, paint, place  # noqa: E402

#: Characters only an edge would draw. A box's own borders come from its
#: shape, and none of these is one of them.
EDGE_CHARS = "─├┤┬┴┼"
ARROWS = "►◄▼▲"


# ---------------------------------------------------------------------------
# the canvas
# ---------------------------------------------------------------------------


class TestCanvas:
    def test_box_draws_a_complete_border(self):
        canvas = draw.Canvas(20, 6)
        canvas.box(draw.Rect(1, 1, 10, 4), "round")
        lines = canvas.plain_lines()
        assert lines[1] == " ╭────────╮"
        assert lines[2] == " │        │"
        assert lines[4] == " ╰────────╯"

    def test_every_shape_draws_its_own_characters(self):
        for shape, (tl, tr, bl, br) in (
            ("rect", ("┌", "┐", "└", "┘")),
            ("round", ("╭", "╮", "╰", "╯")),
            ("heavy", ("┏", "┓", "┗", "┛")),
            ("double", ("╔", "╗", "╚", "╝")),
        ):
            canvas = draw.Canvas(12, 5)
            canvas.box(draw.Rect(0, 0, 8, 4), shape)
            lines = canvas.plain_lines()
            assert (lines[0][0], lines[0][7]) == (tl, tr), shape
            assert (lines[3][0], lines[3][7]) == (bl, br), shape

    def test_a_locked_cell_refuses_a_write(self):
        canvas = draw.Canvas(12, 5)
        canvas.box(draw.Rect(1, 1, 6, 3), "rect")
        assert canvas.put(1, 1, "X") is False
        assert canvas.put(0, 0, "X") is True

    def test_the_whole_box_is_locked_not_just_the_border(self):
        """An edge routed across a box's middle is refused rather than drawn
        through the label."""
        canvas = draw.Canvas(12, 5)
        canvas.box(draw.Rect(1, 1, 8, 3), "rect")
        assert canvas.put(4, 2, "X") is False

    def test_label_is_centred(self):
        canvas = draw.Canvas(20, 6)
        rect = draw.Rect(1, 1, 12, 4)
        canvas.box(rect, "rect")
        canvas.label(rect, ["ab", "cd"])
        lines = canvas.plain_lines()
        for row, text in ((2, "ab"), (3, "cd")):
            inner = lines[row][2:-2]
            lead = len(inner) - len(inner.lstrip())
            trail = len(inner) - len(inner.rstrip())
            # Centred to within a cell: when the slack is odd it cannot be
            # exactly equal, and a label shoved to one side reads as though
            # it were escaping the box.
            assert abs(lead - trail) <= 1, (text, inner)

    def test_label_never_writes_over_the_border(self):
        canvas = draw.Canvas(20, 6)
        rect = draw.Rect(1, 1, 6, 3)
        canvas.box(rect, "rect")
        canvas.label(rect, ["waaaaaaytoolong"])
        lines = canvas.plain_lines()
        # Corners at 1 and 6; the run between them is border, not text.
        assert lines[1][1] == "┌" and lines[1][6] == "┐"
        assert lines[2][1] == "│" and lines[2][6] == "│"
        assert set(lines[1][2:6]) == {"─"}

    def test_a_box_overrunning_the_canvas_is_clipped_not_an_error(self):
        canvas = draw.Canvas(10, 4)
        canvas.box(draw.Rect(6, 2, 20, 8), "rect")
        assert canvas.width == 10

    def test_line_stops_at_a_locked_cell(self):
        canvas = draw.Canvas(20, 3)
        canvas.box(draw.Rect(8, 0, 4, 3), "rect")
        canvas.line([(0, 1), (19, 1)])
        row = canvas.plain_lines()[1]
        assert row[7] == "─"
        assert row[8] == "│"  # the border, untouched

    def test_an_arrowhead_can_be_forced_onto_a_border(self):
        """An edge arriving from above ends with its head in the middle of
        the border it enters, which is the long-standing ASCII idiom."""
        canvas = draw.Canvas(12, 4)
        canvas.box(draw.Rect(2, 1, 6, 3), "rect")
        assert canvas.arrow_head(4, 1, "down") is False
        assert canvas.arrow_head(4, 1, "down", force=True) is True
        assert canvas.plain_lines()[1][4] == "▼"

    def test_rect_reports_its_own_geometry(self):
        rect = draw.Rect(3, 4, 10, 5)
        assert (rect.right, rect.bottom) == (12, 8)
        assert (rect.inner_width, rect.inner_height) == (8, 3)
        assert rect.contains(3, 4) and not rect.contains(13, 4)


# ---------------------------------------------------------------------------
# the diagram: what gets drawn
# ---------------------------------------------------------------------------


def _box(built: diagram.Diagram, name: str) -> diagram.Node:
    return built.nodes[next(n.id for n in built.nodes.values() if n.lines[0] == name)]


class TestDiagram:
    def test_one_box_per_workload_service_and_ingress(self):
        built = diagram.build_diagram(realistic())
        labels = {tuple(n.lines) for n in built.nodes.values()}
        assert ("web", "2/2") in labels
        assert ("web-svc", "80") in labels
        assert ("shop", "shop.example") in labels

    def test_replicas_collapse_into_one_workload_box(self):
        built = diagram.build_diagram(realistic())
        assert len([n for n in built.nodes.values() if n.lines[0] == "web"]) == 1

    def test_edges_are_deduplicated(self):
        """A Service with two replicas links to one Deployment twice. Two
        arrows on top of each other read as a heavier arrow."""
        built = diagram.build_diagram(realistic())
        assert len(built.edges) == len(set(built.edges))

    def test_a_service_backed_by_nothing_has_no_outgoing_edge(self):
        built = diagram.build_diagram(realistic())
        db = _box(built, "db-svc")
        assert not [e for e in built.edges if e[0] == db.id]

    def test_selector_matched_nothing_is_the_red_one(self):
        built = diagram.build_diagram(realistic())
        db = _box(built, "db-svc")
        assert db.broken_service is True
        assert db.healthy is False

    def test_selectorless_service_is_not_called_broken(self):
        """The API server's own Service has no selector. Reporting it as
        broken would condemn the healthiest thing in the cluster."""
        built = diagram.build_diagram(realistic())
        k8s = _box(built, "kubernetes")
        assert k8s.broken_service is False
        assert k8s.healthy is True

    def test_a_workload_with_an_unready_pod_is_not_healthy(self):
        """Judged on container readiness, not phase: a container in
        CrashLoopBackOff keeps its pod in phase Running until it gives up."""
        assert _box(diagram.build_diagram(broken()), "web").healthy is False

    def test_the_ingress_controller_namespace_is_left_out(self):
        model = realistic()
        model["namespaces"].append(
            {"name": "ingress-nginx", "collapsed": True, "workload_count": 1,
             "pods": [{"name": "controller-x", "namespace": "ingress-nginx",
                       "phase": "Running", "ready": True, "node": "minikube",
                       "ip": "", "owner_kind": "ReplicaSet",
                       "owner_name": "controller-abc",
                       "workload": {"name": "controller", "kind": "Deployment",
                                    "inferred": False}}]}
        )
        model["services"].append(
            {"name": "ingress-nginx-controller", "namespace": "ingress-nginx",
             "type": "ClusterIP", "cluster_ip": "", "has_selector": True,
             "ports": [{"port": 443, "target": "443", "node_port": None}],
             "backing_pods": [{"namespace": "ingress-nginx", "pod": "controller-x"}]}
        )
        built = diagram.build_diagram(model)
        assert not any("ingress-nginx" in n.id for n in built.nodes.values())

    def test_the_dns_service_box_is_not_drawn(self):
        """coredns's own label already says what DNS does."""
        built = diagram.build_diagram(realistic())
        assert not any(n.lines[0] == "kube-dns" for n in built.nodes.values())

    def test_a_control_plane_component_is_labelled_with_its_job(self):
        built = diagram.build_diagram(realistic())
        assert _box(built, "etcd").lines == ["etcd", "all cluster state lives here"]

    def test_the_node_carries_its_allocatable_share(self):
        built = diagram.build_diagram(realistic())
        assert "2 of 16 cpu" in _box(built, "minikube").lines[-1]

    def test_no_edge_runs_from_the_node_to_the_control_plane(self):
        """Seven of them became a picket fence down one lane. The reading
        order already says the control plane runs on the node."""
        built = diagram.build_diagram(realistic())
        node = _box(built, "minikube").id
        assert not [e for e in built.edges if e[0] == node]

    def test_an_unavailable_cluster_draws_nothing(self):
        assert diagram.build_diagram({"available": False, "error": "nope"}).nodes == {}

    def test_a_namespace_is_one_group_however_its_members_link(self):
        """Grouping by connectivity split `default` into pieces and repeated
        its name once per disconnected piece."""
        built = diagram.build_diagram(realistic())
        groups = dict(built.groups())
        assert "default" in groups and "host" in groups
        assert len(groups["default"]) == 6


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


class TestPlace:
    @pytest.mark.parametrize("width", [40, 62, 80, 100, 140, 200])
    def test_the_picture_never_exceeds_the_width_asked_for(self, width):
        """The assertion that would have caught "it does not scale with the
        screen size": the old renderer returned byte-identical output at 62
        and at 84 columns, 112 wide either way."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        widest = max((b.x + b.width for b in placement.boxes.values()), default=0)
        assert widest <= width, f"{widest} > {width}"

    def test_a_narrow_panel_wraps_into_bands(self):
        built = diagram.build_diagram(realistic())
        narrow = place.place(built, 40)
        wide = place.place(built, 200)
        assert len({b.band for b in narrow.boxes.values()}) > 1
        assert len({b.band for b in wide.boxes.values()}) < len(
            {b.band for b in narrow.boxes.values()}
        )

    def test_groups_are_ordered_machine_first_your_code_last(self):
        built = diagram.build_diagram(realistic())
        order = [text for _x, _y, text in place.place(built, 200).headings]
        assert order.index("host") < order.index("node")
        assert order.index("node") < order.index("default")

    def test_an_edge_within_a_group_points_rightwards(self):
        built = diagram.build_diagram(realistic())
        placement = place.place(built, 200)
        for source, target in built.edges:
            src, dst = placement.boxes.get(source), placement.boxes.get(target)
            if src and dst and src.band == dst.band:
                assert dst.x > src.x, f"{source} -> {target} goes backwards"

    def test_every_box_gets_a_size_and_a_cell(self):
        placement = place.place(diagram.build_diagram(realistic()), 120)
        assert placement.boxes
        for box in placement.boxes.values():
            assert box.width > 0 and box.height > 0
            assert box.x >= 0 and box.y >= 0

    def test_a_wrapped_band_gets_a_gap_row_to_route_edges_through(self):
        placement = place.place(diagram.build_diagram(realistic()), 40)
        bands = {b.band for b in placement.boxes.values()}
        assert bands, "nothing placed"
        for band in sorted(bands)[:-1]:
            assert band in placement.gaps

    def test_an_empty_diagram_places_to_nothing(self):
        assert place.place(diagram.Diagram(), 80).boxes == {}


# ---------------------------------------------------------------------------
# painting — the guarantees
# ---------------------------------------------------------------------------


class TestPaint:
    @pytest.mark.parametrize("width", [40, 62, 80, 120])
    def test_nothing_is_drawn_inside_a_box_but_its_own_text(self, width):
        """Every box locks its cells and every edge respects the lock, so an
        arrow can never be merged into a border — which is what produced
        `minikube, auto├┼─►│` before."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        lines = paint.render(placement).plain.splitlines()
        for box in placement.boxes.values():
            for y in range(box.y, box.y + box.height):
                row = lines[y] if y < len(lines) else ""
                for x in range(box.x, box.x + box.width):
                    char = row[x] if x < len(row) else " "
                    on_border = (
                        y in (box.y, box.y + box.height - 1)
                        or x in (box.x, box.x + box.width - 1)
                    )
                    if on_border:
                        continue
                    assert char not in EDGE_CHARS, f"edge in {box.node.lines}"
                    assert char not in ARROWS, f"arrowhead in {box.node.lines}"

    def test_a_box_has_symmetric_padding(self):
        """The old renderer moved the gap from both sides to one rather than
        closing it, so lowering its padding made the asymmetry worse."""
        built = diagram.build_diagram(realistic())
        lines = paint.render(place.place(built, 120)).plain.splitlines()
        for box in place.place(built, 120).boxes.values():
            row = lines[box.y + 1]
            left, right = box.x, box.x + box.width - 1
            assert row[left] == row[right], box.node.lines
            inner = row[left + 1:right]
            if inner.strip():
                lead = len(inner) - len(inner.lstrip())
                trail = len(inner) - len(inner.rstrip())
                assert abs(lead - trail) <= 1, box.node.lines

    def test_every_label_fits_inside_its_box(self):
        built = diagram.build_diagram(realistic())
        for box in place.place(built, 120).boxes.values():
            longest = max(len(line) for line in box.node.lines)
            assert longest + 2 <= box.width, box.node.lines

    def test_group_headings_survive_intact(self):
        built = diagram.build_diagram(realistic())
        placement = place.place(built, 62)
        rendered = paint.render(placement).plain
        for _x, _y, text in placement.headings:
            assert text in rendered, text

    def test_a_short_label_becomes_an_ellipse_and_a_long_one_does_not(self):
        """The rule is a predicate on the content rather than a per-node
        exception, so a node added later cannot be left out of it."""
        from kubby.tui.paint import shape_for

        def _box_with(lines: list[str], role: str = "app") -> place.PlacedBox:
            return place.PlacedBox(
                id="x", node=diagram.Node("x", lines, role, "g")
            )

        assert shape_for(_box_with(["web"])) == "ellipse"
        assert shape_for(_box_with(["web-svc", "80"])) == "round"
        assert shape_for(_box_with(["a-very-long-label-indeed"])) == "round"
        # infrastructure is context, and reads as it
        assert shape_for(_box_with(["etcd", "all state"], "infra")) == "heavy"

    def test_an_empty_diagram_renders_without_raising(self):
        assert paint.render(place.place(diagram.Diagram(), 40)) is not None

    def test_centre_only_when_it_fits(self):
        from rich.text import Text

        assert paint.centre(Text("hi"), 20).plain.startswith(" ")
        wide = Text("x" * 40)
        assert paint.centre(wide, 20) is wide
