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

from fixtures_cluster_graph import broken, cross_namespace, realistic  # noqa: E402
from kubby.tui import diagram, draw, paint, place  # noqa: E402

#: Characters only an edge would draw. A box's own borders come from its
#: shape, and none of these is one of them.
#:
#: The vertical bar belongs here and used to be missing, which left
#: `test_nothing_is_drawn_inside_a_box_but_its_own_text` unable to see a
#: *vertical* edge inside a box — the one shape a stacked pair produces, and
#: the one a narrow panel produces most of.
EDGE_CHARS = "─│├┤┬┴┼"

#: The subset that must never appear at all. An edge that merges into a
#: border produces these, and a different one on every crossing.
JUNCTION_CHARS = "├┤┬┴┼"

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


def _contained(built: diagram.Diagram, container: diagram.Container) -> list[str]:
    """Every box inside *container*, however deep.

    Containers nest now, so a container's own ``members`` are only the boxes
    with no frame of their own inside it — `your computer` and `minikube`
    hold none at all. What a frame is *sized* to is everything within it.
    """
    out = list(container.members)
    for child in built.children_of(container.id):
        out.extend(_contained(built, child))
    return out


def _reading_order(built: diagram.Diagram) -> list[diagram.Container]:
    """Containers in the order a reader meets them, outermost first."""
    out: list[diagram.Container] = []

    def walk(parent: str | None) -> None:
        for container in built.children_of(parent):
            out.append(container)
            walk(container.id)

    walk(None)
    return out


#: Which way each arrowhead points, as a step. A head is placed *beside* the
#: border it points at, so the box it belongs to is one cell further on.
_ARROW_STEP = {"\u25ba": (1, 0), "\u25c4": (-1, 0), "\u25bc": (0, 1), "\u25b2": (0, -1)}


def _arrow_cells(lines: list[str]) -> list[tuple[int, int, tuple[int, int]]]:
    """Every arrowhead in a rendered picture, with the way it points."""
    out: list[tuple[int, int, tuple[int, int]]] = []
    for y, row in enumerate(lines):
        for x, char in enumerate(row):
            step = _ARROW_STEP.get(char)
            if step is not None:
                out.append((x, y, step))
    return out


def _is_line(lines: list[str], x: int, y: int) -> bool:
    """Whether a line character is at ``(x, y)``, in bounds or not."""
    if not (0 <= y < len(lines) and 0 <= x < len(lines[y])):
        return False
    return lines[y][x] in EDGE_CHARS


def _has_lane_edge(built, placement, box) -> bool:
    """Whether *box* is one end of an edge joined down a reserved lane."""
    for source, target in placement.edges:
        if box.id not in (source, target):
            continue
        other = placement.boxes[target if box.id == source else source]
        if other is box:
            continue
        # Stacked: the only shape a lane is reserved for.
        if other.y > box.y and box.x < other.x + other.width \
                and other.x < box.x + box.width:
            return True
    return False


