"""kubby TUI application shell.

Layout (top → bottom):

    status line
    ┌ sidebar (minikube / machine / images) ┐┌ cluster ┐
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
from collections.abc import Callable
from typing import Any

from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.screen import Screen
from textual.widgets import Input, Static

from kubby import settings as settings_mod
from kubby.service import KubbyService
from kubby.tui.panels import (
    ClusterPanel,
    ImagesPanel,
    LogPanel,
    MinikubePanel,
    PanelAction,
    PanelFocused,
    MachinePanel,
)
from kubby.tui.popups import ConfirmModal, HelpScreen, PrereqModal, SettingsScreen

log = logging.getLogger("kubby")
#: (key, action, description, show) — the single source of truth for
#: app-wide bindings: it builds ``BINDINGS``, the keybar (``show``) and the
#: help overlay.
#:
#: The number keys go straight to a panel. They are hidden from the keybar on
#: purpose: four more entries would push the focused panel's own keys off a
#: 100-column line, and each panel already shows its number in its own title.
#: They stay in the help overlay, which is the documented place to look up
#: keys.
#:
#: Tab is deliberately unmapped. Cycling is a poor way to cross the layout,
#: and an unmapped tab is available to whatever wants it next.
APP_KEYS: tuple[tuple[str, str, str, bool], ...] = (
    ("1", "focus_panel('#minikube')", "minikube panel", False),
    ("2", "focus_panel('#machine')", "machine panel", False),
    ("3", "focus_panel('#images')", "images panel", False),
    ("4", "focus_panel('#cluster')", "namespaces panel", False),
    ("R", "recheck", "re-check", True),
    ("x", "dismiss_log", "hide log", True),
    ("question_mark", "show_help", "help", True),
    ("q", "quit", "quit", True),
    # Listed only while the filter bar is open (see `check_action`).
    ("escape", "close_filter", "close filter", True),
)


#: Textual's Screen ships `tab`/`shift+tab` for focus cycling, which is why
#: unbinding them on the App is not enough on its own. Everything else Screen
#: binds is kept — in particular `ctrl+c` → `copy_text`, which shadows the
#: App's own `ctrl+c` → `help_quit` and silently does nothing in kubby (no
#: text is ever selected, so `copy_text` always raises `SkipAction`).
#:
#: The kept bindings are copied from `Screen.BINDINGS` rather than written
#: out here, so an upstream change to those defaults is inherited instead of
#: silently diverging from the version of Textual we happen to pin.
_SCREEN_BINDINGS_WITHOUT_TAB = [
    binding for binding in Screen.BINDINGS if binding.key not in ("tab", "shift+tab")
]


class PanelScreen(Screen[None], inherit_bindings=False):
    """The main screen, with focus cycling unbound.

    Panel focus moves by number, so the cycling bindings are removed rather
    than left to compete with whatever wants `tab` next. `inherit_bindings
    =False` is what actually drops them: a subclass's BINDINGS otherwise
    *merge* with the base class's, so listing nothing would keep Screen's.
    """

    BINDINGS = _SCREEN_BINDINGS_WITHOUT_TAB


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


#: The two keys pinned to the right edge of the keybar.
#:
#: These are the always-available, most-reached-for keys, and they must not
#: move when the focused panel's own key count changes. Everything else —
#: the panel's keys *and* the app-wide actions like ``R`` re-check and ``x``
#: hide log — stays on the left, because that is where the things you press
#: to *do* something live.
#:
#: The trade-off is deliberate: ``R`` and ``x`` therefore slide left and
#: right with the panel keys. An earlier version pinned every app-wide key,
#: which stopped ``R`` moving but left it sitting among the chrome, reading
#: like one of the two quit keys. Only these two are worth a fixed position.
PINNED_KEYS: frozenset[str] = frozenset({"question_mark", "q"})


def _is_pinned(binding: Any) -> bool:
    """True for the bindings that belong in the pinned right-hand half.

    Marked with an ``id`` when ``BINDINGS`` is built rather than inferred
    from the key: a panel that later binds ``q`` would otherwise be
    misfiled into the pinned half and jump around with it.
    """
    return str(getattr(binding, "id", "") or "").startswith("pinned:")


def render_keybar(app: "KubbyApp") -> Text:
    """Left half: the focused panel's keys, then the app-wide actions."""
    out = Text()
    first = True
    for active in app.active_bindings.values():
        binding = active.binding
        if not binding.show or _is_pinned(binding):
            continue
        if not first:
            out.append("   ")
        first = False
        key = app.get_key_display(binding)
        out.append(f'"{key}"', style="bold cyan" if active.enabled else "dim")
        if binding.description:
            out.append(f" {binding.description}", style="dim")
    return out


