"""Placing the picture: our own layout, in columns that fit the panel.

The problem this solves, measured rather than guessed. The cluster graph is
mostly *disconnected* — the control plane's components do not link to each
other, and the node's edges to individual workloads are gone on purpose,
because with a single node they carried no information. A conventional
layered layout therefore puts each of the ~15 components in its own column,
and twenty boxes came out 301 columns wide.

So the layout is built around components instead:

1. weakly-connected boxes are grouped into components
2. components are ordered by role — the machine, then what runs on it, then
   your own code — and stacked down the panel
3. inside a component, boxes are layered by longest path from its root, so
   each layer is a column and an edge always points rightwards
4. a component that will not fit the remaining width wraps to a new band
   underneath

Step 4 is what makes the picture scale with the terminal instead of
insisting on a certain size.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from kubby.tui.diagram import ROLES, Diagram, Node

#: Blank columns between two layers. An edge is routed in here, so it has to
#: be wide enough for a lane *and* an arrowhead: four, because with three a
#: stepped edge arriving from above had nowhere to put its head and landed on
#: the border it was aiming at.
GUTTER = 4

#: Blank rows between two boxes stacked in the same column.
NODE_GAP = 1

#: Blank rows between two components.
COMPONENT_GAP = 2

#: How far a box's text sits from its border. Symmetric — the previous
#: renderer could not manage that, because lowering its padding moved the gap
#: to one side rather than closing it.
BOX_PAD = 1

#: Rows a component's heading occupies above its boxes.
HEADING_ROWS = 1


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
class Placement:
    """A laid-out picture, in cells, ready to draw."""

    boxes: dict[str, PlacedBox] = field(default_factory=dict)
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

    components = _ordered_components(diagram)
    band_top = 0
    band_bottom = 0
    band = 0
    x = 0

    for heading, component in components:
        layers = _layer(component, diagram)
        order = _order_within_layers(component, layers, diagram)
        columns = _columns(component, layers, order, diagram.nodes)
        widest = max((b.width for column in columns for b in column), default=0)
        component_width = _span(columns, widest)

        needed = x + (GUTTER if x else 0) + component_width
        if x and needed > width:
            # This component cannot start on the current band. Wrap, leaving
            # a clear row between the two so a cross-band edge has somewhere
            # to run.
            placement.gaps[band] = band_bottom + 1
            band += 1
            band_top = band_bottom + COMPONENT_GAP
            x = 0

        top = band_top + HEADING_ROWS
        offset = x + (GUTTER if x else 0)
        for index, column in enumerate(columns):
            column_x = offset + index * (widest + GUTTER)
            y = top
            for box in column:
                box.x = column_x
                box.y = y
                box.band = band
                y += box.height + NODE_GAP
                placement.boxes[box.id] = box
            band_bottom = max(band_bottom, y - NODE_GAP)
        placement.headings.append((offset, band_top, heading))
        placement.heading_right[band] = max(
            placement.heading_right.get(band, 0), offset + len(heading)
        )
        x = offset + component_width

    placement.width = max(
        (b.x + b.width for b in placement.boxes.values()), default=1
    )
    placement.height = max(
        (b.y + b.height for b in placement.boxes.values()),
        default=band_bottom + 1,
    )
    for source, target in diagram.edges:
        if source in placement.boxes and target in placement.boxes:
            placement.edges.append((source, target))
    return placement


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
            width=max(len(line) for line in node.lines) + 2 + 2 * BOX_PAD,
            height=len(node.lines) + 2,
        )
        by_layer.setdefault(layers.get(node_id, 0), []).append(box)
    for column in by_layer.values():
        column.sort(key=lambda b: order.get(b.id, 0))
    return [by_layer[key] for key in sorted(by_layer)]


def _ordered_components(diagram: Diagram) -> list[tuple[str, list[str]]]:
    """Groups in reading order: the machine first, your own code last.

    Ranked by the most senior role any member has, so `your computer` and
    the node come before the control plane, and the control plane before
    anything the reader wrote. Ties break on the group name, which keeps the
    order stable across refreshes — a picture that reshuffles every `R` is
    impossible to build a mental model of.
    """
    scored = []
    for heading, members in diagram.groups():
        roles = {diagram.role_of(node) for node in members}
        rank = min((ROLES.index(r) for r in roles if r in ROLES), default=len(ROLES))
        scored.append((rank, heading, members))
    scored.sort(key=lambda item: (item[0], item[1]))
    return [(heading, members) for _rank, heading, members in scored]


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
