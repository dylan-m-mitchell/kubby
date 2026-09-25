"""Drawing the placed picture: headings, boxes, then edges.

The order matters, and it is the reason the arrows are consistent. Boxes go
down first and each locks the cells it occupies; edges are routed last into
whatever space is left, which is to say into the gutters between columns. An
edge can therefore never cross a border, so it can never be merged into one,
so the line character is always the same plain ``│`` or ``─``.

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
    """Draw *placement*.

    Order matters and is the reason the arrows are consistent: containers go
    down first, then boxes, then edges. Each locks the cells it must not be
    crossed by, so an edge is routed into whatever space is left — which is
    to say the gutters between columns — and can never be merged into a
    border.
    """
    # One column wider than the content: cross-band edges route down that
    # margin, and it is the only column guaranteed to be free of boxes.
    canvas = Canvas(placement.width + 1, placement.height)

    # Containers first, then the boxes that go inside them, then the edges.
    # A container locks only its own border, so its contents can still be
    # drawn while an edge still cannot cross the frame.
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

    return canvas.to_text()


def _style(box: PlacedBox) -> str | None:
    if box.node.broken_service:
        return "red"
    if not box.node.healthy:
        return "red"
    if box.node.role == "app":
        return "green"
    return ROLE_STYLE.get(box.node.role) or None


def _route(canvas: Canvas, placement: Placement, source: str, target: str) -> None:
    """Route one edge orthogonally, out into a gutter and back.

    Three cases, and they cover everything because the boxes are on a grid:

    * same row, target to the right — one straight run
    * stepped — a lane beside the source, down or up it, then in
    * target to the left, which is what a wrapped band produces — mirrored

    In every case the run stops one cell short of the target. Its border is
    locked, so an edge that overlapped it would be clipped mid-character,
    and one that butts against it reads as pointing *at* the box.
    """
    src = placement.boxes[source]
    dst = placement.boxes[target]

    sx, sy = src.x + src.width, src.y + src.height // 2
    ex, ey = dst.x, dst.y + dst.height // 2

    if src.band != dst.band:
        _route_across_bands(canvas, placement, src, dst)
        return

    if dst.y >= src.y + src.height and _columns_overlap(src, dst):
        # Stacked in one column, which is what a narrow panel forces. A
        # vertical run, down the gap between the two and in through the
        # target's top edge — not sideways, because sideways is where the
        # frame's border is, and an edge that crosses a border is the one
        # thing this whole file exists to avoid.
        _route_down(canvas, placement, src, dst)
        return

    if ex > sx + 2:
        if sy == ey:
            canvas.line([(sx, sy), (ex - 1, sy)], EDGE_STYLE)
            canvas.arrow_head(ex - 1, sy, "right", EDGE_STYLE)
            return
        lane = _free_lane(canvas, sx + 1, sy, ey)
        canvas.line([(sx, sy), (lane, sy)], EDGE_STYLE)
        canvas.line([(lane, sy), (lane, ey)], EDGE_STYLE)
        canvas.line([(lane, ey), (ex - 1, ey)], EDGE_STYLE)
        canvas.arrow_head(ex - 1, ey, "right", EDGE_STYLE)
        return

    if ex >= sx:
        # Stacked in one column: drop into the gutter below the source, come
        # back up, and enter from the left.
        lane = _free_lane(canvas, sx + 1, sy, ey)
        canvas.line([(sx, sy), (lane, sy)], EDGE_STYLE)
        canvas.line([(lane, sy), (lane, ey)], EDGE_STYLE)
        canvas.line([(lane, ey), (ex - 1, ey)], EDGE_STYLE)
        canvas.arrow_head(ex - 1, ey, "right", EDGE_STYLE)
        return

    # Target is to the left in the same band. A horizontal run would have to
    # pass through whatever sits between, so go around: out to the right of
    # the source, vertically to a clear row, back left past the target, and
    # in from its right-hand side.
    lane = _free_lane(canvas, sx + 1, sy, sy)
    detour = _clear_row(canvas, sy, sx, dst.x + dst.width)
    canvas.line([(sx, sy), (lane, sy)], EDGE_STYLE)
    canvas.line([(lane, sy), (lane, detour)], EDGE_STYLE)
    canvas.line([(lane, detour), (dst.x + dst.width + 1, detour)], EDGE_STYLE)
    canvas.line(
        [(dst.x + dst.width + 1, detour), (dst.x + dst.width + 1, ey)], EDGE_STYLE
    )
    canvas.arrow_head(dst.x + dst.width + 1, ey, "left", EDGE_STYLE)


def _route_across_bands(
    canvas: Canvas, placement: Placement, src: PlacedBox, dst: PlacedBox
) -> None:
    """Route an edge whose ends are in different bands.

    It runs along the clear row between the two bands, which exists
    precisely so this is possible: a straight or stepped route would have to
    cross every box in between, and the lock would clip it into a stub with
    an arrowhead floating free of any line.

    It enters the target from *above* (or below), never from the side. The
    side gutter is where the target's own outgoing edges live, and sharing
    it put two arrowheads in adjacent cells — which read as one
    bidirectional arrow between the two boxes.
    """
    lower = min(src.band, dst.band)
    gap = placement.gaps.get(lower)
    if gap is None:
        return  # adjacent bands with no reserved row; nothing sensible to do

    down = dst.band > src.band
    # Drop out of the source into the clear row below its band, run out to a
    # margin column that nothing else is drawn in, go down (or up) that
    # margin to the band the target is in, and come in along the clear row
    # above it.
    #
    # The margin is what makes a long edge possible at all. Descent in a
    # gutter beside the source drew a rule down the full height of the
    # picture, because that gutter is beside every band below it too; and
    # using the gap above the *source* rather than above the *target* sent a
    # short edge on a journey the length of the whole picture.
    drop_x = src.x + src.width // 2
    margin = placement.width
    if down:
        canvas.line([(drop_x, src.y + src.height), (drop_x, gap)], EDGE_STYLE)
    else:
        canvas.line([(drop_x, src.y), (drop_x, gap)], EDGE_STYLE)
    canvas.line([(drop_x, gap), (margin, gap)], EDGE_STYLE)

    target_gap = placement.gaps.get(dst.band - 1 if down else dst.band)
    lane = target_gap if target_gap is not None else gap
    canvas.line([(margin, gap), (margin, lane)], EDGE_STYLE)
    # Step right of the band's heading before coming down, so the descent
    # does not cut through the heading text. When the box is too narrow to be
    # entered clear of its own heading — `kubby-demo` is ten characters and
    # the first box in it starts at column zero — come in from the side
    # instead, which clears the heading entirely.
    clear_of = placement.heading_right.get(dst.band, 0) + 2
    if clear_of <= dst.x + dst.width - 1:
        entry = clear_of
        canvas.line([(margin, lane), (entry, lane)], EDGE_STYLE)
        canvas.line([(entry, lane), (entry, dst.y)], EDGE_STYLE)
        canvas.arrow_head(entry, dst.y, "down", EDGE_STYLE, force=True)
        return

    side = dst.x - 1
    if side < 0:
        side = dst.x + dst.width
        canvas.line([(margin, lane), (side, lane)], EDGE_STYLE)
        canvas.line([(side, lane), (side, dst.y + dst.height // 2)], EDGE_STYLE)
        canvas.arrow_head(side, dst.y + dst.height // 2, "left", EDGE_STYLE)
        return
    row = dst.y + dst.height // 2
    canvas.line([(margin, lane), (side, lane)], EDGE_STYLE)
    canvas.line([(side, lane), (side, row)], EDGE_STYLE)
    canvas.line([(side, row), (dst.x, row)], EDGE_STYLE)
    canvas.arrow_head(dst.x, row, "right", EDGE_STYLE, force=True)


def _columns_overlap(src: PlacedBox, dst: PlacedBox) -> bool:
    """Whether two boxes share any column, and so read as one stack."""
    return src.x < dst.x + dst.width and dst.x < src.x + src.width


def _route_down(
    canvas: Canvas, placement: Placement, src: PlacedBox, dst: PlacedBox
) -> None:
    """Join two boxes stacked in one column.

    Straight down through the gap when there is one, which is the common case
    and needs nothing reserved. Otherwise out to the frame's own lane, down
    it, and back in — the lane exists because the frame that holds both boxes
    is the innermost one with a spare column, and it is inside the frame, so
    no run crosses a border.
    """
    top = src.y + src.height
    for column in range(src.x + 1, src.x + src.width - 1):
        if not any(canvas.locked(column, row) for row in range(top, dst.y)):
            canvas.line([(column, top - 1), (column, dst.y)], EDGE_STYLE)
            canvas.arrow_head(column, dst.y, "down", EDGE_STYLE, force=True)
            return

    channel = _lane_around(placement, src, dst)
    if channel is None:
        return
    out_y = src.y + src.height // 2
    in_y = dst.y + dst.height // 2
    canvas.line([(src.x + src.width, out_y), (channel, out_y)], EDGE_STYLE)
    canvas.line([(channel, out_y), (channel, in_y)], EDGE_STYLE)
    canvas.line([(channel, in_y), (dst.x + dst.width, in_y)], EDGE_STYLE)
    canvas.arrow_head(dst.x + dst.width, in_y, "left", EDGE_STYLE, force=True)


def _lane_around(
    placement: Placement, src: PlacedBox, dst: PlacedBox
) -> int | None:
    """The innermost reserved column inside a frame that holds both boxes.

    Innermost, because the outermost frame that holds both is usually
    the widest one and a lane out there would run the length of the
    picture to join two boxes in one namespace.
    """

    def holds(frame) -> bool:
        return (
            src.x >= frame.x
            and src.y >= frame.y
            and src.x + src.width <= frame.x + frame.width
            and dst.x >= frame.x
            and dst.y >= frame.y
            and dst.x + dst.width <= frame.x + frame.width
        )

    candidates = [
        frame
        for frame in placement.containers.values()
        if frame.lane >= 0 and holds(frame)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda f: f.width * f.height).lane


def _clear_row(canvas: Canvas, near: int, after: int, before: int) -> int:
    """The nearest row to *near* with nothing drawn between two columns."""
    for distance in range(1, canvas.height):
        for row in (near + distance, near - distance):
            if 0 <= row < canvas.height and not any(
                canvas.locked(x, row) for x in range(after, min(before, canvas.width))
            ):
                return row
    return min(near + 1, canvas.height - 1)


def _free_lane(canvas: Canvas, preferred: int, y1: int, y2: int) -> int:
    """The first column at or after *preferred* that is clear for the hop."""
    x = preferred
    while x < canvas.width and any(
        canvas.locked(x, y) for y in range(min(y1, y2), max(y1, y2) + 1)
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
