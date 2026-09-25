"""Placing the picture: our own layout, in columns that fit the panel.

The problem this solves, measured rather than guessed. The cluster graph is
mostly *disconnected* — the control plane's components do not link to each
other, and the node's edges to individual workloads are gone on purpose,
because with a single node they carried no information. A conventional
layered layout therefore puts each of the ~15 components in its own column,
and twenty boxes came out 301 columns wide.

So the layout is built around groups instead:

1. boxes are grouped by namespace, not by connectivity
2. groups are ordered by role — the machine, then what runs on it, then
   your own code — and placed left to right, wrapping to a new band below
3. inside a group, boxes are layered by longest path from its root, so
   each layer is a column and an edge always points rightwards
4. a group that will not fit the remaining width wraps to a new band
   underneath

Step 4 is what makes the picture scale with the terminal instead of
insisting on a certain size.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from kubby.tui.diagram import Diagram, Node

#: Blank columns between two layers. An edge is routed in here, so it has to
#: be wide enough for a lane *and* an arrowhead: four, because with three a
#: stepped edge arriving from above had nowhere to put its head and landed on
#: the border it was aiming at.
GUTTER = 4

#: Blank rows between two boxes stacked in the same column.
NODE_GAP = 1

#: Blank rows between two containers.
COMPONENT_GAP = 2

#: How far a box's text sits from its border. Symmetric — the previous
#: renderer could not manage that, because lowering its padding moved the gap
#: to one side rather than closing it.
BOX_PAD = 1

#: Blank space between a container's border and the boxes inside it.
CONTAINER_PAD = 1


@dataclass
class PlacedBox:
    id: str
    node: Node
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    #: Which horizontal band this box landed in. Edges that cross a band
    #: boundary are routed through the free row between the two bands, which
    #: is only knowable if the bands are numbered.
    band: int = 0


@dataclass
class PlacedContainer:
    """A box drawn *around* its members.

    Containment, not adjacency. A namespace is a frame its workloads sit
    inside, and a reader should not have to infer the grouping from which
    boxes happen to be near each other.
    """

    id: str
    label: str
    detail: str = ""
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    band: int = 0
    #: A free column inside the frame, to the right of its contents, for edges
    #: between boxes stacked in one column to run down. -1 when the frame has
    #: no such column. Reserving it here rather than borrowing one at draw
    #: time is the point: the only column guaranteed to be free is one the
    #: layout put there on purpose, because a borrowed one is the frame's own
    #: border, and an edge drawn across that is the inconsistent-arrow
    #: problem all over again.
    lane: int = -1


@dataclass
class Placement:
    """A laid-out picture, in cells, ready to draw."""

    boxes: dict[str, PlacedBox] = field(default_factory=dict)
    containers: dict[str, PlacedContainer] = field(default_factory=dict)
    edges: list[tuple[str, str]] = field(default_factory=list)
    #: (x, y, text) headings drawn above their component.
    headings: list[tuple[int, int, str]] = field(default_factory=list)
    #: Row of free space between band *n* and band *n+1*, keyed by the lower
    #: band's index. Cross-band edges run along it, which is what keeps them
    #: from having to cross a box.
    gaps: dict[int, int] = field(default_factory=dict)
    #: How far right each band's heading text reaches, keyed by band. An edge
    #: descending into a band steps to the right of this, so it cannot cut a
    #: heading in half — `kubby│demo` is not a thing anyone wants to read.
    heading_right: dict[int, int] = field(default_factory=dict)
    width: int = 0
    height: int = 0

    def rect(self, box_id: str) -> tuple[int, int, int, int]:
        box = self.boxes[box_id]
        return box.x, box.y, box.width, box.height


def place(diagram: Diagram, width: int) -> Placement:
    """Lay *diagram* out to fit *width* columns, wrapping where it must.

    A container is placed whole, so wrapping can never split one across two
    bands and a frame is always exactly as big as what is inside it. A
    container may hold other containers as well as boxes, and those are
    placed inside it rather than beside it.
    """
    placement = Placement()
    if not diagram.nodes and not diagram.containers:
        return placement

    band_top = 0
    band_bottom = 0
    band = 0
    x = 0

    for top in diagram.ordered_containers():
        frame_width, _ = _measure(top, diagram, width)

        if x and x + GUTTER + frame_width > width:
            # This container cannot start on the current band. Wrap, leaving
            # a clear row between the two so a cross-band edge has somewhere
            # to run.
            placement.gaps[band] = band_bottom + 1
            band += 1
            band_top = band_bottom + COMPONENT_GAP
            x = 0

        offset = x + (GUTTER if x else 0)
        # Emit into the width that is actually left on this band, not the
        # full panel width: the contents wrap to *available*, so measuring
        # wide and placing narrow would lay the frame out wider than the
        # room it has and push the picture past the width asked for.
        remaining = max(12, width - offset)
        frame = _emit(top, diagram, placement, offset, band_top, band, remaining)
        if frame is None:
            continue
        title = f" {top.label}" + (f" · {top.detail} " if top.detail else " ")
        placement.heading_right[band] = max(
            placement.heading_right.get(band, 0), offset + 2 + len(title)
        )
        band_bottom = max(band_bottom, frame.y + frame.height - 1)
        x = offset + frame.width

    placement.width = max(
        max((b.x + b.width for b in placement.boxes.values()), default=1),
        max((c.x + c.width for c in placement.containers.values()), default=1),
    )
    placement.height = max(
        max((b.y + b.height for b in placement.boxes.values()), default=1),
        max((c.y + c.height for c in placement.containers.values()), default=1),
        band_bottom + 1,
    )
    for source, target in diagram.edges:
        if source in placement.boxes and target in placement.boxes:
            placement.edges.append((source, target))
    return placement


def _inner_width(container_id: str, diagram: Diagram, available: int) -> int:
    """How much room a container's contents have, once its borders take
    their share."""
    depth = 0
    current: str | None = container_id
    while current is not None:
        found = next(
            (c for c in diagram.containers if c.id == current), None
        )
        if found is None:
            break
        depth += 1
        current = found.parent
    return max(12, available - depth * (CONTAINER_PAD * 2 + 1))


def _box_columns(container: Any, diagram: Diagram) -> list[tuple[int, int, list]]:
    """A container's own boxes, grouped into columns by layer.

    Returns ``[(width, height, boxes), ...]``, one entry per column.
    """
    loose = [
        node_id
        for node_id in container.members
        if node_id in diagram.nodes
        and (
            (owner := diagram.container_of(node_id)) is None
            or owner.id == container.id
        )
    ]
    if not loose:
        return []
    stacks = _columns(
        loose, _layer(loose, diagram), _order(loose, diagram), diagram.nodes
    )
    out: list[tuple[int, int, list]] = []
    for stack in stacks:
        width = max((b.width for b in stack), default=0)
        height = sum(b.height + NODE_GAP for b in stack) - NODE_GAP
        out.append((width, height, stack))
    return out


def _flow(items: list[tuple[int, int, Any]], available: int) -> list[list[tuple[int, int, Any]]]:
    """Pack items into rows no wider than *available*.

    Without this a container lays all of its contents out in one row, so four
    namespaces came out 254 columns wide — the nesting compounded the width
    instead of containing it.
    """
    rows: list[list[tuple[int, int, Any]]] = []
    row: list[tuple[int, int, Any]] = []
    used = 0
    for item in items:
        width = item[0]
        if row and used + width > available:
            rows.append(row)
            row, used = [], 0
        row.append(item)
        used += width + GUTTER
    if row:
        rows.append(row)
    return rows or [[]]


#: A free column kept inside a frame for edges to run down. See
#: ``PlacedContainer.lane``.
CONTAINER_LANE = 1


def _content_size(
    container: Any, diagram: Diagram, available: int
) -> tuple[int, int, int, list]:
    """The contents' size, the lane they need, and the flow they packed into.

    Returns ``(width, height, lane_offset, rows)``. *lane_offset* is where
    inside the frame the spare routing column goes, or -1 when none is
    needed. It is measured against the frame's content origin, so the caller
    adds its own ``inner_x``.
    """
    columns = _box_columns(container, diagram)
    items: list[tuple[int, int, Any]] = [
        (w, h, ("boxes", boxes)) for w, h, boxes in columns
    ]
    for child in diagram.children_of(container.id):
        child_width, child_height = _measure(child, diagram, available)
        items.append((child_width, child_height, ("container", child)))

    rows = _flow(items, max(12, available - CONTAINER_PAD * 2 - 1))
    content_width = 0
    content_height = 0
    for row in rows:
        row_width = sum(i[0] for i in row) + GUTTER * max(0, len(row) - 1)
        row_height = max((i[1] for i in row), default=0)
        content_width = max(content_width, row_width)
        content_height += row_height
    if len(rows) > 1:
        content_height += COMPONENT_GAP * (len(rows) - 1)

    lane = -1
    if _needs_lane(diagram, rows):
        lane = content_width
        content_width += CONTAINER_LANE
    return content_width, content_height, lane, rows


def _needs_lane(diagram: Diagram, rows: list) -> bool:
    """Whether any two of a container's own boxes end up in different rows.

    Boxes in the same row are joined by a straight horizontal run, which
    needs nothing reserved. Boxes in different rows are stacked in one
    column — which is what wrapping a narrow panel forces — and the run
    between them has to go down, either through the gap between them or out
    to a column the layout set aside. Reserving it here is deliberate: the
    only column otherwise free is the frame's own border, and an edge drawn
    across that is the inconsistent-junction problem all over again.
    """
    row_of: dict[str, int] = {}
    for index, row in enumerate(rows):
        for _width, _height, (kind, payload) in row:
            if kind != "boxes":
                continue
            for box in payload:
                row_of[box.id] = index
    if not row_of:
        return False
    return any(
        source in row_of and target in row_of and row_of[source] != row_of[target]
        for source, target in diagram.edges
    )


def _measure(container: Any, diagram: Diagram, available: int) -> tuple[int, int]:
    """How big a container's frame will be, without placing anything."""
    inner = _inner_width(container.id, diagram, available)
    content_width, content_height, _lane, _rows = _content_size(container, diagram, inner)
    frame_width = max(
        content_width + CONTAINER_PAD * 2 + 1,
        _title_width(container, content_width),
    )
    return frame_width, content_height + CONTAINER_PAD * 2 + 1


