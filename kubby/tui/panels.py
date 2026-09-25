"""Main-screen panels for the kubby TUI.

The layout mirrors the plan's sketch: a left sidebar (minikube, machine,
images) beside a right-hand cluster panel, a streaming log strip, and a
keybar.  Every panel is bordered, focusable and lazygit-style, and the
focused one gets a brighter border/title.  Each panel carries its jump key
in its own title ("(1) minikube", "(2) machine", …), in brackets so the digit
cannot be misread for part of the name.

Navigable panels also move with vim's ``j``/``k`` for down/up.  The
namespaces tree additionally takes ``h``/``l`` to collapse/expand, the
convention every vim file-tree plugin uses.  The two list panels leave
``h``/``l`` unbound on purpose — a single column has no horizontal
dimension, so there is nothing honest for them to do.  Arrow keys keep
working everywhere; ``hjkl`` is an addition, not a replacement.

Panels are dumb about *doing*: a key press becomes a `PanelAction`
message, `kubby.tui.app` performs it (in a worker, against the service)
and pushes fresh state back through the `set_*` methods below.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import OptionList, RichLog, Static, Tree
from textual.widgets.option_list import Option

from kubby.cluster import describe_node
from kubby.tui import diagram as diagram_mod
from kubby.tui import paint as paint_mod
from kubby.tui import place as place_mod


class PanelFocused(Message):
    """Bubbles to the app when a panel gains focus, so the keybar can show
    that panel's keys."""

    def __init__(self, panel: "PanelBase") -> None:
        super().__init__()
        self.panel = panel


class PanelAction(Message):
    """A panel key was pressed; the app decides what that means."""

    def __init__(self, action: str, payload: Any = None) -> None:
        super().__init__()
        self.action = action
        self.payload = payload


