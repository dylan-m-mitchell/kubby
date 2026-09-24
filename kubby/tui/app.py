"""kubby TUI application shell.

Layout (top → bottom):

    status line
    ┌ sidebar (minikube / tools / images) ┐┌ namespaces ┐
    └──────────────────────────────────────┘└────────────┘
    log panel
    [image filter bar — only while filtering]
    keybar

The shell owns fetching and marshaling: anything that can block (kubectl,
podman, subprocess generally) runs in a Textual worker thread, and results
reach the UI thread either through ``call_from_thread`` (worker completion)
or by posting a message (streaming output, which arrives from arbitrary
service threads).

Actions follow one rule: panels *ask* (``PanelAction``), the app *does*.
The single-job contract itself lives in ``KubbyService`` — the UI only
reflects it through ``is_busy`` and the bindings' enabled state, it never
re-implements it.
"""

from __future__ import annotations

import logging
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Input, Static

from kubby.service import KubbyService
from kubby.tui.panels import (
    ClusterPanel,
    ImagesPanel,
    LogPanel,
    MinikubePanel,
    PanelAction,
    PanelFocused,
    ToolsPanel,
)
from kubby.tui.popups import ConfirmModal, HelpScreen, PrereqModal

log = logging.getLogger("kubby")

#: (key, action, description, show) — the single source of truth for
#: app-wide bindings: it builds ``BINDINGS``, the keybar (``show``) and the
#: help overlay. tab/shift+tab are real bindings but stay out of the
#: keybar: panel cycling is implied, and the bar already lists every
#: panel's own keys.
APP_KEYS: tuple[tuple[str, str, str, bool], ...] = (
    ("tab", "focus_next", "next panel", False),
    ("shift+tab", "focus_previous", "previous panel", False),
    ("R", "recheck", "re-check", True),
    ("x", "dismiss_log", "hide log", True),
    ("question_mark", "show_help", "help", True),
    ("q", "quit", "quit", True),
    # Listed only while the filter bar is open (see `check_action`).
    ("escape", "close_filter", "close filter", True),
)


class LogLine(Message):
    """One line of job output, posted from a service thread."""

    def __init__(self, line: str) -> None:
        super().__init__()
        self.line = line