def _within(outer, inner) -> bool:
    return (
        outer.x <= inner.x
        and outer.y <= inner.y
        and inner.x + inner.width <= outer.x + outer.width
        and inner.y + inner.height <= outer.y + outer.height
    )


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
        assert "2 of 16 cpu" in "\n".join(_box(built, "minikube").lines)

    def test_no_edge_runs_from_the_node_to_the_control_plane(self):
        """Seven of them became a picket fence down one lane. The reading
        order already says the control plane runs on the node."""
        built = diagram.build_diagram(realistic())
        node = _box(built, "minikube").id
        assert not [e for e in built.edges if e[0] == node]

    def test_an_unavailable_cluster_draws_nothing(self):
        assert diagram.build_diagram({"available": False, "error": "nope"}).nodes == {}

    def test_a_namespace_is_one_container_however_its_members_link(self):
        """Grouping by connectivity split `default` into pieces and repeated
        its name once per disconnected piece."""
        built = diagram.build_diagram(realistic())
        containers = {c.label: c for c in built.containers}
        assert "default" in containers
        # six objects in the namespace, plus its Ingress
        assert len(containers["default"].members) == 7

    def test_no_frame_wraps_the_whole_picture(self):
        """`your computer` and `minikube` are gone.

        The machine minikube made is not a box: the cluster panel's own
        border is where the cluster starts, and the sidebar describes the
        machine in words. A frame around everything said it twice and cost
        the picture a row and six columns.
        """
        built = diagram.build_diagram(realistic())
        labels = {c.label for c in built.containers}
        assert "your computer" not in labels
        assert "minikube" not in labels

    def test_containment_is_a_container_and_not_an_edge(self):
        """A namespace contains its workloads, so it is a box they sit
        inside; a Service points at the workload it backs, so it is a line
        between them. Containment as adjacency would make the two look like
        the same kind of fact."""
        built = diagram.build_diagram(realistic())
        by_id = {c.id: c for c in built.containers}
        assert by_id["ns:default"].members
        for container in built.containers:
            assert not [e for e in built.edges if e[0] == container.id]

    def test_one_node_is_described_in_words_not_drawn_as_a_box(self):
        """A single node's box said what the sidebar's machine panel says,
        twice over. With several nodes the boxes are needed — and so is the
        frame, or node boxes beside the namespace frames would read as peers
        of them rather than as the machines they run on."""
        model = realistic()
        model["node_facts"] = model["node_facts"][:1]
        model["nodes"] = model["nodes"][:1]
        one = diagram.build_diagram(model)
        assert "the nodes" not in {c.label for c in one.containers}
        assert not [n for n in one.nodes.values() if n.role == "node"]

        two = diagram.build_diagram(realistic())
        by_id = {c.id: c for c in two.containers}
        assert by_id["nodes"].members
        assert len([n for n in two.nodes.values() if n.role == "node"]) == 2

    def test_nothing_outside_the_cluster_is_drawn(self):
        """No browser, no "you". The picture is of the cluster, not of an
        application architecture — a client on the other end of a request is
        not part of the cluster, and drawing it made the picture assert
        things it could not know."""
        built = diagram.build_diagram(realistic())
        assert "client" not in diagram.ROLES
        for node in built.nodes.values():
            assert "browser" not in " ".join(node.lines)
            assert node.group != "client"

    def test_an_ingress_in_an_omitted_namespace_is_not_drawn(self):
        """A box with no frame to sit in is worse than no box.

        Services already skip omitted namespaces; an Ingress that did not
        was added to the diagram, never placed (no container is built for an
        omitted namespace), and its edge silently dropped by the
        `source in placement.boxes` filter. The picture just lost the box
        and said nothing.
        """
        model = realistic()
        model["ingresses"] = model["ingresses"] + [
            {
                "name": "web",
                "namespace": "ingress-nginx",
                "rules": [{"host": "web.example"}],
            }
        ]
        built = diagram.build_diagram(model)
        assert "ingress-nginx" not in {c.label for c in built.containers}
        assert not [
            n for n in built.nodes.values() if n.group == "ingress-nginx"
        ]

    def test_a_malformed_ingress_does_not_take_the_picture_down(self):
        """One bad object from the API should cost one box, not the picture.
        The normalising layer this renderer replaced used to default these
        fields, and losing that turned a partial ingress into a KeyError."""
        model = realistic()
        model["ingresses"] = model["ingresses"] + [
            {"namespace": "default", "rules": [{}, "not a rule"]},
            "not even a dict",
        ]
        built = diagram.build_diagram(model)
        assert built.nodes
        assert any(n.lines[0] == "?" for n in built.nodes.values())

    def test_an_ingress_is_still_drawn_and_still_links_to_its_service(self):
        """An Ingress is a real object in the cluster with a real backend, so
        it belongs in the picture. Only the client goes."""
        built = diagram.build_diagram(realistic())
        shop = _box(built, "shop")
        assert shop.group == "default"
        assert (shop.id, _box(built, "web-svc").id) in built.edges


# ---------------------------------------------------------------------------
# placement
# ---------------------------------------------------------------------------


