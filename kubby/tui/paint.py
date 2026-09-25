"""Drawing the placed picture: frames, boxes, then edges.

The order matters, and it is the reason the arrows are consistent. Frames go
down first, then the boxes inside them, and each locks the cells it occupies.
Edges are routed last, and only along runs that have been checked clear from
end to end — so an edge can never be merged into a border, and the line
character is always the same plain ``│`` or ``─``.

A frame's border is the one cell an edge may cross, and only because it has
to: an edge between two namespaces leaves one frame and enters the other.
Crossing replaces the border with the line character, the same way every
time, rather than refusing the cell and leaving the line in two pieces.

The consequence worth stating: an arrowhead stops one cell short of the box
it points at, reading as ``─►│`` — the arrow pointing *at* the border rather
than a glyph sitting on top of it.
"""

from __future__ import annotations

from rich.text import Text

from kubby.tui.draw import Canvas, Rect
from kubby.tui.place import Placement, PlacedBox

#: Style for the connecting lines. Dim, so the boxes stay the subject.
EDGE_STYLE = "#3d444d"

#: Style for a component's heading.
HEADING_STYLE = "bold"

#: Style for a box's border, by what the box is.
ROLE_STYLE = {
    "host": "#d2a8ff",
    "node": "#8b949e",
    "infra": "#8b949e",
    "app": "",
}


def shape_for(box: PlacedBox) -> str:
    """The border shape, so shapes carry meaning.

    An ellipse only holds a short single line: the text has to fit between
    the slants, and a wide label degenerates into a rectangle with unjoined
    corners. Rather than hand-maintaining which box is an ellipse, the rule
    is a predicate on the content, so it cannot be forgotten when a node is
    added.
    """
    if box.node.role in ("infra", "node", "host"):
        return "heavy"
    longest = max((len(line) for line in box.node.lines), default=0)
    if len(box.node.lines) == 1 and longest <= 8:
        return "ellipse"
    return "round"


def render(placement: Placement) -> Text:
    """Draw *placement*."""
    return render_checked(placement)[0]


def render_checked(placement: Placement) -> tuple[Text, int]:
    """The picture, and how many cells an edge could not draw.

    Order matters and is the reason the arrows are consistent: containers go
    down first, then boxes, then edges. A box locks every cell it occupies,
    so an edge is routed into whatever space is left and can never be merged
    into a border.

    A *frame*'s border is the one exception, and it has to be: an edge
    between two namespaces has to leave one frame and enter the other, and
    refusing the border cell would cut the line in two with the arrowhead
    stranded beyond it. Crossing one replaces it with the line character,
    which is the same every time.

    The second element is the number of cells an edge asked for and was
    refused. It should be zero, and the property tests assert it: a
    non-zero count is a line with a piece missing, which reads as a
    connection that is not there.
    """
    # One column wider than the content: cross-band edges route down that
    # margin, and it is the only column guaranteed to be free of boxes.
    canvas = Canvas(placement.width + 1, placement.height)

    # Containers first, then the boxes that go inside them, then the edges.
    # A container locks only its own border, so its contents can still be
    # drawn while an edge still cannot cross into them.
    # Outermost frames first, so an inner frame is never clipped by the
    # border of the one containing it.
    for frame in sorted(
        placement.containers.values(),
        key=lambda f: (f.x + f.y),
    ):
        canvas.container(
            Rect(frame.x, frame.y, frame.width, frame.height),
            frame.label,
            "dim",
            frame.detail,
        )

    for box in placement.boxes.values():
        rect = Rect(box.x, box.y, box.width, box.height)
        style = _style(box)
        canvas.box(rect, shape_for(box), style)
        canvas.label(rect, box.node.lines, style)

    for source, target in placement.edges:
        _route(canvas, placement, source, target)

    return canvas.to_text(), canvas.skipped


def _style(box: PlacedBox) -> str | None:
    if box.node.broken_service:
        return "red"
    if not box.node.healthy:
        return "red"
    if box.node.role == "app":
        return "green"
    return ROLE_STYLE.get(box.node.role) or None


