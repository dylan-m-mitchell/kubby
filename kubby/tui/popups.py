"""Modal screens: help, confirm, preflight (settings lands in Phase 4).

Every modal here is a ``ModalScreen``, which stops Textual's non-priority
binding chain — so app-level ``q`` (quit) is suspended while one is open.
Each modal therefore binds ``q``/``escape`` to *its own* close action.
"""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Static

Rows = Sequence[tuple[str, str]]
Sections = Sequence[tuple[str, Rows]]

#: Keys the confirm modal already binds — never hijack one of these for the
#: action label ("n" for a label like "no thanks" must stay *cancel*).
_TAKEN_KEYS = frozenset({"y", "n", "enter", "escape", "q"})


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


class PrereqModal(ModalScreen[None]):
    """Preflight findings that block ``minikube start`` (rules from the GUI:
    settings not writable, no container driver, no local images path …)."""

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close"),
    ]

    def __init__(self, issues: Sequence[str]) -> None:
        super().__init__()
        self.issues = [str(issue) for issue in issues]

    def compose(self) -> ComposeResult:
        with Container(id="prereq-box"):
            yield Static("can't start minikube yet", id="prereq-title")
            yield Static(self._render_issues(), id="prereq-issues")
            yield Static('press "esc" to close', id="prereq-hints")

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
