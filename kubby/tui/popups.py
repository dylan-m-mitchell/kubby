"""Modal screens: settings, help, confirm, preflight.

Every modal here is a ``ModalScreen``, which stops Textual's non-priority
binding chain — so app-level ``q`` (quit) is suspended while one is open.
Each modal therefore binds ``q``/``escape`` to *its own* close action.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from typing import Any

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal
from textual.screen import ModalScreen
from textual.widgets import Checkbox, Input, Select, Static

Rows = Sequence[tuple[str, str]]
Sections = Sequence[tuple[str, Rows]]

#: Keys the confirm modal already binds — never hijack one of these for the
#: action label ("n" for a label like "no thanks" must stay *cancel*).
_TAKEN_KEYS = frozenset({"y", "n", "enter", "escape", "q"})

#: Validation, lifted verbatim from ``kubby/ui/app.js`` so both UIs accept
#: (and reject) exactly the same settings.json values. Messages are the
#: GUI's strings too — same form, same complaints.
CPUS_RE = re.compile(r"^\d+(\.\d+)?$")
MEMORY_RE = re.compile(r"^\d+(\.\d+)?(m|mi|mb|g|gi|gb)?$", re.IGNORECASE)

#: (stored value, label) — mirrors the GUI's <select> options for `driver`.
DRIVER_OPTIONS: tuple[tuple[str, str], ...] = (
    ("", "automatic (let minikube pick)"),
    ("docker", "docker"),
    ("podman", "podman"),
    ("kvm2", "kvm2 (Linux, requires QEMU)"),
    ("none", "none (Linux only)"),
)

#: (stored value, label) — mirrors the GUI's addon checkboxes.
ADDON_OPTIONS: tuple[tuple[str, str], ...] = (
    ("default", "default (storage, dashboard)"),
    ("ingress", "ingress (NGINX controller)"),
    ("metrics-server", "metrics-server"),
)

#: ``SettingsScreen``'s save hook: hand it the settings dict and a callback
#: that will receive the service's ``{"ok": ...}`` result on the UI thread.
#: The app implements it as a worker so the disk write stays off the UI.
SaveFn = Callable[[dict[str, Any], Callable[[dict[str, Any]], None]], None]


class SettingsScreen(ModalScreen[None]):
    """The GUI's settings modal, ported field for field.

    Same validation regexes and error strings as ``kubby/ui/app.js``, so a
    ``settings.json`` written by either UI is accepted by the other.

    ``esc`` backs out without writing; ``ctrl+s``/``enter`` save through the
    injected ``save(payload, done)`` callable — the app runs the write in a
    worker and calls ``done`` back on the UI thread: ``{"ok": True}``
    dismisses, anything else surfaces the error while staying open.
    """

    BINDINGS = [
        Binding("escape", "cancel", "cancel"),
        Binding("ctrl+s", "save", "save"),
        Binding("enter", "save", "save"),
    ]

    def __init__(
        self,
        settings: dict[str, Any],
        settings_path: str,
        save: SaveFn,
    ) -> None:
        super().__init__()
        self._base = dict(settings)
        self._path = settings_path
        self._save_request = save
        self._saving = False

    # ----- form ---------------------------------------------------------

    @staticmethod
    def _row(label: str, *fields: Any) -> Horizontal:
        return Horizontal(
            Static(label, classes="field-label"),
            *fields,
            classes="settings-row",
        )

    def compose(self) -> ComposeResult:
        base = self._base
        addons = set(base.get("addons") or [])
        with Container(id="settings-box"):
            yield Static("minikube settings", id="settings-title")
            yield Static(f"saved to {self._path}", id="settings-path")
            yield self._row(
                "driver",
                Select(
                    [(label, value) for value, label in DRIVER_OPTIONS],
                    value=str(base.get("driver") or ""),
                    allow_blank=False,
                    id="set-driver",
                    compact=True,
                ),
            )
            yield self._row(
                "cpus",
                Input(
                    str(base.get("cpus") or ""),
                    placeholder="2",
                    id="set-cpus",
                    compact=True,
                ),
            )
            yield self._row(
                "memory",
                Input(
                    str(base.get("memory") or ""),
                    placeholder="2g",
                    id="set-memory",
                    compact=True,
                ),
            )
            yield self._row(
                "kubernetes",
                Input(
                    str(base.get("kubernetes_version") or ""),
                    placeholder="latest stable",
                    id="set-k8s",
                    compact=True,
                ),
            )
            yield self._row(
                "",
                Checkbox(
                    "run as rootless (no sudo for the VM)",
                    value=bool(base.get("rootless")),
                    id="set-rootless",
                    compact=True,
                ),
            )
            yield Static("addons", id="settings-addons", classes="field-label")
            for key, label in ADDON_OPTIONS:
                yield self._row(
                    "",
                    Checkbox(
                        label,
                        value=key in addons,
                        id=f"set-addon-{key}",
                        compact=True,
                    ),
                )
            yield Static("", id="settings-error")
            yield Static(self._hints(), id="settings-hints")

    @staticmethod
    def _hints() -> Text:
        return (
            Text().append('"ctrl+s"/"enter"', style="bold cyan")
            .append(" save   ", style="dim")
            .append('"esc"', style="bold cyan")
            .append(" cancel", style="dim")
        )

    def on_mount(self) -> None:
        self.set_focus(self.query_one("#set-driver", Select))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in any text field saves, like submitting a web form."""
        event.stop()
        self.action_save()

    # ----- save ---------------------------------------------------------

    def action_save(self) -> None:
        if self._saving:
            return
        cpus = self.query_one("#set-cpus", Input).value.strip()
        memory = self.query_one("#set-memory", Input).value.strip()
        if cpus and not CPUS_RE.match(cpus):
            self._fail("CPUs must be a positive number like '2' or '2.5' (or empty)")
            return
        if memory and not MEMORY_RE.match(memory):
            self._fail('Memory must look like "2g", "4096mb", "2048Mi" (or empty)')
            return

        payload = {
            "minikube": {
                **self._base,
                "driver": str(self.query_one("#set-driver", Select).value or ""),
                "cpus": cpus,
                "memory": memory,
                "kubernetes_version": self.query_one("#set-k8s", Input).value.strip(),
                "rootless": bool(self.query_one("#set-rootless", Checkbox).value),
                "addons": [
                    key
                    for key, _ in ADDON_OPTIONS
                    if self.query_one(f"#set-addon-{key}", Checkbox).value
                ],
            }
        }
        self._saving = True
        self.query_one("#settings-hints", Static).update("saving…")
        self._save_request(payload, self._save_done)

    def _save_done(self, result: dict[str, Any]) -> None:
        if not self.is_attached:
            return  # closed while the write was in flight
        self._saving = False
        if result.get("ok"):
            self.app.notify("Settings saved", severity="information", title="kubby")
            self.dismiss(None)
        else:
            self.query_one("#settings-hints", Static).update(self._hints())
            self._fail(str(result.get("error") or "could not save settings"))

    def _fail(self, message: str) -> None:
        self.query_one("#settings-error", Static).update(Text(message, style="red"))

    def action_cancel(self) -> None:
        self.dismiss(None)