def _route(canvas: Canvas, placement: Placement, source: str, target: str) -> None:
    """Route one edge orthogonally, out into free space and back.

    Three shapes, and they cover everything because the boxes are on a grid:

    * stacked in one column — a vertical run, through the gap between them
    * side by side — one straight run, or a stepped one round a box
    * neither — a detour along a row that is clear from end to end

    The one rule every case obeys: a run is only drawn after the whole of it
    has been checked clear. Refusing a cell mid-run is what produced lines
    broken in two with an arrowhead stranded on the far side, and a picture
    like that teaches the reader a connection that is not there.
    """
    src = placement.boxes[source]
    dst = placement.boxes[target]
    canvas.begin_edge()

    if src.band != dst.band:
        _route_across_bands(canvas, placement, src, dst)
        return

    if not _shared_frame(placement, src, dst):
        # Different namespaces, same band: the short routes all work within
        # one frame and there is a whole frame in the way, so the only thing
        # that can join them is the long way round.
        _route_across_frames(canvas, placement, src, dst)
        return

    if dst.y >= src.y + src.height and _columns_overlap(src, dst):
        _route_down(canvas, placement, src, dst)
        return

    _route_sideways(canvas, src, dst)


def _shared_frame(placement: Placement, src: PlacedBox, dst: PlacedBox) -> bool:
    """Whether one frame contains both boxes.

    A frame is a namespace, so this is really "are these two in the same
    namespace", and the answer decides which routes are even worth trying:
    the ones that stay inside a frame cannot help if there is a frame in
    between.
    """
    for frame in placement.containers.values():
        if (
            frame.x <= src.x
            and src.x + src.width <= frame.x + frame.width
            and frame.y <= src.y
            and src.y + src.height <= frame.y + frame.height
            and frame.x <= dst.x
            and dst.x + dst.width <= frame.x + frame.width
            and frame.y <= dst.y
            and dst.y + dst.height <= frame.y + frame.height
        ):
            return True
    return False


def _route_sideways(canvas: Canvas, src: PlacedBox, dst: PlacedBox) -> None:
    """Two boxes beside each other, or nearly so, in the same band."""
    sy = src.y + src.height // 2
    ey = dst.y + dst.height // 2

    if dst.x > src.x:
        out_x = src.x + src.width
        if sy == ey and _clear(canvas, sy, out_x, dst.x) and _head_room(
            canvas, dst.x - 1, sy
        ):
            _run_right(canvas, dst, sy, out_x)
            return
        lane = _free_lane(canvas, out_x + 1, sy, ey)
        legs = [(out_x, sy, lane, sy), (lane, sy, lane, ey)]
        if lane < dst.x and _legs_clear(
            canvas, legs + [(lane, ey, dst.x - 1, ey)], (dst.x - 1, ey)
        ):
            _draw_legs(canvas, legs)
            _run_right(canvas, dst, ey, lane)
            return

    _route_around(canvas, src, dst)


def _run_right(canvas: Canvas, dst: PlacedBox, row: int, from_column: int) -> None:
    """The last leg of a rightward edge: up to the target, head against it.

    The head sits in the cell beside the border rather than on it, so the
    arrow reads as pointing *at* the box and the border stays a border.

    The leg stops a cell short of the head, and is skipped when there is no
    room for it. Two reasons, both learned the hard way: a leg that ran
    *through* the head's own cell would claim it, and a head on a claimed
    cell is refused — so the route drew its line and lost its arrow. And a
    leg started from a column already past the head's runs backwards,
    claiming the same cell on the way.
    """
    if from_column <= dst.x - 2:
        _draw_legs(canvas, [(from_column, row, dst.x - 2, row)])
    canvas.arrow_head(dst.x - 1, row, "right", EDGE_STYLE)