class TestPlace:
    @staticmethod
    def _widest_single_group(built: diagram.Diagram) -> int:
        """How wide the widest group is, in columns, laid out on its own.

        A group cannot be split across bands — it is a namespace, and a
        namespace shown in two pieces with its name on both is worse than one
        that overflows. So below this width the picture legitimately
        overflows and the panel scrolls.
        """
        widest = 1
        for container in built.containers:
            members = container.members
            placement = place.place(
                diagram.Diagram(
                    nodes={m: built.nodes[m] for m in members},
                    containers=[
                        diagram.Container(
                            id=container.id, label=container.label, members=members
                        )
                    ],
                    edges=[
                        e for e in built.edges
                        if e[0] in members and e[1] in members
                    ],
                ),
                10_000,
            )
            widest = max(
                widest,
                max((b.x + b.width for b in placement.boxes.values()), default=0),
            )
        return widest

    @pytest.mark.parametrize("width", [40, 62, 80, 100, 140, 200])
    def test_the_picture_never_exceeds_the_width_asked_for(self, width):
        """The assertion that would have caught "it does not scale with the
        screen size": the old renderer returned byte-identical output at 62
        and at 84 columns, 112 wide either way.

        The allowance is the widest single group, because a group narrower
        than nothing is not a thing — a namespace with seven objects in it
        is 47 columns wide whatever the panel is, and the panel scrolls.
        What must not happen is a *band* growing with the panel, which is
        what the old renderer did.
        """
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        widest = max((b.x + b.width for b in placement.boxes.values()), default=0)
        floor = self._widest_single_group(built)
        assert widest <= max(width, floor), f"{widest} > {width} (floor {floor})"

    def test_a_narrow_panel_wraps_rather_than_overflowing(self):
        """Wrapping used to be between top-level containers, and there is now
        only one of those, so it happens inside them: a namespace too wide
        for the panel stacks its columns into rows instead. The observable
        consequence is the same either way — the picture gets taller, not
        wider."""
        built = diagram.build_diagram(realistic())
        narrow = place.place(built, 40)
        wide = place.place(built, 200)
        assert narrow.height > wide.height
        assert narrow.width <= wide.width

    def test_containers_are_ordered_control_plane_first_your_code_last(self):
        built = diagram.build_diagram(realistic())
        order = [c.label for c in _reading_order(built)]
        assert order[0] == "the nodes"
        # the control plane before the reader's own namespaces
        assert order.index("kube-system") < order.index("default")

    def test_an_edge_within_a_group_points_rightwards(self):
        built = diagram.build_diagram(realistic())
        placement = place.place(built, 200)
        for source, target in built.edges:
            src, dst = placement.boxes.get(source), placement.boxes.get(target)
            if src and dst and src.band == dst.band:
                assert dst.x > src.x, f"{source} -> {target} goes backwards"

    def test_a_service_sits_level_with_the_workload_it_feeds(self):
        """Boxes in a layer are ordered by barycentre, with unconnected ones
        last. Without it `db-svc` sorted above `web-svc`, the edge to `web`
        had to double back, and its horizontal run landed on db-svc's row —
        so one correct edge looked like it came out of the wrong box.
        """
        model = realistic()
        model["namespaces"] = [
            ns for ns in model["namespaces"] if ns["name"] == "default"
        ]
        model["services"] = [
            s for s in model["services"] if s["namespace"] == "default"
        ]
        model["ingresses"] = []
        built = diagram.build_diagram(model)
        placement = place.place(built, 200)
        svc = placement.boxes[_box(built, "web-svc").id]
        web = placement.boxes[_box(built, "web").id]
        assert abs(svc.y - web.y) <= max(svc.height, web.height)

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

    def test_a_frame_whose_boxes_stack_reserves_a_lane_to_join_them(self):
        """Narrow panels stack a namespace's boxes in one column, so the edge
        between two of them has to go down. The only column free to go down
        is one the layout set aside — the alternative is the frame's own
        border, and an edge across that is the inconsistent-junction problem
        the whole renderer exists to avoid."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, 40)
        stacked = {
            frame.id
            for frame in placement.containers.values()
            if frame.lane >= 0
        }
        # Everything is in one band now, so a lane is the only routing a
        # narrow panel has.
        assert stacked
        for container_id in stacked:
            frame = placement.containers[container_id]
            assert frame.x < frame.lane < frame.x + frame.width - 1
            # and nothing is drawn in it
            for box in placement.boxes.values():
                if box.x <= frame.lane <= box.x + box.width - 1:
                    assert not (frame.y <= box.y and box.y < frame.y + frame.height)

    def test_an_empty_diagram_places_to_nothing(self):
        assert place.place(diagram.Diagram(), 80).boxes == {}

    @pytest.mark.parametrize("width", [62, 90, 140, 200])
    def test_every_box_sits_inside_its_container(self, width):
        """Containment is a frame, not proximity. A reader should not have to
        infer which boxes belong together from which ones are nearby."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        for container in built.containers:
            frame = placement.containers[container.id]
            for member in container.members:
                box = placement.boxes[member]
                assert frame.x <= box.x, container.label
                assert box.x + box.width <= frame.x + frame.width, container.label
                assert frame.y <= box.y, container.label
                assert box.y + box.height <= frame.y + frame.height, container.label

    @pytest.mark.parametrize("width", [62, 90, 140, 200])
    def test_a_container_is_sized_to_its_own_contents(self, width):
        """Not to the band, and not to its parent. Sizing to the band padded
        every container out to the height of the tallest one beside it."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        for container in built.containers:
            frame = placement.containers[container.id]
            inside = [placement.boxes[m] for m in _contained(built, container)]
            if not inside:
                continue
            lowest = max(b.y + b.height for b in inside)
            assert frame.y + frame.height - lowest <= 3, container.label

    @pytest.mark.parametrize("width", [62, 90, 140, 200])
    def test_containers_never_overlap_unless_one_holds_the_other(self, width):
        """Two frames either sit apart or one is inside the other. Anything
        else means two frames are drawn across each other, which reads as a
        box that is somehow both of them."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        frames = list(placement.containers.values())
        for index, first in enumerate(frames):
            for second in frames[index + 1:]:
                apart = (
                    first.x + first.width <= second.x
                    or second.x + second.width <= first.x
                    or first.y + first.height <= second.y
                    or second.y + second.height <= first.y
                )
                nested = _within(first, second) or _within(second, first)
                assert apart or nested, (first.label, second.label)

    def test_a_container_label_is_not_truncated(self):
        """The frame is only as wide as its contents, so a long title has to
        be short enough to fit or it is cut mid-word."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, 200)
        for frame in placement.containers.values():
            rendered = paint.render(placement).plain
            assert frame.label in rendered, frame.label


# ---------------------------------------------------------------------------
# painting — the guarantees
# ---------------------------------------------------------------------------


class TestPaint:
    @pytest.mark.parametrize("model", [realistic, broken, cross_namespace])
    @pytest.mark.parametrize("width", list(range(20, 121, 4)))
    def test_no_edge_is_drawn_in_pieces(self, model, width):
        """Every cell an edge asks for, it gets.

        The one property that holds the routing together. A run that is
        refused a cell is a line with a piece missing, and the arrowhead
        beyond it reads as pointing at something the line never reached —
        a connection the reader will believe is not there. It is also the
        only assertion in this file that could have caught the four routing
        bugs it now guards: a detour drawn along a box's border row, an
        arrowhead a column wide of its target, a descent straight through
        the next box down, and a cross-namespace edge severed by two frames.

        A route that cannot find clear space draws no edge at all rather
        than a fragment, and that is not a failure of this property — a
        missing line is a gap the reader can see.
        """
        built = diagram.build_diagram(model())
        placement = place.place(built, width)
        _picture, severed = paint.render_checked(placement)
        assert severed == 0, f"{severed} cells refused at width {width}"

    @pytest.mark.parametrize("model", [realistic, cross_namespace])
    def test_every_edge_that_can_be_drawn_is_drawn(self, model):
        """The companion to the above: not drawing an edge is worse than
        drawing it badly, so the same sweep checks that an edge exists for
        every link the model has, at every width."""
        built = diagram.build_diagram(model())
        expected = len(built.edges)
        assert expected
        for width in range(20, 121, 4):
            placement = place.place(built, width)
            assert len(placement.edges) == expected, width

    @pytest.mark.parametrize("model", [realistic, broken, cross_namespace])
    @pytest.mark.parametrize("width", list(range(28, 121, 3)))
    def test_every_arrowhead_points_at_a_box(self, model, width):
        """The cell an arrowhead points into is a box's border, and a line
        reaches the arrowhead from behind.

        One cell from the border, and never two: a head two cells out points
        at a blank cell, and a head *on* the border is a glyph sitting on top
        of one. The narrow-panel case is where the target is to the left of
        the source, which takes the detour route, and it is where both were
        wrong at once — the head landed beside nothing at all."""
        built = diagram.build_diagram(model())
        placement = place.place(built, width)
        lines = paint.render(placement).plain.splitlines()
        # Every cell a box owns, border and interior both. A head that comes
        # in from the side is placed *beside* the border and points at it; a
        # head that comes in from above is placed *on* the border, which is
        # the long-standing ASCII idiom for "it enters here", and points into
        # the box. Both point at a box, and that is the property.
        owned = {
            (x, y)
            for box in placement.boxes.values()
            for y in range(box.y, box.y + box.height)
            for x in range(box.x, box.x + box.width)
        }
        for x, y, facing in _arrow_cells(lines):
            ahead = (x + facing[0], y + facing[1])
            assert ahead in owned, (
                f"arrowhead at ({x},{y}) points at ({ahead[0]},{ahead[1]}), "
                f"which belongs to no box, at width {width}"
            )
            # And something is attached to it. Which side depends on the
            # route: a rightward edge is fed from the left, but the detour
            # that rejoins a target stacked below comes down beside it and
            # arrives from above. So: any neighbour, just not the border it
            # points into. A head with nothing at all around it is a head
            # whose line was severed.
            attached = any(
                (x + dx, y + dy) != ahead and _is_line(lines, x + dx, y + dy)
                for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0))
            )
            assert attached, (
                f"arrowhead at ({x},{y}) has no line touching it, "
                f"at width {width}"
            )

    @pytest.mark.parametrize("width", [20, 24, 30, 40, 62])
    def test_a_vertical_edge_stays_inside_its_own_frame(self, width):
        """The lane a stacked pair is joined down is reserved *by their
        frame*. Taking the innermost frame that merely started above both
        boxes picked one in another band, and the edge was drawn out of its
        own container and across a border to get there."""
        built = diagram.build_diagram(realistic())
        placement = place.place(built, width)
        for frame in placement.containers.values():
            if frame.lane < 0:
                continue
            inside = [
                b
                for b in placement.boxes.values()
                if frame.x <= b.x and b.x + b.width <= frame.x + frame.width
                and frame.y <= b.y and b.y + b.height <= frame.y + frame.height
            ]
            for box in inside:
                # A box may occupy the lane's column only if it is not one
                # of the boxes the lane exists to join.
                if box.x <= frame.lane <= box.x + box.width - 1:
                    assert not _has_lane_edge(built, placement, box), box.node.lines

    def test_a_cross_namespace_edge_is_drawn_all_the_way(self):
        """An Ingress whose backend is another namespace's Service, in the
        band arrangement where the two namespaces share a band. It used to
        run the whole width of the first frame, be refused by that frame's
        border, cross a box, and be refused again — arriving at the target
        as an arrowhead with nothing attached to it."""
        built = diagram.build_diagram(cross_namespace())
        placement = place.place(built, 44)
        lines = paint.render(placement).plain.splitlines()
        shop = next(
            b for b in placement.boxes.values() if b.node.lines[0] == "shop"
        )
        backend = next(
            b
            for b in placement.boxes.values()
            if b.node.lines[0] == "postgres" and b.x > shop.x
        )
        # A line leaves the source and one arrives at the target, on the
        # rows the two boxes sit on.
        assert any(
            _is_line(lines, x, y)
            for y in range(shop.y - 1, shop.y + shop.height + 1)
            for x in range(shop.x, shop.x + shop.width + 2)
        ), "nothing leaves the ingress"
        assert any(
            lines[y][x] in ARROWS
            for y in range(backend.y, backend.y + backend.height)
            for x in range(backend.x - 1, backend.x + backend.width + 1)
        ), "nothing arrives at the backend"

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

    @pytest.mark.parametrize("model", [realistic, cross_namespace])
    @pytest.mark.parametrize("width", [30, 40, 62, 80, 120])
    def test_an_edge_never_produces_a_junction(self, model, width):
        """No ``├┼─►``. A junction character is what an edge looks like when
        it has been merged into a border, and it is a different character on
        every crossing, which is the inconsistency this renderer is for."""
        built = diagram.build_diagram(model())
        rendered = paint.render(place.place(built, width)).plain
        for char in JUNCTION_CHARS:
            assert char not in rendered, f"{char} at width {width}"

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