class HelpScreen(ModalScreen[None]):
    """Keyboard reference overlay; ``q``, ``esc`` and ``?`` dismiss it."""

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close"),
        Binding("question_mark", "dismiss", "close"),
    ]

    def __init__(self, sections: Sections) -> None:
        super().__init__()
        self.sections = [(title, list(rows)) for title, rows in sections]

    def compose(self) -> ComposeResult:
        with Container(id="help-box"):
            yield Static(self._render_help(), id="help-body")

    @staticmethod
    def _render_rows(rows: Rows) -> Text:
        out = Text()
        if not rows:
            out.append("  (nothing yet)\n", style="dim")
            return out
        width = max(len(f'"{keys}"') for keys, _ in rows)
        for keys, label in rows:
            quoted = f'"{keys}"'
            out.append(f"  {quoted}", style="bold cyan")
            out.append(" " * (width - len(quoted) + 2))
            out.append(f"{label}\n")
        return out

    def _render_help(self) -> Text:
        out = Text()
        out.append("kubby — keys\n\n", style="bold")
        for title, rows in self.sections:
            out.append(f"{title}\n", style="bold underline")
            out.append(self._render_rows(rows))
            out.append("\n")
        out.append('press "q" or "esc" to close', style="dim")
        return out

    def action_dismiss(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[bool]):
    """``y`` accepts, ``n``/``esc``/``q`` backs out; dismisses with a bool."""

    BINDINGS = [
        Binding("y", "accept", "confirm"),
        Binding("enter", "accept", "confirm"),
        Binding("n", "cancel", "cancel"),
        Binding("escape", "cancel", "cancel"),
        Binding("q", "cancel", "cancel"),
    ]

    def __init__(self, *, title: str, message: str, confirm_label: str) -> None:
        super().__init__()
        self._title = title
        self._message = message
        self._confirm_label = confirm_label
        # The hint quotes the label's first letter (`"d" yes` for delete);
        # accept it alongside y/enter unless a bound key already owns it —
        # a label starting with "n" must never shadow *cancel*.
        key = confirm_label[:1].strip().lower()
        self._accept_key = key if key and key not in _TAKEN_KEYS else None

    def compose(self) -> ComposeResult:
        with Container(id="confirm-box"):
            yield Static(self._title, id="confirm-title")
            yield Static(self._message, id="confirm-message")
            yield Static(
                Text()
                .append(f'"{self._confirm_label[0]}"', style="bold cyan")
                .append(" yes   ", style="dim")
                .append('"n"', style="bold cyan")
                .append(" no", style="dim"),
                id="confirm-hints",
            )

    def on_key(self, event: events.Key) -> None:
        if self._accept_key and event.character == self._accept_key:
            event.stop()
            self.action_accept()

    def action_accept(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


class PrereqModal(ModalScreen[str | None]):
    """Preflight findings that block ``minikube start`` (rules from the GUI:
    settings not writable, no container driver, no local images path …).

    Dismisses with ``"settings"`` when ``o`` asks the app to open the
    settings screen, ``None`` otherwise.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close"),
        Binding("o", "open_settings", "settings"),
    ]

    def __init__(self, issues: Sequence[str]) -> None:
        super().__init__()
        self.issues = [str(issue) for issue in issues]

    def compose(self) -> ComposeResult:
        with Container(id="prereq-box"):
            yield Static("can't start minikube yet", id="prereq-title")
            yield Static(self._render_issues(), id="prereq-issues")
            yield Static(
                Text()
                .append('"o"', style="bold cyan")
                .append(" open settings   ", style="dim")
                .append('"esc"', style="bold cyan")
                .append(" close", style="dim"),
                id="prereq-hints",
            )

    def _render_issues(self) -> Text:
        out = Text()
        for issue in self.issues:
            out.append("✗ ", style="red")
            out.append(f"{issue}\n")
        if not self.issues:
            out.append("(no details reported)", style="dim")
        return out

    def action_dismiss(self) -> None:
        self.dismiss(None)

    def action_open_settings(self) -> None:
        self.dismiss("settings")
