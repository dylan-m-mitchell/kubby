"""A character canvas we own, so the picture can be drawn the way we want.

This replaces a third-party renderer that drew well enough to be nearly
right and not quite right in ways that made the picture illegible:

- its box padding was asymmetric under compaction, so boxes picked up a gap
  down one side only, and lowering the padding moved the gap rather than
  closing it
- edges were routed after the boxes with no reserved gutter, so arrows
  crossed borders and the junction character varied every crossing
  (``minikube, auto├┼─►│``)
- its subgraph padding was a module constant, so the spacing could not be
  tuned at all

Two rules make the output consistent here, and both are enforced by the
canvas rather than left to convention:

**A box is never overwritten.** A box locks every cell it occupies once
drawn, so an edge routed later cannot scratch a character through its border
or through the text inside it.

**An edge stops one cell short of a box.** Arrows therefore sit *beside* a
border, pointing at it (``─►│``), never merged into it. That is the whole of
the arrow-consistency guarantee: an arrow cannot end up fused with a border,
because it is never allowed to write there.

**A *frame* is the one exception, and deliberately.** An edge between two
namespaces has to leave one frame and enter the other, and there is no way to
do that without crossing a border. So a frame's border may be written, and
what replaces it is the line character — the same ``─`` or ``│`` on every
crossing. Refusing it instead left the line in two pieces with the arrowhead
stranded beyond. `Canvas.solid` is the predicate that draws the line, and
`_frame` is the set of cells it excuses.
"""

from __future__ import annotations

from dataclasses import dataclass

from rich.text import Text

#: Border characters per shape. A shape is (top-left, top-right,
#: bottom-left, bottom-right, horizontal, vertical).
SHAPES: dict[str, tuple[str, str, str, str, str, str]] = {
    "rect": ("┌", "┐", "└", "┘", "─", "│"),
    "round": ("╭", "╮", "╰", "╯", "─", "│"),
    "heavy": ("┏", "┓", "┗", "┛", "━", "┃"),
    "double": ("╔", "╗", "╚", "╝", "═", "║"),
    # An ellipse drawn as a real curve rather than a box with a glyph in the
    # edge. Only usable for short single-line labels: the text has to fit
    # between the slants, and a wide label degenerates into a rectangle with
    # unjoined corners.
    "ellipse": (".", ".", "'", "'", "-", "|"),
}

#: Arrowheads, by the direction the arrow is travelling.
ARROWS = {"right": "►", "left": "◄", "down": "▼", "up": "▲"}


@dataclass(frozen=True)
class Rect:
    """A box in character cells. ``width``/``height`` include the border."""

    x: int
    y: int
    width: int
    height: int

    @property
    def right(self) -> int:
        return self.x + self.width - 1

    @property
    def bottom(self) -> int:
        return self.y + self.height - 1

    @property
    def inner_width(self) -> int:
        return max(0, self.width - 2)

    @property
    def inner_height(self) -> int:
        return max(0, self.height - 2)

    def contains(self, x: int, y: int) -> bool:
        return self.x <= x <= self.right and self.y <= y <= self.bottom