class JobDone(Message):
    """A minikube job finished; payload is the service's completion dict."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        self.payload = payload


def render_status(
    system: dict[str, Any], cluster: dict[str, Any], busy: str = ""
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

    if busy:
        out.append("  │ ", style="dim")
        out.append(f"… {busy}", style="yellow")

    pkg_mgr = system.get("package_manager_label")
    elevation = system.get("elevation")
    if pkg_mgr or elevation:
        out.append("  │ ", style="dim")
        # `elevation` reads "pkexec (graphical polkit prompt)" — keep the verb.
        elevation_verb = str(elevation).split("(")[0].strip() if elevation else ""
        out.append(
            f"{pkg_mgr} + {elevation_verb}" if elevation_verb else str(pkg_mgr),
            style="dim",
        )
    return out


def render_keybar(app: "KubbyApp") -> Text:
    """Quoted keys + labels for every *shown* binding in play right now.

    Derived from ``active_bindings`` rather than a hand-kept table, so the
    bar always matches what a keypress would actually do — including
    grayed-out (``enabled=False``) entries while a job is running.
    """
    out = Text()
    first = True
    for active in app.active_bindings.values():
        binding = active.binding
        if not binding.show:
            continue
        if not first:
            out.append("   ")
        first = False
        key = app.get_key_display(binding)
        out.append(f'"{key}"', style="bold cyan" if active.enabled else "dim")
        if binding.description:
            out.append(f" {binding.description}", style="dim")
    return out


class KubbyApp(App[None]):
    """The kubby TUI."""

    CSS_PATH = "styles.tcss"
    TITLE = "kubby"
    BINDINGS = [
        Binding(key, action, description, show=show)
        for key, action, description, show in APP_KEYS
    ]

    def __init__(self, service: KubbyService | None = None) -> None:
        super().__init__()
        # Tests inject a fake; everything the shell touches goes through it.
        self.service = service if service is not None else KubbyService()
        # Streaming callbacks arrive on job threads — they must only ever
        # touch the UI through thread-safe plumbing.
        self.service.on_log = self._post_log_line
        self.service.on_job_done = self._post_job_done

        self.system: dict[str, Any] = {}
        self.tools: list[dict[str, Any]] = []
        self.cluster: dict[str, Any] = {}
        self.images: list[dict[str, Any]] = []

        #: True while a minikube job or an install is streaming.
        self.busy = False
        self.busy_label = ""
        #: Client-side image filter (``/``), kept across refreshes.
        self.image_filter = ""
        self.filter_open = False

    # ----- compose / lifecycle -----------------------------------------

    @property
    def is_busy(self) -> bool:
        """What the panels' ``check_action`` consults before enabling keys."""
        return self.busy

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield MinikubePanel(id="minikube")
                yield ToolsPanel(id="tools")
                yield ImagesPanel(id="images")
            yield ClusterPanel(id="cluster")
        yield LogPanel()
        yield Input(
            placeholder="filter images — enter applies, esc clears",
            id="filter-bar",
        )
        yield Static("", id="keybar")

    def on_mount(self) -> None:
        # `Widget.focus()` only schedules the change (call_later), which
        # would leave the first keybar render built against the old focus —
        # set it synchronously instead.
        self.screen.set_focus(self.query_one(MinikubePanel))
        # Rule 5: idle means the log is hidden until a job starts.
        self.query_one(LogPanel).sync()
        self.query_one("#filter-bar", Input).display = False
        self.refresh_data()
        self._update_keybar()

    # ----- service sinks (any thread) -----------------------------------

    def _post_log_line(self, line: str) -> None:
        """`on_log` — called from job threads. `post_message` is thread-safe."""
        self.post_message(LogLine(line))

    def _post_job_done(self, payload: dict[str, Any]) -> None:
        """`on_job_done` — called from the job thread when minikube exits."""
        self.post_message(JobDone(payload))

    def on_log_line(self, message: LogLine) -> None:
        self.query_one(LogPanel).append(message.line)

    def on_job_done(self, message: JobDone) -> None:
        payload = message.payload
        self._finish_job(bool(payload.get("ok")))

    def on_panel_focused(self, message: PanelFocused) -> None:
        self._update_keybar()

    def on_panel_action(self, message: PanelAction) -> None:
        handler = {
            "start": self._start_cluster,
            "stop": self._stop_cluster,
            "delete": self._confirm_delete,
            "install": self._install_one,
            "install_all": self._install_all,
            "filter": self._open_filter,
        }.get(message.action)
        if handler is None:
            log.warning("unhandled panel action %r", message.action)
            return
        handler(message.payload)

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
        """Run *callback* on the UI thread from a worker (blocks until it ran)."""
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

        self._update_header()
        self.query_one(MinikubePanel).set_cluster(cluster)
        self.query_one(ToolsPanel).set_tools(tools)
        self.query_one(ClusterPanel).set_cluster(cluster)
        self._render_images()
        # Cluster state changed → start/stop/delete availability changed.
        self._update_keybar()

    def _update_header(self) -> None:
        self.query_one("#status", Static).update(
            render_status(self.system, self.cluster, self.busy_label)
        )

    def _update_keybar(self) -> None:
        try:
            bar = self.query_one("#keybar", Static)
        except NoMatches:
            # Focus can land while compose is still mounting widgets;
            # `on_mount` runs this again once everything is in place.
            return
        bar.update(render_keybar(self))

    # ----- job lifecycle -------------------------------------------------

    def _begin_job(self, label: str) -> None:
        """A job is starting: show the log, grey out the keys (rule 4)."""
        self.busy = True
        self.busy_label = label
        self.query_one(LogPanel).begin_job()
        self._update_header()
        self._update_keybar()

    def _finish_job(self, ok: bool) -> None:
        """Job over: un-hide/hide per rules 2/3/5, then re-read the world."""
        self.busy = False
        self.busy_label = ""
        self.query_one(LogPanel).end_job(ok)
        self._update_header()
        self._update_keybar()
        self.refresh_data()

    def _kick_job(self, action: str) -> None:
        """Run a minikube verb; the service refuses if a job is already live.

        The call itself is non-blocking (the service spawns the job thread
        and returns immediately), so it is safe on the UI thread.
        """
        runner = {
            "start": self.service.start_minikube,
            "stop": self.service.stop_minikube,
            "delete": self.service.delete_minikube,
        }[action]
        result = runner()
        if result.get("ok"):
            self._begin_job(action)
        else:
            self.notify(
                str(result.get("error") or "could not do that"),
                severity="error",
                title="kubby",
            )

    # ----- minikube actions ---------------------------------------------

    def _start_cluster(self, _payload: Any) -> None:
        """Preflight first, exactly like the GUI's start gate."""
        self._run_preflight()

    @work(thread=True, exclusive=True, group="preflight")
    def _run_preflight(self) -> None:
        prereq = self.service.get_minikube_prerequisites()
        self._call_on_ui(self._preflight_done, prereq)

    def _preflight_done(self, prereq: dict[str, Any]) -> None:
        if prereq.get("ok"):
            self._kick_job("start")
        else:
            self.push_screen(PrereqModal(prereq.get("issues") or []))

    def _stop_cluster(self, _payload: Any) -> None:
        self._kick_job("stop")

    def _confirm_delete(self, _payload: Any) -> None:
        self.push_screen(
            ConfirmModal(
                title="delete cluster",
                message=(
                    "Delete the minikube cluster?\n"
                    "Everything running in it is lost."
                ),
                confirm_label="delete",
            ),
            self._delete_confirmed,
        )

    def _delete_confirmed(self, accepted: bool | None) -> None:
        if accepted:
            self._kick_job("delete")

    # ----- installs (worker: they block for minutes) ---------------------

    def _install_one(self, key: Any) -> None:
        if key:
            self._run_install(str(key))

    def _install_all(self, _payload: Any) -> None:
        self._run_install(None)

    @work(thread=True, exclusive=True, group="install")
    def _run_install(self, key: str | None) -> None:
        # Busy + log must be up before the first byte streams; `_call_on_ui`
        # blocks until the UI thread has run it, so the order is guaranteed.
        self._call_on_ui(self._begin_job, f"install {key}" if key else "install all")
        if key:
            result = self.service.install_tool(key)
        else:
            result = self.service.install_all()
        self._call_on_ui(self._finish_job, bool(result.get("ok", False)))

    # ----- image filter --------------------------------------------------

    def _open_filter(self, _payload: Any) -> None:
        bar = self.query_one("#filter-bar", Input)
        bar.value = self.image_filter
        bar.display = True
        self.filter_open = True
        self.screen.set_focus(bar)
        self._update_keybar()

    def action_close_filter(self) -> None:
        """esc — drop the filter and go back to the full image list."""
        if not self.filter_open:
            return
        self.image_filter = ""
        bar = self.query_one("#filter-bar", Input)
        bar.value = ""  # posts Input.Changed, which re-renders below
        self._close_filter()

    def _close_filter(self) -> None:
        """Leave the filter bar (enter keeps the current query)."""
        bar = self.query_one("#filter-bar", Input)
        bar.display = False
        self.filter_open = False
        self._render_images()
        self.screen.set_focus(self.query_one(ImagesPanel))
        self._update_keybar()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter-bar":
            self.image_filter = event.value
            self._render_images()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter-bar":
            self._close_filter()

    def _render_images(self) -> None:
        self.query_one(ImagesPanel).set_images(self.images, self.image_filter)

    # ----- actions -------------------------------------------------------

    def action_recheck(self) -> None:
        self.refresh_data()

    def action_show_help(self) -> None:
        self.push_screen(HelpScreen(self.help_sections()))

    def action_dismiss_log(self) -> None:
        self.query_one(LogPanel).dismiss()
        self._update_keybar()

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        if action == "dismiss_log":
            try:
                return self.query_one(LogPanel).display
            except NoMatches:
                return False
        if action == "close_filter":
            # Hidden when idle so esc never gets swallowed for nothing.
            return self.filter_open
        return super().check_action(action, parameters)

    # ----- help ----------------------------------------------------------

    def help_sections(self) -> list[tuple[str, list[tuple[str, str]]]]:
        """Sections for the overlay: globals, then each panel's own keys."""
        images_rows = self._panel_rows(ImagesPanel) + [
            ("enter", "apply filter and close"),
            ("esc", "clear filter"),
        ]
        return [
            (
                "global",
                [
                    (self.get_key_display(Binding(key, action, description)), description)
                    for key, action, description, _show in APP_KEYS
                ],
            ),
            ("minikube panel", self._panel_rows(MinikubePanel)),
            ("tools panel", self._panel_rows(ToolsPanel)),
            ("images panel", images_rows),
            (
                "namespaces panel",
                [
                    ("up/down", "move"),
                    ("enter/space", "expand / collapse"),
                ],
            ),
        ]

    def _panel_rows(self, panel_cls: type) -> list[tuple[str, str]]:
        return [
            (self.get_key_display(binding), binding.description or binding.action)
            for binding in panel_cls.BINDINGS
            if binding.show
        ]