def _route_around(canvas: Canvas, src: PlacedBox, dst: PlacedBox) -> None:
    """The last resort: out, along a clear row, and back in.

    Reached when the target is to the left, or when everything between the
    two boxes is occupied. All three legs have to be clear — the row the
    horizontal leg runs along, and the columns the two verticals descend.
    Checking only the horizontal one drew a leg straight through the box in
    the next row down, and the refused cells left the edge hanging.

    The horizontal leg runs *outside* both boxes, so the row has to be clear
    of their borders as well as their interiors: a row along a box's bottom
    edge is a row that reads as its border.
    """
    sy = src.y + src.height // 2
    right = dst.x > src.x
    out_x = src.x + src.width if right else src.x - 1

    for detour in _rows_near(canvas, sy):
        # In from the side: the cell beside the target, on a row it spans.
        for column, row in _side_arrivals(dst, right):
            # The last leg stops beside the head, not on it: a leg running
            # through the head's own cell would claim it, and a head on a
            # claimed cell is refused.
            land = row - 1 if row > detour else row + 1
            legs = [
                (out_x, sy, out_x, detour),
                (out_x, detour, column, detour),
                (column, detour, column, land),
            ]
            if not _legs_clear(canvas, legs, (column, row)):
                continue
            _draw_legs(canvas, legs)
            canvas.arrow_head(column, row, "right" if right else "left", EDGE_STYLE)
            return

        # Or in through the top or the bottom, on a column of the target's
        # own border. The side is not always available: a third edge in the
        # same namespace may be using that column as its way out, and then
        # two edges in one frame have nowhere to arrive but the top or the
        # bottom.
        from_above = detour < dst.y
        land = dst.y - 1 if from_above else dst.y + dst.height
        for column in _border_interior(dst):
            legs = [
                (out_x, sy, out_x, detour),
                (out_x, detour, column, detour),
                (column, detour, column, land),
            ]
            head = (column, dst.y if from_above else dst.y + dst.height - 1)
            if not _legs_clear(canvas, legs):  # forced head; see above
                continue
            _draw_legs(canvas, legs)
            canvas.arrow_head(
                head[0], head[1], "down" if from_above else "up",
                EDGE_STYLE, force=True,
            )
            return
    # Nothing clear in either direction. Draw no edge rather than a fragment
    # that reads as one — a missing line is a gap the reader can see; a
    # broken one is a connection they will believe.


def _border_interior(dst: PlacedBox) -> list[int]:
    """The columns of *dst*'s top and bottom border, excluding its corners.

    A head on a corner replaces the corner glyph, so the middle of the
    border is the only place one belongs. Centre first, because a head in
    the middle of a box is the least surprising place for it.
    """
    if dst.width < 3:
        return []
    mid = (dst.x + dst.x + dst.width - 1) // 2
    columns = list(range(dst.x + 1, dst.x + dst.width - 1))
    return sorted(columns, key=lambda c: abs(c - mid))


def _side_arrivals(dst: PlacedBox, right: bool) -> list[tuple[int, int]]:
    """The cells beside *dst* an edge can arrive at, as ``(column, row)``.

    On a row the box actually spans. A row *outside* it is a row the arrow
    points past, which is the same mistake as putting the head a column wide
    of the target: it points at a cell belonging to nothing.

    The cell is beside the box, never its own border column — a vertical leg
    ending on the border is refused at the border, so the whole route was
    rejected every time and nothing was ever drawn to a target on its right.
    Two Services on one Deployment is the ordinary shape that exposed it: the
    second Service's edge was silently absent at every width, with nothing
    else wrong anywhere.
    """
    column = dst.x - 1 if right else dst.x + dst.width
    mid = dst.y + dst.height // 2
    rows = sorted(range(dst.y, dst.y + dst.height), key=lambda r: abs(r - mid))
    return [(column, row) for row in rows]


def _columns_near(canvas: Canvas, lo: int, hi: int) -> list[int]:
    """Every column, nearest the span ``lo..hi`` first, in both directions.

    The span itself comes first because a run straight out of a box is the
    cheapest and the least visible. The rest is there for when every column
    the box spans is blocked on the way out, which the enclosing frame's own
    title is a reliable way to arrange — it sits in exactly that band of
    columns, just above or below the box.
    """
    order = [c for c in range(lo, hi + 1) if 0 <= c < canvas.width]
    seen = set(order)
    left, right = lo - 1, hi + 1
    while left >= 0 or right < canvas.width:
        if left >= 0 and left not in seen:
            seen.add(left)
            order.append(left)
        if right < canvas.width and right not in seen:
            seen.add(right)
            order.append(right)
        left -= 1
        right += 1
    return order