def _title_width(container: Any, inner: int) -> int:
    """How wide the frame must be to hold its own title without cutting it.

    A title longer than the contents would otherwise be truncated, which is
    how ``minikube · v1.35.1 · 192.168.49.2 · 16 cpu, 15.6Gi`` came out as
    ``minikube · v1.35.1 · 192.16``.
    """
    title = len(container.label) + (len(container.detail) + 3 if container.detail else 0) + 5
    return min(max(title, inner + 2 * CONTAINER_PAD + 1), 10_000)


def _emit(
    container: Any,
    diagram: Diagram,
    placement: Placement,
    x: int,
    y: int,
    band: int,
    available: int,
) -> PlacedContainer | None:
    """Place a container's contents and draw its frame around them."""
    inner_available = _inner_width(container.id, diagram, available)
    content_width, content_height, lane, rows = _content_size(
        container, diagram, inner_available
    )
    frame_width = max(
        content_width + CONTAINER_PAD * 2 + 1,
        _title_width(container, content_width),
    )
    frame = PlacedContainer(
        id=container.id,
        label=container.label,
        detail=container.detail,
        x=x,
        y=y,
        width=frame_width,
        height=content_height + CONTAINER_PAD * 2 + 1,
        band=band,
        lane=x + CONTAINER_PAD + 1 + lane if lane >= 0 else -1,
    )
    placement.containers[container.id] = frame
    # Every frame's title, not just the top-level ones', so what the picture
    # is organised into is visible from the placement alone. The preview
    # script reads this to say what it drew.
    placement.headings.append((x, y, container.label))

    inner_x = x + CONTAINER_PAD + 1
    inner_y = y + CONTAINER_PAD + 1
    row_y = inner_y
    for row in rows:
        column_x = inner_x
        row_height = 0
        for width, height, (kind, payload) in row:
            if kind == "boxes":
                stack_y = row_y
                for box in payload:
                    box.x = column_x
                    box.y = stack_y
                    box.band = band
                    stack_y += box.height + NODE_GAP
                    placement.boxes[box.id] = box
            else:
                child_width, _ = _measure(payload, diagram, inner_available)
                _emit(payload, diagram, placement, column_x, row_y, band,
                      inner_available)
                width = child_width
            column_x += width + GUTTER
            row_height = max(row_height, height)
        row_y += row_height + COMPONENT_GAP
    return frame


