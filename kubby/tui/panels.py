"""Main-screen panels for the kubby TUI.

The layout mirrors the plan's sketch: a left sidebar (minikube, tools,
images) beside a right-hand namespaces tree, a streaming log strip, and a
keybar.  Every panel is bordered, focusable and lazygit-style, and the
focused one gets a brighter border/title.  Each panel carries its jump key
in its own title ("1 minikube", "2 tools", …) so the number is visible
where you press it.

Panels are dumb about *doing*: a key press becomes a `PanelAction`
message, `kubby.tui.app` performs it (in a worker, against the service)
and pushes fresh state back through the `set_*` methods below.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import OptionList, RichLog, Static, Tree
from textual.widgets.option_list import Option


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
        super().__init__(*args, **kwargs)
        # A Text, not a markup string: Textual renders a border title
        # literally, so "[bold]1[/]" would show up as those characters.
        title = Text()
        if self.JUMP_KEY:
            title.append(f"{self.JUMP_KEY} ", style="bold cyan")
        title.append(self.BORDER_TITLE)
        self.border_title = title

    def on_focus(self) -> None:
        self.post_message(PanelFocused(self))

    def _request(self, action: str, payload: Any = None) -> None:
        self.post_message(PanelAction(action, payload))


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


class ToolsPanel(PanelBase, OptionList):
    """Managed tools and their install state."""

    BORDER_TITLE = "tools"
    JUMP_KEY = "2"
    EMPTY_TEXT = "no tools"

    BINDINGS = [
        Binding("i", "request('install')", "install"),
        Binding("I", "request('install_all')", "install all"),
    ]

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=False, **kwargs)

    def action_request(self, action: str) -> None:
        self._request(action, self.selected_tool() if action == "install" else None)

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action != "request":
            return super().check_action(action, parameters)
        if self.app.is_busy:
            return None
        if parameters and parameters[0] == "install":
            return self.selected_tool() is not None
        return True

    def selected_tool(self) -> str | None:
        """Registry key of the highlighted row (None for the empty state)."""
        index = self.highlighted
        if index is None:
            return None
        try:
            option = self.get_option_at_index(index)
        except Exception:
            return None
        return option.id

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
        # Keep the user's selection across a refresh (post-install, rows move).
        selected = self.selected_tool()
        self.clear_options()
        if not tools:
            self.add_option(Option(Text(self.EMPTY_TEXT, style="dim")))
            return
        for tool in tools:
            label = str(tool.get("label") or tool.get("key") or "?")
            if tool.get("installed"):
                row = Text("✓ ", style="green")
                row.append(f"{label:<9}")
                row.append(str(tool.get("version") or "installed"), style="dim")
            else:
                row = Text("✗ ", style="red")
                row.append(f"{label:<9}")
                row.append("not installed", style="dim")
            self.add_option(Option(row, id=str(tool.get("key"))))
        if selected is not None:
            for index, tool in enumerate(tools):
                if str(tool.get("key")) == selected:
                    self.highlighted = index
                    break
        if self.highlighted is None:
            # OptionList starts with nothing highlighted; a row must always
            # be actionable, otherwise `i` has no target after a refresh.
            self.highlighted = 0


class ImagesPanel(PanelBase, OptionList):
    """Local podman images (no network), filterable with ``/``."""

    BORDER_TITLE = "images"
    JUMP_KEY = "3"
    EMPTY_TEXT = "no local images"

    BINDINGS = [
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


class ClusterPanel(PanelBase, Tree):
    """Namespaces with their pods, expandable (arrows / enter / left-right)."""

    BORDER_TITLE = "namespaces"
    JUMP_KEY = "4"
    EMPTY_TEXT = "no cluster"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__("", **kwargs)
        # Hide the synthetic root so namespaces sit at the top edge of the
        # panel; `Tree.__init__` doesn't take the flag, so set it here.
        self.show_root = False

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