def _rows_near(canvas: Canvas, near: int) -> list[int]:
    """Every row, nearest to *near* first, in both directions."""
    order: list[int] = []
    seen: set[int] = set()
    for distance in range(canvas.height + 1):
        for row in (near - distance, near + distance):
            if 0 <= row < canvas.height and row not in seen:
                seen.add(row)
                order.append(row)
        if len(order) >= canvas.height:
            break
    return order


def _columns_overlap(src: PlacedBox, dst: PlacedBox) -> bool:
    """Whether two boxes share any column, and so read as one stack."""
    return src.x < dst.x + dst.width and dst.x < src.x + src.width


def _route_across_bands(
    canvas: Canvas, placement: Placement, src: PlacedBox, dst: PlacedBox
) -> None:
    """Route an edge whose ends are in different bands.

    It runs along the clear row between the two bands, which exists
    precisely so this is possible: a straight or stepped route would have to
    cross every box in between.

    It enters the target from *above* (or below), never from the side. The
    side gutter is where the target's own outgoing edges live, and sharing
    it put two arrowheads in adjacent cells — which read as one
    bidirectional arrow between the two boxes.

    Every leg is checked before any of it is drawn. This route is the one
    that leaves its container twice, so it is the one that has to cross
    frame borders; a leg that started *on* the source's bottom border and
    was refused there left the source with no line leaving it at all, and
    the horizontal leg began in mid-air a row below.
    """
    down = dst.band > src.band
    # Two different clear rows, and which is which depends on the direction.
    #
    # `gaps[n]` is the free row between band *n* and band *n+1*. Leaving the
    # source means reaching the row on the side of the source we travel
    # towards; arriving means the row on that side of the target. Taking
    # `gaps[min(src, dst)]` for both put the *exit* row on the far side of
    # the target when travelling up, so the first leg had to climb from the
    # source through every band in between — and one box in any of them, in
    # the source's own columns, killed every candidate column and the edge
    # went undrawn.
    exit_gap = placement.gaps.get(src.band if down else src.band - 1)
    entry_gap = placement.gaps.get(dst.band - 1 if down else dst.band)
    if exit_gap is None:
        exit_gap = entry_gap if entry_gap is not None else placement.gaps.get(
            min(src.band, dst.band)
        )
    _route_the_long_way(
        canvas, placement, src, dst, exit_gap,
        entry_gap if entry_gap is not None else exit_gap, down,
    )


def _route_across_frames(
    canvas: Canvas, placement: Placement, src: PlacedBox, dst: PlacedBox
) -> None:
    """Two boxes in different frames that happen to share a band.

    An Ingress in one namespace routing to a Service in another, with both
    namespaces wide enough to sit side by side. The short routes all work
    within one frame, so they all gave up: there is no clear row between two
    boxes with a whole frame in between, and the lane that would join them
    belongs to a frame that holds neither.

    The shape is the same as a cross-band edge — out to a free row, along it
    to the margin, down the margin, back along a free row to the target — so
    it is the same code, with the two free rows searched for rather than
    looked up in a table of band gaps.
    """
    down = dst.y > src.y
    for exit_row in _rows_beside(canvas, src):
        for entry_row in _rows_beside(canvas, dst):
            if _route_the_long_way(
                canvas, placement, src, dst, exit_row, entry_row, down
            ):
                return