def _order(members: list[str], diagram: Diagram) -> dict[str, int]:
    """Row assignment within a container's boxes."""
    return _order_within_layers(
        members, _layer(members, diagram), diagram
    )


def _columns(
    component: list[str],
    layers: dict[str, int],
    order: dict[str, int],
    nodes: dict[str, Node],
) -> list[list[PlacedBox]]:
    """Group a component's boxes into columns, one per layer.

    *layers* decides which column a box is in — which is what makes an edge
    point rightwards — and *order* decides its place within that column.
    Keeping them separate matters: the crossing-reduction pass produces
    rows, and feeding those in as layers collapsed the whole component into
    a single column.
    """
    by_layer: dict[int, list[PlacedBox]] = {}
    for node_id in component:
        node = nodes[node_id]
        box = PlacedBox(
            id=node_id,
            node=node,
            width=max((len(line) for line in node.lines), default=0) + 2 + 2 * BOX_PAD,
            height=len(node.lines) + 2,
        )
        by_layer.setdefault(layers.get(node_id, 0), []).append(box)
    for column in by_layer.values():
        column.sort(key=lambda b: order.get(b.id, 0))
    return [by_layer[key] for key in sorted(by_layer)]


def _order_within_layers(
    component: list[str], layers: dict[str, int], diagram: Diagram
) -> dict[str, int]:
    """Assign a row to every box, ordering each layer to reduce crossings.

    Two passes of the barycentre heuristic: a box is placed at the average
    row of the boxes it links to, looking backwards at its sources and then
    forwards at its targets.

    Without it, a Service and its workload can end up on rows where the edge
    between them has to double back, and the horizontal run then sits on the
    row of some *other* box — so the arrow appears to come out of that box
    instead. `db-svc 0 endpoints ───► web` was exactly that, from a single
    correct edge.
    """
    inside = set(component)
    outgoing: dict[str, list[str]] = {node: [] for node in component}
    incoming: dict[str, list[str]] = {node: [] for node in component}
    for source, target in diagram.edges:
        if source in inside and target in inside:
            outgoing[source].append(target)
            incoming[target].append(source)

    # Rows are per layer: a box is placed at the average row of the boxes it
    # links to, and those are in the *neighbouring* layer. Comparing against
    # positions in its own layer instead — which is the obvious mistake —
    # makes the pass a no-op, because a node's neighbours are never in its
    # own layer.
    rows: dict[str, float] = {node: 0.0 for node in component}
    by_layer: dict[int, list[str]] = {}
    for node in component:
        by_layer.setdefault(layers[node], []).append(node)
    for members in by_layer.values():
        members.sort()

    for _ in range(2):
        for neighbours in (incoming, outgoing):
            for layer in sorted(by_layer):
                group = by_layer[layer]
                if not group:
                    continue
                for node in group:
                    linked = [rows[m] for m in neighbours[node]]
                    if linked:
                        rows[node] = sum(linked) / len(linked)
                # Unconnected boxes sort last within their layer. Left to
                # sort by name they would push a connected pair out of line
                # with each other: `db-svc` sorted above `web-svc` and the
                # edge between `web-svc` and `web` had to double back,
                # which made its arrow look like it came out of `db-svc`.
                group.sort(key=lambda n: (not neighbours[n], rows[n], n))
                for index, node in enumerate(group):
                    rows[node] = float(index)
    return {node: int(rows[node]) for node in component}


