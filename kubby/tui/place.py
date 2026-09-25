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
    x: int = 0
    y: int = 0
    width: int = 0
    height: int = 0
    band: int = 0


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
    """Lay *diagram* out to fit *width* columns, wrapping where it must."""
    placement = Placement()
    if not diagram.nodes:
        return placement

    # Containers are the unit of placement, not loose boxes. A namespace is
    # placed whole — its objects inside a frame around them — so wrapping
    # can never split one across two bands, and so the frame is always
    # exactly as big as its contents.
    band_top = 0
    band_bottom = 0
    band = 0
    x = 0

    for container in diagram.ordered_containers():
        members = [m for m in container.members if m in diagram.nodes]
        if not members:
            continue
        columns = _columns(
            members, _layer(members, diagram), _order(members, diagram), diagram.nodes
        )
        widest = max((b.width for column in columns for b in column), default=0)
        # A container wider than the panel cannot be wrapped — it is the
        # smallest unit placed — so it overflows and the panel scrolls.
        outer_width = _span(columns, widest)
        inner = CONTAINER_PAD * 2 + 1  # left, right, and the title row
        total_width = outer_width + inner

        needed = x + (GUTTER if x else 0) + total_width
        if x and needed > width:
            placement.gaps[band] = band_bottom + 1
            band += 1
            band_top = band_bottom + COMPONENT_GAP
            x = 0

        offset = x + (GUTTER if x else 0)
        top = band_top
        content_bottom = top
        for index, column in enumerate(columns):
            column_x = offset + CONTAINER_PAD + index * (widest + GUTTER)
            y = top + CONTAINER_PAD + 1
            for box in column:
                box.x = column_x
                box.y = y
                box.band = band
                y += box.height + NODE_GAP
                content_bottom = max(content_bottom, y - NODE_GAP)
                placement.boxes[box.id] = box

        frame = PlacedContainer(
            id=container.id,
            label=container.label,
            x=offset,
            y=top,
            width=total_width,
            # +1 for the bottom border below the last box. Sized to *its own*
            # contents: sizing to the band left every container padded out to
            # the height of the tallest one beside it.
            height=(content_bottom - top) + CONTAINER_PAD + 1,
            band=band,
        )
        placement.containers[container.id] = frame
        placement.headings.append((offset, top, container.label))
        placement.heading_right[band] = max(
            placement.heading_right.get(band, 0), offset + len(container.label)
        )
        band_bottom = max(band_bottom, frame.y + frame.height - 1)
        x = offset + total_width

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


def _order(members: list[str], diagram: Diagram) -> dict[str, int]:
    """Row assignment within a container's boxes."""
    return _order_within_layers(
        members, _layer(members, diagram), diagram
    )


def _span(columns: list[list[PlacedBox]], widest: int) -> int:
    """How wide a component is: every column but the last is `widest` wide."""
    if not columns:
        return 0
    return len(columns) * widest + (len(columns) - 1) * GUTTER


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
    return depth