def _rows_beside(canvas: Canvas, box: PlacedBox) -> list[int]:
    """Rows outside *box*'s own, nearest first, so an edge can pass by it."""
    return [
        row
        for row in _rows_near(canvas, box.y + box.height // 2)
        if row < box.y or row >= box.y + box.height
    ]


def _route_the_long_way(
    canvas: Canvas,
    placement: Placement,
    src: PlacedBox,
    dst: PlacedBox,
    exit_gap: int,
    lane: int,
    down: bool,
) -> bool:
    """Out of the source, along a free row, out to the margin, along it, and
    in to the target. Whether *exit_gap* and *lane* come from the band table
    or from a search, the route is the same, and so is the reason it is only
    ever drawn whole. Returns whether it managed it."""
    # One column past the picture, which is the only column guaranteed to
    # have nothing in it. Descent in a gutter beside the source drew a rule
    # down the full height of the picture, because that gutter is beside
    # every band below it too.
    margin = canvas.width - 1
    start_y = src.y + src.height if down else src.y - 1
    approaches = _approaches(canvas, dst, down)
    for drop_x in _columns_near(canvas, src.x, src.x + src.width - 1):
        # Out of the source sideways first. Every column the source spans
        # can be blocked on the way to the gap — the frame's own title sits
        # in exactly that band of columns, and a box below the source can sit
        # in the rest — so the route has to be able to step out to the side
        # and drop from there, not only fall straight out of the source.
        out_x = src.x - 1 if drop_x < src.x else src.x + src.width
        for column, head_x, head_y, facing in approaches:
            legs = [
                (out_x, start_y, drop_x, start_y),
                (drop_x, start_y, drop_x, exit_gap),
            ]
            if lane == exit_gap:
                # The common case, and the one that used to draw over
                # itself: the two gaps are the same row, so the run is one
                # leg, straight across. Drawn as two — out to the margin and
                # back from it — the second re-walked the first's cells, and
                # lines now claim the cells they cover, so the tail of every
                # long edge came out as a row of refusals.
                legs.append((drop_x, exit_gap, column, exit_gap))
            else:
                # The vertical starts one row *past* the corner the
                # horizontal already covered, for the same reason.
                step = 1 if lane > exit_gap else -1
                legs.append((drop_x, exit_gap, margin, exit_gap))
                legs.append((margin, exit_gap + step, margin, lane))
                legs.append((margin, lane, column, lane))
            # And in, stopping at the last cell *before* the target's
            # border; the head goes on the border itself, deliberately.
            legs.append((column, lane, head_x, head_y))
            # No head argument: this head is forced onto the target's own
            # border, which is the cell it is supposed to occupy.
            if not _legs_clear(canvas, legs):
                continue
            _draw_legs(canvas, legs)
            canvas.arrow_head(head_x, head_y, facing, EDGE_STYLE, force=True)
            return True
    # No column out of the source and in to the target is clear, which takes
    # a picture far denser than any real cluster. Draw nothing: a missing
    # line is a gap the reader can see, and a broken one is a connection
    # they will believe.
    return False


def _approaches(
    canvas: Canvas, dst: PlacedBox, down: bool
) -> list[tuple[int, int, int, str]]:
    """Ways to come into *dst*: ``(column, head_x, head_y, facing)``.

    Two families, most direct first. Straight in through the top or the
    bottom, over every column the box spans; then in from either side, over
    every row of it. The caller tries them in order and keeps the first whose
    legs are all clear.

    A single cell per family is not enough. Two edges can want the same
    target — a cross-namespace Ingress and the Service beside it both point
    at the same box — and the first one's arrowhead claims the cell. Locking
    it is the point (see `Canvas.arrow_head`); the answer is for the second
    edge to arrive a row or a column further along, not to give up.
    """
    out: list[tuple[int, int, int, str]] = []
    mid_x = (dst.x + dst.x + dst.width - 1) // 2
    for column in sorted(range(dst.x, dst.x + dst.width), key=lambda c: abs(c - mid_x)):
        # A column another edge has already put a head in is not available.
        # This is what lets two namespaces route to one Service: the second
        # takes the next column along instead of landing on the first's
        # head and erasing it.
        # The cell the head actually lands in, which is the row *beyond*
        # the border rather than the border itself — the run stops a cell
        # short, so `─▼` reads as the arrow entering the box.
        landing = dst.y - 1 if down else dst.y + dst.height
        if not canvas.free_for_forced_head(column, landing):
            continue
        out.append((column, column, landing, "down" if down else "up"))
    mid_y = dst.y + dst.height // 2
    for row in sorted(range(dst.y, dst.y + dst.height), key=lambda r: abs(r - mid_y)):
        if dst.x - 1 >= 0 and canvas.headroom(dst.x - 1, row):
            out.append((dst.x - 1, dst.x - 1, row, "right"))
        if canvas.headroom(dst.x + dst.width, row):
            out.append((dst.x + dst.width, dst.x + dst.width, row, "left"))
    return out


def _route_down(
    canvas: Canvas, placement: Placement, src: PlacedBox, dst: PlacedBox
) -> None:
    """Join two boxes stacked in one column.

    Straight down through the gap when there is one, which is the common case
    and needs nothing reserved. Otherwise out to the frame's own lane, down
    it, and back in — the lane exists because the frame that holds both boxes
    is the innermost one with a spare column, and it is inside the frame, so
    no run has to cross a border to reach it.
    """
    top = src.y + src.height
    # The column has to be one the *target* spans as well as the source. A
    # wide Ingress above a narrow Service offered columns past the Service's
    # right edge, and the head landed there pointing down at blank space —
    # an arrow with nothing under it, which reads as a connection to nowhere.
    #
    # And it has to be one of the target's *border* columns, not its corner
    # columns: the head is written on the border it enters, and a head on a
    # corner replaces the corner glyph. `dst.x + 1 .. dst.x + width - 2` is
    # the middle of that border, which is where "it comes in here" belongs.
    lo = max(src.x + 1, dst.x + 1)
    hi = min(src.x + src.width - 1, dst.x + dst.width - 2)
    for column in range(lo, hi + 1):
        if _clear(canvas, column, top, dst.y, vertical=True):
            _draw_legs(canvas, [(column, top, column, dst.y - 1)])
            canvas.arrow_head(column, dst.y, "down", EDGE_STYLE, force=True)
            return

    channel = _lane_around(placement, src, dst)
    if channel is not None:
        out_y = src.y + src.height // 2
        in_y = dst.y + dst.height // 2
        out_x = src.x + src.width
        in_x = dst.x + dst.width
        legs = [
            (out_x, out_y, channel, out_y),
            (channel, out_y, channel, in_y),
            (channel, in_y, in_x, in_y),
        ]
        if _legs_clear(canvas, legs):
            _draw_legs(canvas, legs)
            canvas.arrow_head(in_x, in_y, "left", EDGE_STYLE, force=True)
            return

    # The lane is one way round a stacked pair and the detour is another.
    # Falling back to it is what stops a narrow panel losing an edge it
    # could have drawn: the last leg of the lane route runs at the target's
    # middle row, and when a third box shares that row and column there is
    # no lane but there is a clear row somewhere else.
    _route_around(canvas, src, dst)


def _lane_around(placement: Placement, src: PlacedBox, dst: PlacedBox) -> int | None:
    """The innermost reserved column inside a frame that holds both boxes.

    Innermost, because the outermost frame that holds both is the widest and
    a lane out there would run the length of the picture to join two boxes in
    one namespace. *Holds* has to test the vertical extent as well as the
    horizontal one: a frame that merely starts above both boxes is in a
    different band, and routing down its lane drew the edge out of its own
    frame and across a border to get there.
    """

    def holds(frame) -> bool:
        return (
            src.x >= frame.x
            and src.y >= frame.y
            and src.x + src.width <= frame.x + frame.width
            and src.y + src.height <= frame.y + frame.height
            and dst.x >= frame.x
            and dst.y >= frame.y
            and dst.x + dst.width <= frame.x + frame.width
            and dst.y + dst.height <= frame.y + frame.height
        )

    candidates = [
        frame
        for frame in placement.containers.values()
        if frame.lane >= 0 and holds(frame)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: f.width * f.height).lane


def _clear(
    canvas: Canvas, fixed: int, start: int, stop: int, *, vertical: bool = False
) -> bool:
    """Whether every cell of a run is free to draw on.

    *fixed* is the row for a horizontal run and the column for a vertical
    one. Frame borders count as free — see ``Canvas.solid`` — and so do the
    cells an earlier edge has already claimed, which is what stops two
    edges from sharing a run and one of them erasing the other's arrowhead.

    One flag, not two. An earlier version took ``horizontal`` *and*
    ``vertical`` and branched on the first, so every caller asking for a
    vertical check got a horizontal one: it tested the right cells for the
    wrong axis, found them clear, and the run was then drawn straight
    through a box.

    A span that leaves the canvas is not clear. Clamping it said yes to a
    run that would walk off the edge, and the cells it lost on the way out
    were the difference between a line that stops and a line that stops in
    the wrong place.
    """
    lo, hi = sorted((start, stop))
    if vertical:
        if lo < 0 or hi > canvas.height:
            return False
        return not any(canvas.solid(fixed, y) for y in range(lo, hi))
    if lo < 0 or hi > canvas.width:
        return False
    return not any(canvas.solid(x, fixed) for x in range(lo, hi))


def _head_room(canvas: Canvas, x: int, y: int) -> bool:
    """Whether an arrowhead can be placed at ``(x, y)``."""
    return canvas.headroom(x, y)


def _leg_span(
    x1: int, y1: int, x2: int, y2: int
) -> tuple[int, int, int, bool] | None:
    """The cells one leg of a route covers, as ``(fixed, start, stop, vertical)``.

    One function for both checking and drawing a leg, because computing the
    cells twice is how the two come to disagree. A check over
    ``[out_x, channel)`` and a draw over ``[channel - 1, channel]`` differ by
    one cell; the check passed, and the draw then lost that cell to the
    frame's border. ``None`` means the leg is a single point and there is
    nothing to draw.
    """
    if x1 == x2:
        if y1 == y2:
            return None
        return (x1, min(y1, y2), max(y1, y2) + 1, True)
    if y1 != y2:
        # `Canvas.line` would draw this as a horizontal run at `y1` and
        # silently drop the vertical half, so the check has to say no rather
        # than agree with a draw that is not the one being asked about.
        raise ValueError(f"leg ({x1},{y1})->({x2},{y2}) is not axis-aligned")
    return (y1, min(x1, x2), max(x1, x2) + 1, False)


def _legs_clear(
    canvas: Canvas,
    legs: list[tuple[int, int, int, int]],
    head: tuple[int, int] | None = None,
) -> bool:
    """Whether every leg, and the head's own cell, are free to draw on.

    The head has to be in here. It is not part of any leg — the leg stops
    beside it, so the line does not run through it — which meant a route
    could check clear, draw its whole path, and then find the arrowhead
    already claimed by an earlier edge. That is the worst of both: a line
    with nothing at the end of it, and no fallback tried.

    Only for a head that is placed *beside* the box. A forced head goes
    *on* the target's border on purpose, so testing it for room would fail
    on the very cell it is meant to occupy.
    """
    if head is not None and not canvas.headroom(*head):
        return False
    return all(
        span is None or _clear(canvas, span[0], span[1], span[2],
                               vertical=span[3])
        for span in (_leg_span(*leg) for leg in legs)
    )


def _draw_legs(canvas: Canvas, legs: list[tuple[int, int, int, int]]) -> None:
    for leg in legs:
        if _leg_span(*leg) is not None:
            canvas.line([(leg[0], leg[1]), (leg[2], leg[3])], EDGE_STYLE)


def _free_lane(canvas: Canvas, preferred: int, y1: int, y2: int) -> int:
    """The first column at or after *preferred* that is clear for the hop."""
    x = max(0, preferred)
    while x < canvas.width and not _clear(
        canvas, x, min(y1, y2), max(y1, y2) + 1, vertical=True
    ):
        x += 1
    return min(x, canvas.width - 1)


def centre(picture: Text, width: int) -> Text:
    """Centre a picture narrower than the space it has.

    Only when it fits: centring an over-wide picture would push its left edge
    off the scroll origin, where it cannot be scrolled back to.
    """
    widest = max((len(line) for line in picture.plain.splitlines()), default=0)
    if widest >= width:
        return picture
    pad = " " * ((width - widest) // 2)
    out = Text()
    for index, line in enumerate(picture.split(allow_blank=True)):
        if index:
            out.append("\n")
        out.append(pad)
        out.append_text(line)
    return out
