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

    if src.band != dst.band:
        _route_across_bands(canvas, placement, src, dst)
        return

    if dst.y >= src.y + src.height and _columns_overlap(src, dst):
        _route_down(canvas, placement, src, dst)
        return

    _route_sideways(canvas, src, dst)


def _route_sideways(canvas: Canvas, src: PlacedBox, dst: PlacedBox) -> None:
    """Two boxes beside each other, or nearly so, in the same band."""
    sy = src.y + src.height // 2
    ey = dst.y + dst.height // 2

    if dst.x > src.x:
        out_x = src.x + src.width
        if sy == ey and _clear(canvas, sy, out_x, dst.x):
            _run_right(canvas, dst, sy, out_x)
            return
        lane = _free_lane(canvas, out_x + 1, sy, ey)
        legs = [(out_x, sy, lane, sy), (lane, sy, lane, ey)]
        if lane < dst.x and _legs_clear(
            canvas, legs + [(lane, ey, dst.x - 1, ey)]
        ):
            _draw_legs(canvas, legs)
            _run_right(canvas, dst, ey, lane)
            return

    _route_around(canvas, src, dst)


def _run_right(canvas: Canvas, dst: PlacedBox, row: int, from_column: int) -> None:
    """The last leg of a rightward edge: up to the target, head against it.

    The head sits in the cell beside the border rather than on it, so the
    arrow reads as pointing *at* the box and the border stays a border.
    """
    _draw_legs(canvas, [(from_column, row, dst.x - 1, row)])
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
    ey = dst.y + dst.height // 2
    right = dst.x > src.x
    out_x = src.x + src.width if right else src.x - 1
    in_x = dst.x if right else dst.x + dst.width

    for detour in _rows_near(canvas, sy):
        legs = [
            (out_x, sy, out_x, detour),
            (out_x, detour, in_x, detour),
            (in_x, detour, in_x, ey),
        ]
        if not _legs_clear(canvas, legs):
            continue
        _draw_legs(canvas, legs)
        canvas.arrow_head(in_x, ey, "right" if right else "left", EDGE_STYLE)
        return
    # Nothing clear in either direction. Draw no edge rather than a fragment
    # that reads as one — a missing line is a gap the reader can see; a
    # broken one is a connection they will believe.


def _rows_near(canvas: Canvas, near: int) -> list[int]:
    """Every row, nearest to *near* first, in both directions."""
    rows: list[int] = []
    for distance in range(canvas.height + 1):
        for row in (near - distance, near + distance):
            if 0 <= row < canvas.height and row not in rows:
                rows.append(row)
    return rows


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
    lower = min(src.band, dst.band)
    gap = placement.gaps.get(lower)
    if gap is None:
        return  # adjacent bands with no reserved row; nothing sensible to do

    down = dst.band > src.band
    target_gap = placement.gaps.get(dst.band - 1 if down else dst.band)
    lane = target_gap if target_gap is not None else gap
    # One column past the picture, which is the only column guaranteed to
    # have nothing in it. Descent in a gutter beside the source drew a rule
    # down the full height of the picture, because that gutter is beside
    # every band below it too.
    margin = canvas.width - 1

    start_y = src.y + src.height if down else src.y - 1
    approaches = _approaches(placement, dst, down)
    for drop_x in range(src.x, src.x + src.width):
        if not _clear(canvas, drop_x, start_y, gap, vertical=True):
            continue
        for column, head_x, head_y, facing in approaches:
            legs = [
                # Out of the source, starting at the first cell *past* its
                # border. Starting on the border itself was refused, which
                # left the source with no line leaving it and the next leg
                # starting in mid-air.
                (drop_x, start_y, drop_x, gap),
                (drop_x, gap, margin, gap),
                (margin, gap, margin, lane),
                (margin, lane, column, lane),
                # And in, stopping at the last cell *before* the target's
                # border; the head goes on the border itself, deliberately.
                (column, lane, head_x, head_y),
            ]
            if not _legs_clear(canvas, legs):
                continue
            _draw_legs(canvas, legs)
            canvas.arrow_head(head_x, head_y, facing, EDGE_STYLE, force=True)
            return
    # No column out of the source and in to the target is clear, which takes
    # a picture far denser than any real cluster. Draw nothing: a missing
    # line is a gap the reader can see, and a broken one is a connection
    # they will believe.


def _approaches(
    placement: Placement, dst: PlacedBox, down: bool
) -> list[tuple[int, int, int, str]]:
    """Ways to come into *dst*: ``(column, head_x, head_y, facing)``.

    From above when descending and below when ascending, stepped to the
    right of the frame's own title so the run does not cut through the name.
    Then the same from the side, for a box too narrow to be entered clear of
    that — which points the other way, and so comes last.
    """
    clear_of = placement.heading_right.get(dst.band, 0) + 2
    out: list[tuple[int, int, int, str]] = []
    if dst.x <= clear_of <= dst.x + dst.width - 1:
        if down:
            out.append((clear_of, clear_of, dst.y - 1, "down"))
        else:
            out.append((clear_of, clear_of, dst.y + dst.height, "up"))
    row = dst.y + dst.height // 2
    if dst.x - 1 >= 0:
        out.append((dst.x - 1, dst.x - 1, row, "right"))
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
    lo = max(src.x + 1, dst.x)
    hi = min(src.x + src.width - 1, dst.x + dst.width - 1)
    for column in range(lo, hi + 1):
        if _clear(canvas, column, top, dst.y, vertical=True):
            _draw_legs(canvas, [(column, top, column, dst.y - 1)])
            canvas.arrow_head(column, dst.y, "down", EDGE_STYLE, force=True)
            return

    channel = _lane_around(placement, src, dst)
    if channel is None:
        return
    out_y = src.y + src.height // 2
    in_y = dst.y + dst.height // 2
    out_x = src.x + src.width
    in_x = dst.x + dst.width
    legs = [
        (out_x, out_y, channel, out_y),
        (channel, out_y, channel, in_y),
        (channel, in_y, in_x, in_y),
    ]
    if not _legs_clear(canvas, legs):
        return
    _draw_legs(canvas, legs)
    canvas.arrow_head(in_x, in_y, "left", EDGE_STYLE, force=True)


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
    return (y1, min(x1, x2), max(x1, x2) + 1, False)


def _legs_clear(canvas: Canvas, legs: list[tuple[int, int, int, int]]) -> bool:
    return all(
        span is None or _clear(canvas, span[0], span[1], span[2],
                               vertical=span[3])
        for span in (_leg_span(*leg) for leg in legs)
    )


def _draw_legs(canvas: Canvas, legs: list[tuple[int, int, int, int]]) -> None:
    for leg in legs:
        if _leg_span(*leg) is not None:
            canvas.line([(leg[0], leg[1]), (leg[2], leg[3])], EDGE_STYLE)


def _clear_row(canvas: Canvas, near: int, after: int, before: int) -> int:
    """The nearest row to *near* with nothing solid between two columns."""
    after, before = sorted((after, before))
    for distance in range(0, canvas.height):
        for row in (near + distance, near - distance):
            if 0 <= row < canvas.height and _clear(canvas, row, after, before):
                return row
    return min(near + 1, canvas.height - 1)


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