class Canvas:
    """A grid of characters with locked regions.

    Deliberately small. It does not wrap, it does not scroll, and it does
    not decide anything about layout — it draws what it is told, once, and
    is then thrown away. That makes a picture a pure function of
    ``(model, width)``, which is the property the tests need.
    """

    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self._chars = [[" "] * self.width for _ in range(self.height)]
        self._styles: list[list[str | None]] = [
            [None] * self.width for _ in range(self.height)
        ]
        self._locked = [[False] * self.width for _ in range(self.height)]
        #: Cells that are a *frame* border, as opposed to a box. Tracked
        #: apart because an edge may cross one and may not cross the other.
        self._frame: set[tuple[int, int]] = set()
        #: Cells an edge asked for and could not have. Zero is the only
        #: acceptable value: a non-zero count means a line is broken in the
        #: middle and the arrowhead is no longer attached to it.
        self.skipped = 0

    # ----- cells -------------------------------------------------------

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def locked(self, x: int, y: int) -> bool:
        return self.inside(x, y) and self._locked[y][x]

    def solid(self, x: int, y: int) -> bool:
        """Occupied by something an edge may not write over.

        A box — its border or its text — is solid. So is anything an earlier
        edge has drawn that a later one must not run over, which is why an
        arrowhead locks its cell.

        A *frame* border is not solid, for the reason in the module
        docstring. The frame's interior is not solid either: it holds the
        boxes, and an edge has to be able to pass through the padding between
        them to reach a box on the far side.
        """
        return self.locked(x, y) and (x, y) not in self._frame

    def put(self, x: int, y: int, char: str, style: str | None = None,
            lock: bool = False) -> bool:
        """Write one character. Returns False if the cell was solid.

        A refused write is counted in ``skipped`` as well as reported, because
        a picture that silently lost part of a line is the one failure this
        canvas exists to make impossible.
        """
        if not self.inside(x, y):
            self.skipped += 1
            return False
        if self.solid(x, y):
            self.skipped += 1
            return False
        self._force(x, y, char, style)
        if lock:
            self._locked[y][x] = True
        return True

    def _force(self, x: int, y: int, char: str, style: str | None = None) -> None:
        """Write ignoring the lock. For the box itself, which lays down its
        own border and only then claims it."""
        if not self.inside(x, y):
            return
        self._chars[y][x] = char
        self._styles[y][x] = style

    def write(self, x: int, y: int, text: str, style: str | None = None) -> None:
        """Write a string left to right, stopping at the first locked cell."""
        for offset, char in enumerate(text):
            if not self.put(x + offset, y, char, style):
                return

    # ----- primitives --------------------------------------------------

    def hline(self, x: int, y: int, length: int, char: str,
              style: str | None = None, lock: bool = False) -> None:
        for offset in range(length):
            self.put(x + offset, y, char, style, lock)

    def vline(self, x: int, y: int, length: int, char: str,
              style: str | None = None, lock: bool = False) -> None:
        for offset in range(length):
            self.put(x, y + offset, char, style, lock)

    def box(
        self, rect: Rect, shape: str = "round", style: str | None = None,
        lock_inside: bool = True,
    ) -> None:
        """Draw a border and lock the cells it should not be crossed by.

        Locking the whole rectangle is what reserves a box's interior: an
        edge routed across its middle is refused rather than drawn through
        the label.

        A container passes ``lock_inside=False`` because the boxes it
        contains have not been drawn yet, and locking its whole area would
        refuse every one of them. Its *border* is still locked, which is
        what stops an edge crossing into or out of it.
        """
        top_left, top_right, bottom_left, bottom_right, horizontal, vertical = (
            SHAPES[shape]
        )
        # Every border character is written with `_force`, and the cells are
        # locked only afterwards. Locking first makes each border write
        # refuse itself, which is a bug this has now had twice.
        for y in range(rect.y, rect.bottom + 1):
            for x in range(rect.x, rect.right + 1):
                if not self.inside(x, y):
                    continue
                self._force(
                    x, y,
                    horizontal if y in (rect.y, rect.bottom) else " ",
                    style,
                )
        for offset in range(rect.height):
            self._force(rect.x, rect.y + offset, vertical, style)
            self._force(rect.right, rect.y + offset, vertical, style)
        for x, y, char in (
            (rect.x, rect.y, top_left),
            (rect.right, rect.y, top_right),
            (rect.x, rect.bottom, bottom_left),
            (rect.right, rect.bottom, bottom_right),
        ):
            self._force(x, y, char, style)
        for y in range(rect.y, rect.bottom + 1):
            for x in range(rect.x, rect.right + 1):
                if not self.inside(x, y):
                    continue
                on_border = (
                    y in (rect.y, rect.bottom) or x in (rect.x, rect.right)
                )
                if on_border or lock_inside:
                    self._locked[y][x] = True

    def label(self, rect: Rect, lines: list[str], style: str | None = None) -> None:
        """Centre *lines* in a box's interior, one per row.

        Centred rather than left-aligned: a box is a shape, and text set
        flush against one side reads as though it were escaping.
        """
        top = rect.y + 1 + max(0, (rect.inner_height - len(lines)) // 2)
        for offset, line in enumerate(lines):
            y = top + offset
            if not (rect.y < y < rect.bottom):
                continue
            x = rect.x + 1 + max(0, (rect.inner_width - len(line)) // 2)
            for index, char in enumerate(line):
                if rect.x < x + index < rect.right:
                    self._force(x + index, y, char, style)

    # ----- output ------------------------------------------------------

    def to_text(self) -> Text:
        out = Text()
        for y, row in enumerate(self._chars):
            if y:
                out.append("\n")
            for x, char in enumerate(row):
                style = self._styles[y][x]
                if style:
                    out.append(char, style=style)
                else:
                    out.append(char)
        return out

    # ----- edges -------------------------------------------------------

    def line(self, points: list[tuple[int, int]], style: str | None = None) -> None:
        """Draw an orthogonal polyline, skipping anything solid.

        Skipping rather than merging is the whole point: a solid cell is a
        box, and an arrow that ends up merged into one produces ``├┼─►`` and
        a different junction character on every crossing. Here a solid cell
        is left untouched and the run continues beyond it.

        A *frame* border is not solid, so a run crossing one replaces the
        border with the line character. See the module docstring for why.
        """
        for (x1, y1), (x2, y2) in zip(points, points[1:]):
            if x1 == x2:
                self.vline(x1, min(y1, y2), abs(y2 - y1) + 1, "│", style)
            else:
                self.hline(min(x1, x2), y1, abs(x2 - x1) + 1, "─", style)

    def arrow_head(
        self, x: int, y: int, direction: str, style: str | None = None,
        force: bool = False,
    ) -> bool:
        """Place an arrowhead, and claim the cell.

        *force* writes over a solid cell, which is only ever wanted for an
        edge arriving at a box from above or below: the head then sits in
        the middle of the border it is entering, which is the long-standing
        ASCII idiom for "it comes in here". The alternative — stopping a row
        short — lands the head on whatever else shares that row, which is
        usually the group's heading, and `defau▼t` is not a thing anyone
        wants to read.

        The cell is locked afterwards, so a later edge cannot draw its line
        through an arrowhead that is already there. Two edges sharing a run
        used to produce exactly that: the second overwrote the first's head
        and a correct edge came out as a plain rule running into a box. A
        later edge now routes around it, because the routing search skips
        solid cells and the head has made this one solid.
        """
        if force:
            if not self.inside(x, y):
                return False
            self._force(x, y, ARROWS[direction], style)
            self._locked[y][x] = True
            return True
        if self.solid(x, y):
            # Never on a box. A head belongs against the thing it points at,
            # and a frame is not the thing, so a head on a frame border is
            # refused even though a *line* may cross one.
            self.skipped += 1
            return False
        return self.put(x, y, ARROWS[direction], style, lock=True)

    # ----- containers --------------------------------------------------

    def container(
        self, rect: Rect, title: str, style: str | None = None, detail: str = ""
    ) -> None:
        """A frame around a group, with the group's name in its top edge.

        Distinct from a box on purpose: a container is something that
        *contains*, a box is a thing. Double lines for containment, so the
        nesting reads without having to work out which is which.

        *detail* is a second run of text in the same edge, after the name.
        A frame is only as wide as what is inside it, so anything that does
        not fit in a box has to go here or be cut.
        """
        self.box(rect, "double", style, lock_inside=False)
        # Remember which cells are the frame, so an edge routed out of this
        # container and into another can cross the border instead of being
        # refused by it. See `Canvas.solid`.
        for x in range(rect.x, rect.x + rect.width):
            for y in (rect.y, rect.y + rect.height - 1):
                if self.inside(x, y):
                    self._frame.add((x, y))
        for y in range(rect.y, rect.y + rect.height):
            for x in (rect.x, rect.x + rect.width - 1):
                if self.inside(x, y):
                    self._frame.add((x, y))
        room = max(0, rect.width - 4)
        if not title:
            return
        text = f" {title}"
        if detail:
            text += f" · {detail} "
        else:
            text += " "
        for offset, char in enumerate(text[:room]):
            cell = (rect.x + 2 + offset, rect.y)
            self._force(*cell, char, style)
            self._locked[cell[1]][cell[0]] = True
            # The title is text, not border. An edge crossing the top of a
            # frame replaces the border with a line; it must not replace
            # `default` with a dash.
            self._frame.discard(cell)

    def plain_lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self._chars]
