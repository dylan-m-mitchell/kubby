"""Main-screen panels for the kubby TUI.

The layout mirrors the plan's sketch: a left sidebar (minikube, tools,
images) beside a right-hand namespaces tree, a streaming log strip, and a
keybar.  Every panel is bordered, focusable and lazygit-style — Tab
cycles between them and the focused one gets a brighter border/title.

Panels are dumb: they *render* state handed to them by `kubby.tui.app`,
which owns fetching, actions and key handling.
"""

from __future__ import annotations

from typing import Any, ClassVar

from rich.text import Text
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import OptionList, RichLog, Static, Tree
from textual.widgets.option_list import Option


class PanelBase:
    """Shared chrome + contract for the four main-screen panels.

    Concrete panels are *also* Textual widgets (the mixin sits leftmost in
    the MRO so its handlers run before the widget's own).
    """

    BORDER_TITLE: ClassVar[str] = ""
    #: Shown when a panel has nothing to display.
    EMPTY_TEXT: ClassVar[str] = ""
    #: Every concrete panel picks up the `.panel` stylesheet rules
    #: (border, background, focus highlight) automatically.
    DEFAULT_CLASSES = "panel"


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
    """Cluster summary — context, version, reachable/not."""

    BORDER_TITLE = "minikube"
    EMPTY_TEXT = "no cluster"

    def compose(self) -> ComposeResult:
        yield Static(Text(self.EMPTY_TEXT, style="dim"), id="minikube-body")

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
            out.append(f"{ready}/{len(nodes)}", style="green" if ready == len(nodes) else "yellow")
            out.append(" nodes ready", style="dim")
        return out


class ToolsPanel(PanelBase, OptionList):
    """Managed tools and their install state."""

    BORDER_TITLE = "tools"
    EMPTY_TEXT = "no tools"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=False, **kwargs)

    def set_tools(self, tools: list[dict[str, Any]]) -> None:
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
            self.add_option(Option(row))


class ImagesPanel(PanelBase, OptionList):
    """Local podman images (no network)."""

    BORDER_TITLE = "images"
    EMPTY_TEXT = "no local images"

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(markup=False, **kwargs)
        #: Full image dicts as reported by the service (the filter matches
        #: these, while the rows show the short name).
        self._images: list[dict[str, Any]] = []

    def set_images(self, images: list[dict[str, Any]]) -> None:
        self.clear_options()
        self._images = list(images)
        if not images:
            self.add_option(Option(Text(self.EMPTY_TEXT, style="dim")))
            return
        for image in images:
            self.add_option(Option(self._row(image)))

    def _row(self, image: dict[str, Any]) -> Text:
        name = str(image.get("name") or "?")
        # Display the familiar short name; the filter matches the full one.
        short = name.split("/")[-1]
        row = Text(short)
        size = format_size(image.get("size"))
        if size:
            row.append(f"  {size}", style="dim")
        return row


class ClusterPanel(PanelBase, Tree):
    """Namespaces, ready to grow pod rows (Phase 3)."""

    BORDER_TITLE = "namespaces"
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
            return

        namespaces = info.get("namespaces") or []
        if not namespaces:
            self.root.add_leaf(Text("no namespaces", style="dim"))
            return
        for namespace in namespaces:
            pods = namespace.get("pods") or []
            label = Text(str(namespace.get("name") or "?"))
            label.append(f"  {len(pods)}", style="dim")
            label.append(" pods" if len(pods) != 1 else " pod", style="dim")
            self.root.add_leaf(label, data=namespace)


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
        """The job finished; a failure pins the log open (rule 5)."""
        self.job_active = False
        if not ok:
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