class PanelBase:
    """Shared chrome + contract for the four main-screen panels.

    Concrete panels are *also* Textual widgets (the mixin sits leftmost in
    the MRO so its handlers run before the widget's own).
    """

    BORDER_TITLE: ClassVar[str] = ""
    #: The number that jumps straight to this panel. It is rendered into the
    #: panel's own title so the key you press is visible where you press it,
    #: instead of only in the help overlay. The app builds its binding from
    #: the same digit; `test_panel_titles_match_their_jump_keys` is what
    #: keeps the two from drifting apart.
    JUMP_KEY: ClassVar[str] = ""
    #: Shown when a panel has nothing to display.
    EMPTY_TEXT: ClassVar[str] = ""
    #: Every concrete panel picks up the `.panel` stylesheet rules
    #: (border, background, focus highlight) automatically.
    DEFAULT_CLASSES = "panel"

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Set the border title, prefixing its jump key when assigned.

        Pass positional and keyword arguments to the widget constructor.
        """
        super().__init__(*args, **kwargs)
        # A Text, not a markup string: Textual renders a border title
        # literally, so "[bold]1[/]" would show up as those characters.
        title = Text()
        if self.JUMP_KEY:
            # Parentheses mark the digit as a key rather than part of the
            # name — "1 minikube" alone reads like a version. The brackets
            # are dim so the digit itself carries the emphasis.
            title.append("(", style="dim")
            title.append(self.JUMP_KEY, style="bold cyan")
            title.append(") ", style="dim")
        title.append(self.BORDER_TITLE)
        self.border_title = title

    def on_focus(self) -> None:
        self.post_message(PanelFocused(self))

    def _request(self, action: str, payload: Any = None) -> None:
        self.post_message(PanelAction(action, payload))


#: Vim-style vertical movement, shared by the navigable panels (both
#: single-column list panels and the namespaces tree) so ``j``/``k`` is
#: written once.
#:
#: A constant rather than a mixin on purpose: Textual *replaces* BINDINGS
#: along the MRO instead of merging them, so a mixin's bindings are silently
#: dropped the moment a panel declares its own. A panel therefore has to
#: include this explicitly — see ``ImagesPanel`` and ``ClusterPanel``.
#:
#: ``h``/``l`` are deliberately absent: a one-column list has no horizontal
#: dimension, so there is nothing for them to do, and inventing a meaning
#: would be worse than leaving them inert. The help overlay says so, so it
#: does not read as an oversight.
#:
#: ``show=False`` because the keybar has only ~27 characters of headroom at
#: 100 columns, and four nav keys per panel would overflow it. The keys are
#: documented in the help overlay instead, like the panel jump numbers.
LIST_NAV_BINDINGS: list[Binding] = [
    Binding("j", "cursor_down", "down", show=False),
    Binding("k", "cursor_up", "up", show=False),
]


def format_size(size: Any) -> str:
    """Human-readable byte count; passes already-formatted strings through."""
    if isinstance(size, str):
        stripped = size.strip()
        if not stripped.isdigit():
            return stripped  # podman (or a fake) already formatted it
        size = int(stripped)
    if not isinstance(size, int) or size <= 0:
        return ""
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)}B"
            return f"{value:.1f}{unit}"
        value /= 1024
    return ""  # unreachable: the TB branch always returns


class MinikubePanel(PanelBase, Vertical, can_focus=True):
    """Cluster summary — context, version, reachable/not — plus the
    start/stop/delete keys."""

    BORDER_TITLE = "minikube"
    JUMP_KEY = "1"
    EMPTY_TEXT = "no cluster"

    BINDINGS = [
        Binding("s", "request('start')", "start"),
        Binding("S", "request('stop')", "stop"),
        Binding("d", "request('delete')", "delete"),
        Binding("o", "request('settings')", "settings"),
    ]

    def compose(self) -> ComposeResult:
        yield Static(Text(self.EMPTY_TEXT, style="dim"), id="minikube-body")

    def action_request(self, action: str) -> None:
        self._request(action)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action != "request":
            return super().check_action(action, parameters)
        if self.app.is_busy:
            return None  # visible but grayed while something is running
        kind = parameters[0] if parameters else None
        if kind == "settings":
            # Editing config never depends on cluster state (the GUI only
            # hid it while a job was running, same as here).
            return True
        running = bool(self.app.cluster.get("running"))
        # Start only makes sense without a cluster; stop/delete need one.
        return (not running) if kind == "start" else running

    def set_cluster(self, info: dict[str, Any]) -> None:
        body = self.query_one("#minikube-body", Static)
        body.update(self._cluster_text(info))

    @staticmethod
    def _cluster_text(info: dict[str, Any]) -> Text:
        if not info.get("running"):
            out = Text()
            out.append("○ ", style="dim red")
            out.append("not running", style="dim red")
            error = info.get("error")
            if error:
                out.append("\n")
                out.append(str(error), style="dim")
            return out

        out = Text()
        out.append("● ", style="green")
        out.append("running", style="bold green")
        version = info.get("version")
        if version:
            out.append(f"  {version}", style="cyan")
        context = info.get("context")
        if context:
            out.append("\n")
            out.append("ctx ", style="dim")
            out.append(str(context))
        nodes = info.get("nodes") or []
        if nodes:
            ready = sum(1 for n in nodes if n.get("status") == "Ready")
            out.append("\n")
            out.append(
                f"{ready}/{len(nodes)}",
                style="green" if ready == len(nodes) else "yellow",
            )
            out.append(" nodes ready", style="dim")
        return out


class MachinePanel(PanelBase, VerticalScroll, can_focus=True):
    """The machine the cluster runs on, as Kubernetes sees it.

    What the node *is*, rather than what runs on it: its own operating
    system, its own container runtime, the address the host reaches it on,
    the range pod addresses come from, and the share of CPU and memory a
    Pod can actually ask for. That share is the explanation for a Pod stuck
    ``Pending`` with "insufficient cpu", and it is invisible in a list of
    pods.

    This took the tools panel's place rather than joining it. A node's
    figures answer "why is my Pod not starting", which is the question
    somebody is asking while they look at a cluster; a list of installed
    binaries does not, and once the graph stopped drawing the machine as a
    box there was nowhere else for these facts to go. Tools are still
    reported by ``kubby --check`` and by the preflight — they are just not
    the story the sidebar should be telling.
    """

    BORDER_TITLE = "machine"
    JUMP_KEY = "2"
    EMPTY_TEXT = "no cluster"

    #: Listed explicitly rather than mixed in, because Textual *replaces*
    #: BINDINGS along the MRO instead of merging them, and ScrollableWidget
    #: has its own. Same reasoning as `GraphPanel`: j/k scroll, because that
    #: is what they do on a read-only view, and the same gesture moves a
    #: cursor on a list.
    BINDINGS = [
        Binding("j", "scroll_down", "scroll", show=False),
        Binding("k", "scroll_up", "scroll", show=False),
        # The picture scrolls sideways when it is wider than the panel, and
        # h/l are the horizontal half of the same gesture j/k already is.
        Binding("l", "scroll_right", "scroll", show=False),
        Binding("h", "scroll_left", "scroll", show=False),
    ]

    def compose(self) -> ComposeResult:
        yield Static(Text(self.EMPTY_TEXT, style="dim"), id="machine-body")

    def set_cluster(self, info: dict[str, Any]) -> None:
        self.query_one("#machine-body", Static).update(self._machine_text(info))

    @staticmethod
    def _nothing_to_say(info: dict[str, Any]) -> str:
        """What to show when there are no node facts to describe.

        The reason when there is no cluster, and a plain statement when there
        is one but its nodes could not be read — a panel that silently shows
        nothing looks like a machine with no specifications.
        """
        if not info.get("running", True):
            return str(info.get("error") or "not running")
        return "no node facts"

    @staticmethod
    def _machine_text(info: dict[str, Any]) -> Text:
        # The node's own facts come from the graph payload rather than the
        # inventory: the inventory's node list is name/status/roles, and
        # widening it for this panel would cost every caller the `nodeInfo`
        # and capacity blocks it has no use for.
        graph = info.get("graph") or {}
        facts = graph.get("node_facts") or []
        if not facts:
            return Text(MachinePanel._nothing_to_say(info), style="dim")

        out = Text()
        driver = str(graph.get("driver") or "")
        head = " · ".join(
            part
            for part in (str(info.get("version") or ""), f"{driver} driver" if driver else "")
            if part
        )
        if head:
            out.append(head, style="cyan")

        for index, fact in enumerate(facts):
            out.append("\n")
            ready = str(fact.get("status") or "") == "Ready"
            out.append("● " if ready else "○ ", style="green" if ready else "yellow")
            out.append(str(fact.get("name") or "the machine"), style="bold")
            roles = [str(r) for r in fact.get("roles") or []]
            if roles:
                out.append(f"  {', '.join(roles)}", style="dim")
            # `describe_node`'s first line is the name, already shown above
            # beside its ready state.
            for line in describe_node(fact)[1:]:
                out.append("\n")
                out.append(line, style="dim")
            if index < len(facts) - 1:
                out.append("\n")
        return out


class ImagesPanel(PanelBase, OptionList):
    """Local podman images (no network), filterable with ``/``."""

    BORDER_TITLE = "images"
    JUMP_KEY = "3"
    EMPTY_TEXT = "no local images"

    BINDINGS = LIST_NAV_BINDINGS + [
        Binding("slash", "request('filter')", "filter"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=False, **kwargs)
        #: Full image dicts as reported by the service (the filter matches
        #: these, while the rows show the short name).
        self._images: list[dict[str, Any]] = []

    def action_request(self, action: str) -> None:
        self._request(action)

    def set_images(
        self, images: list[dict[str, Any]], query: str = ""
    ) -> None:
        self._images = list(images)
        rows = images
        needle = query.strip().lower()
        if needle:
            rows = [
                image
                for image in images
                if needle in str(image.get("name", "")).lower()
                or any(needle in str(tag).lower() for tag in image.get("tags") or [])
            ]
        self.clear_options()
        if not rows:
            label = (
                f"no image matches “{query.strip()}”"
                if needle
                else self.EMPTY_TEXT
            )
            self.add_option(Option(Text(label, style="dim")))
            return
        for image in rows:
            self.add_option(Option(self._row(image)))
        if self.highlighted is None:
            self.highlighted = 0

    @staticmethod
    def _row(image: dict[str, Any]) -> Text:
        name = str(image.get("name") or "?")
        # Display the familiar short name; the filter matches the full one.
        short = name.split("/")[-1]
        row = Text(short)
        size = format_size(image.get("size"))
        if size:
            row.append(f"  {size}", style="dim")
        return row


class GraphPanel(PanelBase, VerticalScroll, can_focus=True):
    """A drawn picture of the cluster's wiring, scrollable.

    Read-only for now, which is a deliberate first step rather than a
    limitation: the layout that positions the boxes already reports where
    each one landed, so moving a cursor between them later is a search over
    those rectangles and not a layout engine.

    It scrolls because a real cluster does not fit a terminal panel. A
    13-workload cluster came out 72 columns by 63 lines against a panel of
    roughly 62 by 24, and no amount of compaction fixes that — the overflow
    is boxes side by side, not spacing. The layout wraps groups into bands
    to fit the panel width, so what does overflow is the axis that
    scrolls the way a person expects.
    """

    BORDER_TITLE = "cluster graph"
    JUMP_KEY = "4"
    EMPTY_TEXT = "no cluster"

    #: Sits inside the cluster panel's own border, so it must not draw a
    #: second one — a border inside a border reads as two panels.
    DEFAULT_CLASSES = "panel-inner"

    #: Scrolls sideways as well as down. A namespace with seven objects in
    #: it is 33 columns wide whatever the panel is, and a 50-column terminal
    #: leaves this panel 13 — so without it, ten to twenty columns of the
    #: cluster were clipped and unreachable, while the code and its comments
    #: claimed the panel scrolled. The picture cannot be made narrower than
    #: its widest namespace, so the honest answer is to let the reader move
    #: across it.
    DEFAULT_CSS = """
    GraphPanel {
        overflow-x: auto;
    }
    """

    #: Listed explicitly rather than mixed in, because Textual *replaces*
    #: BINDINGS along the MRO instead of merging them, and ScrollableWidget
    #: has its own. j/k are the movement keys of record everywhere in this
    #: app; on a picture they scroll it — the same gesture as moving a
    #: cursor, over a canvas instead of a list.
    BINDINGS = [
        Binding("j", "scroll_down", "scroll", show=False),
        Binding("k", "scroll_up", "scroll", show=False),
        # The picture scrolls sideways when it is wider than the panel, and
        # h/l are the horizontal half of the same gesture j/k already is.
        Binding("l", "scroll_right", "scroll", show=False),
        Binding("h", "scroll_left", "scroll", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._cluster: dict[str, Any] = {}
        self._picture = Static("", id="graph-picture")
        #: The last placement, kept so the `hjkl` cursor can move between
        #: boxes without re-laying the picture out on every keypress.
        self.placement: place_mod.Placement | None = None

    def compose(self) -> ComposeResult:
        yield self._picture

    def _clear(self) -> None:
        """Forget the last picture, so a panel with nothing to draw does not
        keep its width and its scroll range.

        Without this, a cluster that stops being readable left a panel reading
        `cannot draw the cluster` with a horizontal scrollbar out to the
        width of the picture it was showing a moment ago, and a `placement`
        pointing at boxes that are not there."""
        self.placement = None
        self.border_subtitle = None
        self._picture.styles.width = None

    def set_cluster(self, cluster: dict[str, Any]) -> None:
        self._cluster = cluster or {}
        self.render_picture()

    def on_resize(self) -> None:
        """Redraw when this panel changes size.

        Handled here rather than on the app: the app's own resize arrives
        before the new layout has been applied, so the panel still reports
        its *previous* width and the picture comes out identical. Deferred
        from there with ``call_after_refresh`` it was reliably one resize
        behind, which looked like a picture that had frozen at its launch
        size until `R` was pressed.
        """
        self.render_picture()

    def render_picture(self) -> None:
        """Draw the picture for the current cluster and panel size."""
        cluster = self._cluster
        if not cluster:
            self._clear()
            self._picture.update(Text(self.EMPTY_TEXT, style="dim"))
            return

        if not cluster.get("available", True):
            # Say *why* rather than showing an empty panel: a picture that
            # silently fails to draw looks like a broken cluster.
            body = Text("cannot draw the cluster", style="dim")
            body.append(f"\n{cluster.get('error') or 'unavailable'}",
                        style="yellow")
            self._clear()
            self._picture.update(body)
            return

        self.border_subtitle = f"{cluster.get('pod_count') or 0} pods"
        width = max(20, self.size.width)
        placement = place_mod.place(diagram_mod.build_diagram(cluster), width)
        # Centred rather than left-aligned, which is what the tree looks like
        # because a tree is a list. A picture with a shape wants the space
        # either side of it. Only applied when it fits — see `centre`.
        self.placement = placement
        self._picture.update(paint_mod.centre(paint_mod.render(placement), width))
        # The Static is shrunk to the panel by default, so the panel's own
        # horizontal scrollbar has nothing to scroll: the virtual size stays
        # the panel's width and the columns past it are simply gone. Stating
        # the width is what makes them reachable.
        self._picture.styles.width = max(placement.width + 1, width)


class ClusterPanel(PanelBase, Vertical, can_focus=True):
    """The cluster, in two views: a picture, or the namespace tree.

    The graph is the default. Someone who does not yet know what the pieces
    are gets a picture of how they connect, which is the question the tree
    cannot answer; the tree stays one keypress away for the times you want
    to walk namespaces and pods row by row.

    Both views are always mounted and only one is displayed, so switching is
    instant and neither view loses its scroll position or cursor.
    """

    BORDER_TITLE = "cluster"
    JUMP_KEY = "4"
    EMPTY_TEXT = "no cluster"

    BINDINGS = [
        Binding("g", "show_graph", "graph"),
        Binding("t", "show_tree", "tree"),
    ]

    #: Which view is showing. `graph` is the default: it is the reason this
    #: panel exists now, and the tree is a keypress away.
    DEFAULT_VIEW = "graph"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.view = self.DEFAULT_VIEW

    def compose(self) -> ComposeResult:
        yield GraphPanel()
        yield ClusterTree()

    # ----- switching views ---------------------------------------------

    def _show(self, view: str) -> None:
        self.view = view
        self.query_one(GraphPanel).display = view == "graph"
        self.query_one(ClusterTree).display = view == "tree"
        # Focus has to follow the visible view or the keys go to a hidden
        # widget and the panel reads as unresponsive.
        target = (
            self.query_one(GraphPanel) if view == "graph" else self.query_one(ClusterTree)
        )
        target.focus()
        self.post_message(PanelFocused(self))

    def action_show_graph(self) -> None:
        self._show("graph")

    def action_show_tree(self) -> None:
        self._show("tree")

    def on_mount(self) -> None:
        self.query_one(GraphPanel).display = self.view == "graph"
        self.query_one(ClusterTree).display = self.view == "tree"

    def set_cluster(self, info: dict[str, Any]) -> None:
        """Hand the cluster to both views; each picks the part it needs."""
        self.query_one(ClusterTree).set_cluster(info)
        self.query_one(GraphPanel).set_cluster(info.get("graph") or {})


class ClusterTree(PanelBase, Tree):
    """Namespaces with their pods.

    Navigable with ``j``/``k`` and collapsible with ``h``/``l``, vim-style;
    the arrow keys, enter and space continue to work as before.
    """

    BORDER_TITLE = "namespaces"
    JUMP_KEY = "4"
    EMPTY_TEXT = "no cluster"

    #: Sits inside the cluster panel's border, so it must not draw one.
    DEFAULT_CLASSES = "panel-inner"

    BINDINGS = LIST_NAV_BINDINGS + [
        Binding("h", "vim_collapse", "collapse", show=False),
        Binding("l", "vim_expand", "expand", show=False),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        # Hide the synthetic root so namespaces sit at the top edge of the
        # panel; `Tree.__init__` doesn't take the flag, so set it here.
        self.show_root = False

    # ----- vim h / l ---------------------------------------------------

    def _current_node(self) -> Any | None:
        """The node under the cursor, or None on an empty/short tree.

        Guarded like Textual's own `action_toggle_node`, which catches
        IndexError from the same lookup: a collapsed or empty tree must be a
        no-op here rather than a crash on a keypress.
        """
        if self.cursor_line < 0:
            return None
        try:
            return self._tree_lines[self.cursor_line]
        except IndexError:
            return None

    def action_vim_collapse(self) -> None:
        """`h` — collapse the current namespace, or step out to its parent.

        One key does both, which is what makes `h` useful rather than a
        toggle you have to keep pressing: collapse once to fold the pods
        away, press again to climb to the namespace above.
        """
        line = self._current_node()
        if line is None:
            return
        node = line.path[-1]
        if node.allow_expand and node.is_expanded:
            node.collapse()
        elif node.parent is not self.root:
            self.action_cursor_parent()

    def action_vim_expand(self) -> None:
        """`l` — expand the current namespace, or step into its first pod.

        `Tree` has no combined expand-or-descend action, so this is the one
        piece of new behaviour here; everything else maps onto an action the
        widget already provides.
        """
        line = self._current_node()
        if line is None:
            return
        node = line.path[-1]
        if not node.allow_expand:
            return  # a pod, not a namespace
        if not node.is_expanded:
            node.expand()
        elif node.children:
            self.move_cursor(node.children[0])

    def set_cluster(self, info: dict[str, Any]) -> None:
        self.clear()

        nodes = info.get("nodes") or []
        if nodes:
            ready = sum(1 for n in nodes if n.get("status") == "Ready")
            self.border_subtitle = f"{ready}/{len(nodes)} ready"
        else:
            self.border_subtitle = None

        if not info.get("running"):
            self.root.add_leaf(Text(self.EMPTY_TEXT, style="dim"))
        elif not (info.get("namespaces") or []):
            self.root.add_leaf(Text("no namespaces", style="dim"))
        else:
            for namespace in info["namespaces"]:
                pods = namespace.get("pods") or []
                label = Text(str(namespace.get("name") or "?"))
                label.append(f"  {len(pods)}", style="dim")
                label.append(" pods" if len(pods) != 1 else " pod", style="dim")
                # Expanded by default so pods are visible without a keypress.
                node = self.root.add(label, data=namespace, expand=True)
                for pod in pods:
                    node.add(self._pod_label(pod), data=pod, allow_expand=False)

        # Tree starts with no cursor (show_root is off); a focused panel
        # always shows one, and the existing position survives a refresh.
        if self.cursor_line < 0:
            self.cursor_line = 0

    @staticmethod
    def _pod_label(pod: dict[str, Any]) -> Text:
        name = str(pod.get("name") or "?")
        status = str(pod.get("status") or "Unknown")
        if status in ("Running", "Succeeded", "Completed"):
            style = "green"
        elif status in ("Pending", "ContainerCreating"):
            style = "yellow"
        else:
            style = "red"
        label = Text(name)
        label.append(f"  {status}", style=style)
        return label


class LogPanel(RichLog):
    """Streaming job output with job-scoped dismissal.

    Dismissal follows the semantics fixed in the GUI (see the plan): the
    ``dismissed`` flag belongs to the *current* job, it beats the failure
    pin, and a new job clears it.
    """

    BORDER_TITLE = "log"
    can_focus = False

    def __init__(self) -> None:
        super().__init__(
            id="log",
            wrap=False,
            highlight=False,
            markup=False,
            auto_scroll=True,
        )
        #: Every line written this session — handy for assertions.
        self.history: list[str] = []
        self.dismissed = False
        self.pinned = False
        self.job_active = False

    def append(self, line: str) -> None:
        """Write one line of job output (UI thread only)."""
        self.history.append(line)
        self.write(line)

    # ----- dismissal state machine -------------------------------------

    def begin_job(self) -> None:
        """A new job starts: forget the previous job's dismissal (rule 4)."""
        self.dismissed = False
        self.pinned = False
        self.job_active = True
        self.sync()

    def end_job(self, ok: bool) -> None:
        """The job finished; a failure pins the log open (rule 5) — unless
        this job was dismissed, which wins over the pin (rule 2)."""
        self.job_active = False
        if not ok and not self.dismissed:
            self.pinned = True
        self.sync()

    def dismiss(self) -> None:
        """``x`` — hide the log for the current job; hides clear the pin (rule 3)."""
        self.dismissed = True
        self.pinned = False
        self.sync()

    def sync(self) -> None:
        """Apply rules 2 and 5: visible unless dismissed, and only while
        a job is running or a failure is pinned."""
        visible = (self.job_active or self.pinned) and not self.dismissed
        self.display = visible