def _layer(component: list[str], diagram: Diagram) -> dict[str, int]:
    """Longest-path layering inside a component.

    A node's layer is one more than the deepest thing pointing at it, so an
    edge always points rightwards. Cycles are bounded by a visited set:
    Kubernetes wiring is not acyclic in practice — a Service selects a Pod
    that talks back out through a Service — and a layout that hangs on one
    is worse than one that lays a single edge sideways.
    """
    inside = set(component)
    incoming: dict[str, list[str]] = {node: [] for node in component}
    for source, target in diagram.edges:
        if source in inside and target in inside:
            incoming[target].append(source)

    depth: dict[str, int] = {}

    def resolve(node: str, seen: frozenset[str]) -> int:
        if node in depth:
            return depth[node]
        if node in seen:
            return 0
        parents = incoming[node]
        value = (
            0
            if not parents
            else 1 + max(resolve(parent, seen | {node}) for parent in parents)
        )
        depth[node] = value
        return value

    for node in component:
        resolve(node, frozenset())

    # A box with no edges at all — a workload nothing routes to and that
    # routes nowhere — has no place in the flow, and longest-path layering
    # gives it layer 0, which stacks every one of them into a single column.
    # Twenty-four rows for three boxes that fit side by side in forty
    # columns. Each gets its own column past the end of the flow instead, so
    # they pack into a row.
    #
    # "No edges" means neither end of one — an Ingress is a *source*, and
    # testing only its incoming edges classified it as loose and pushed the
    # whole chain it starts to the right of the boxes it feeds.
    linked = {end for edge in diagram.edges for end in edge}
    loose = [node for node in component if node not in linked]
    if loose:
        after = max(depth.values(), default=0) + 1
        for offset, node in enumerate(sorted(loose)):
            depth[node] = after + offset
    return depth
