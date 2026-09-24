"""kubby TUI application shell.

Layout (top → bottom):

    status line
    ┌ sidebar (minikube / tools / images) ┐┌ namespaces ┐
    └──────────────────────────────────────┘└────────────┘
    log panel
    keybar

The shell owns fetching and marshaling: everything that blocks (kubectl,
podman, subprocessing generally) runs in a Textual worker thread, and
results come back to the UI thread either through ``call_from_thread``
(worker completion) or by posting a message (streaming output, which can
arrive from arbitrary service threads).
"""

from __future__ import annotations

import logging
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Static

from kubby.service import KubbyService
from kubby.tui.panels import (
    ClusterPanel,
    ImagesPanel,
    LogPanel,
    MinikubePanel,
    ToolsPanel,
)
from kubby.tui.popups import HelpScreen

log = logging.getLogger("kubby")

#: ``(key, label)`` pairs shown in the keybar and the help overlay.
GLOBAL_KEYS: tuple[tuple[str, str], ...] = (
    ("tab", "next panel"),
    ("shift+tab", "previous panel"),
    ("R", "re-check"),
    ("?", "help"),
    ("q", "quit"),
)


class LogLine(Message):
    """One line of job output, posted from a service thread."""

    def __init__(self, line: str) -> None:
        super().__init__()
        self.line = line


def render_keybar() -> Text:
    out = Text()
    for index, (key, label) in enumerate(GLOBAL_KEYS):
        if index:
            out.append("   ")
        out.append(f'"{key}"', style="bold cyan")
        out.append(f" {label}", style="dim")
    return out


def render_status(
    system: dict[str, Any], cluster: dict[str, Any]
) -> Text:
    """Compose the header line: state, context, version, nodes, host path."""
    out = Text()
    out.append("kubby", style="bold white")

    out.append("  ")
    if cluster.get("running"):
        out.append("● running", style="bold green")
        version = cluster.get("version")
        if version:
            out.append(f"  {version}", style="cyan")
    else:
        out.append("○ no cluster", style="dim red")

    context = cluster.get("context")
    if context:
        out.append("  │ ", style="dim")
        out.append("ctx: ", style="dim")
        out.append(str(context), style="bold")

    nodes = cluster.get("nodes") or []
    if nodes:
        ready = sum(1 for n in nodes if n.get("status") == "Ready")
        out.append("  │ ", style="dim")
        out.append(
            f"{ready}/{len(nodes)} nodes",
            style="green" if ready == len(nodes) else "yellow",
        )

    pkg_mgr = system.get("package_manager_label")
    elevation = system.get("elevation")
    if pkg_mgr or elevation:
        out.append("  │ ", style="dim")
        # `elevation` reads "pkexec (graphical polkit prompt)" — keep the verb.
        elevation_verb = str(elevation).split("(")[0].strip() if elevation else ""
        out.append(f"{pkg_mgr} + {elevation_verb}" if elevation_verb else str(pkg_mgr),
                  style="dim")
    return out


class KubbyApp(App[None]):
    """The kubby TUI."""

    CSS_PATH = "styles.tcss"
    TITLE = "kubby"
    BINDINGS = [
        Binding("tab", "focus_next", "next panel", show=False),
        Binding("shift+tab", "focus_previous", "previous panel", show=False),
        Binding("R", "recheck", "re-check"),
        Binding("question_mark", "show_help", "help"),
        Binding("q", "quit", "quit"),
    ]

    def __init__(self, service: KubbyService | None = None) -> None:
        super().__init__()
        # Tests inject a fake; everything the shell touches goes through it.
        self.service = service if service is not None else KubbyService()
        # Streaming log callbacks arrive on job threads — they must only
        # ever touch the UI through thread-safe plumbing.
        self.service.on_log = self._post_log_line

        self.system: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.cluster: dict[str, Any] = {}
        self.images: list[dict[str, Any]] = []

    # ----- compose / lifecycle -----------------------------------------

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield MinikubePanel(id="minikube")
                yield ToolsPanel(id="tools")
                yield ImagesPanel(id="images")
            yield ClusterPanel(id="cluster")
        yield LogPanel()
        yield Static(render_keybar(), id="keybar")

    def on_mount(self) -> None:
        self.query_one(MinikubePanel).focus()
        # Rule 5: idle means the log is hidden until a job starts.
        self.query_one(LogPanel).sync()
        self.refresh_data()

    # ----- service sinks (any thread) -----------------------------------

    def _post_log_line(self, line: str) -> None:
        """`on_log` — called from job threads. `post_message` is thread-safe."""
        self.post_message(LogLine(line))

    def on_log_line(self, message: LogLine) -> None:
        self.query_one(LogPanel).append(message.line)

    # ----- data refresh (worker thread → UI thread) ----------------------

    def refresh_data(self) -> None:
        """Fetch host/tool/cluster/image state off the UI thread."""
        self._refresh()

    @work(thread=True, exclusive=True, group="refresh")
    def _refresh(self) -> None:
        system = self.service.system_info()
        tools = self.service.get_status()
        cluster = self.service.get_cluster_info()
        images = self.service.list_local_images()
        self._call_on_ui(self._apply_data, system, tools, cluster, images)

    def _call_on_ui(self, callback: Any, *args: Any) -> None:
        """Run *callback* on the UI thread from a worker."""
        try:
            self.call_from_thread(callback, *args)
        except RuntimeError:
            # App isn't running (startup/shutdown race) — nothing to update.
            log.debug("dropped UI update: %s", getattr(callback, "__name__", callback))

    def _apply_data(
        self,
        system: dict[str, Any],
        tools: list[dict[str, Any]],
        cluster: dict[str, Any],
        images: list[dict[str, Any]],
    ) -> None:
        """UI thread: store state and render it."""
        self.system = system
        self.tools = tools
        self.cluster = cluster
        self.images = images

        self.query_one("#status", Static).update(render_status(system, cluster))
        self.query_one(MinikubePanel).set_cluster(cluster)
        self.query_one(ToolsPanel).set_tools(tools)
        self.query_one(ClusterPanel).set_cluster(cluster)
        self.query_one(ImagesPanel).set_images(images)

    # ----- actions -------------------------------------------------------

    def action_recheck(self) -> None:
        self.refresh_data()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen(self.help_rows()))

    def help_rows(self) -> list[tuple[str, str]]:
        """Key table for the help overlay (grows with the panels)."""
        return list(GLOBAL_KEYS)