def render_globals(app: "KubbyApp") -> Text:
    """Right half: the pinned `?` help and `q` quit keys.

    These two used to slide left and right as the focused panel's own keys
    came and went, because they shared a line with them. Their own
    right-aligned region means they sit in the same place on every panel.
    """
    out = Text()
    first = True
    for active in app.active_bindings.values():
        binding = active.binding
        if not binding.show or not _is_pinned(binding):
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
        # The `pinned:` id is what tells the keybar renderer which half of
        # the bar this binding belongs to — see `_is_pinned`.
        Binding(
            key,
            action,
            description,
            show=show,
            id=f"pinned:{key}" if key in PINNED_KEYS else None,
        )
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

        #: True while a minikube job is streaming.
        self.busy = False
        self.busy_label = ""
        #: Client-side image filter (``/``), kept across refreshes.
        self.image_filter = ""
        self.filter_open = False

    # ----- compose / lifecycle -----------------------------------------

    def get_default_screen(self) -> PanelScreen:
        """Create the main screen without Tab or Shift+Tab focus cycling."""
        return PanelScreen(id="_default")

    @property
    def is_busy(self) -> bool:
        """What the panels' ``check_action`` consults before enabling keys."""
        return self.busy

    def compose(self) -> ComposeResult:
        yield Static("", id="status")
        with Horizontal(id="body"):
            with Vertical(id="sidebar"):
                yield MinikubePanel(id="minikube")
                yield MachinePanel(id="machine")
                yield ImagesPanel(id="images")
            yield ClusterPanel(id="cluster")
        yield LogPanel()
        yield Input(
            placeholder="filter images — enter applies, esc clears",
            id="filter-bar",
        )
        # Two regions in one row: the focused panel's keys and the app-wide
        # actions on the left, `?` and `q` pinned to the right edge. One bar
        # rendered both in a single line made those two slide left and right
        # every time the panel's own key count changed.
        with Horizontal(id="keybar-row"):
            yield Static("", id="keybar")
            yield Static("", id="keybar-globals")

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
            "filter": self._open_filter,
            "settings": self._open_settings,
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
        # The graph reuses the inventory above rather than re-fetching it, so
        # this costs four extra calls, not seven.
        graph = self.service.get_cluster_graph(cluster)
        cluster = {**cluster, "graph": graph}
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
        self.query_one(MachinePanel).set_cluster(cluster)
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
            panel_bar = self.query_one("#keybar", Static)
            globals_bar = self.query_one("#keybar-globals", Static)
        except NoMatches:
            # Focus can land while compose is still mounting widgets;
            # `on_mount` runs this again once everything is in place.
            return
        panel_bar.update(render_keybar(self))
        globals_bar.update(render_globals(self))

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
            self.push_screen(
                PrereqModal(prereq.get("issues") or []), self._prereq_closed
            )

    def _prereq_closed(self, choice: str | None) -> None:
        if choice == "settings":
            self._open_settings(None)

    # ----- settings ------------------------------------------------------
    # Same shape as every other blocking call: read/write in a worker,
    # results back via `_call_on_ui`.

    def _open_settings(self, _payload: Any) -> None:
        self._load_settings()

    @work(thread=True, exclusive=True, group="settings")
    def _load_settings(self) -> None:
        settings = self.service.get_minikube_settings()
        self._call_on_ui(self._show_settings, settings)

    def _show_settings(self, settings: dict[str, Any]) -> None:
        self.push_screen(
            SettingsScreen(
                settings.get("minikube") or {},
                str(settings_mod.CONFIG_FILE),
                save=self._save_settings,
            )
        )

    def _save_settings(
        self,
        payload: dict[str, Any],
        done: Callable[[dict[str, Any]], None],
    ) -> None:
        """SettingsScreen's save hook — writes in a worker, calls back here."""
        self._save_settings_worker(payload, done)

    @work(thread=True, exclusive=True, group="settings")
    def _save_settings_worker(
        self,
        payload: dict[str, Any],
        done: Callable[[dict[str, Any]], None],
    ) -> None:
        result = self.service.save_minikube_settings(payload)
        self._call_on_ui(done, result)

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

    def _focus_panel(self, selector: str) -> None:
        """Jump straight to a panel by id.

        `set_focus` rather than `Widget.focus()` for the same reason
        `on_mount` uses it: focus() only schedules the change, so the
        keybar would render against the panel the user just left.
        """
        self.screen.set_focus(self.query_one(selector))

    def action_focus_panel(self, selector: str) -> None:
        """Focus the panel selected by a number-key binding.

        ``selector`` is a Textual CSS selector such as ``#images``.
        ``NoMatches`` propagates if it matches no widget.
        """
        self._focus_panel(selector)

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
        # Nav keys are `show=False` (the keybar has ~27 characters of
        # headroom, and four nav keys per panel would overflow it), so they
        # are written out here instead — the overlay is the documented place
        # to look up keys.
        #
        # j/k are listed one per row and first, because they are the movement
        # keys of record; the arrows follow as a fallback note. Presenting
        # them as one combined "j/k or up/down" row would make them look like
        # equals, which is not how they are meant to be read.
        move_rows = [
            ("j", "down"),
            ("k", "up"),
            ("up/down", "also move"),
        ]
        list_nav = move_rows + [
            ("h/l", "not applicable — one column"),
        ]
        images_rows = self._panel_rows(ImagesPanel) + [
            ("enter", "apply filter and close"),
            ("esc", "clear filter"),
        ] + list_nav
        # The machine panel is a readout with no keys of its own. j/k scroll
        # it, but the help says so rather than listing an empty section,
        # which would read as a panel that was never finished. Written as two
        # rows rather than one "j/k" row, because j and k are opposites and
        # a combined row reads as equals.
        machine_rows: list[tuple[str, str]] = [
            ("j", "scroll down"),
            ("k", "scroll up"),
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
            ("machine panel", machine_rows),
            ("images panel", images_rows),
            # The cluster panel is two views behind one pair of keys, so the
            # help says which is showing rather than listing two panels.
            (
                "cluster panel",
                [
                    ("g", "the graph (default)"),
                    ("t", "the namespace tree"),
                ]
                + move_rows
                + [
                    ("h", "collapse, or out to the parent (tree)"),
                    ("l", "expand, or in to the first pod (tree)"),
                    ("enter/space", "expand / collapse (tree)"),
                ],
            ),
        ]

    def _panel_rows(self, panel_cls: type) -> list[tuple[str, str]]:
        return [
            (self.get_key_display(binding), binding.description or binding.action)
            for binding in panel_cls.BINDINGS
            if binding.show
        ]
