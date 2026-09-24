"""Modal screens: help (Phase 2), then settings / preflight / confirm."""

from __future__ import annotations

from collections.abc import Sequence

from rich.text import Text
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Static


class HelpScreen(ModalScreen[None]):
    """Keyboard reference overlay.

    Modal, so app-level bindings (``q`` quit in particular) are suspended
    while it is open — ``q`` and ``escape`` both dismiss it.
    """

    BINDINGS = [
        Binding("escape", "dismiss", "close"),
        Binding("q", "dismiss", "close"),
        Binding("question_mark", "dismiss", "close"),
    ]

    def __init__(self, rows: Sequence[tuple[str, str]]) -> None:
        super().__init__()
        self.rows = list(rows)

    def compose(self) -> ComposeResult:
        with Container(id="help-box"):
            yield Static(self._render_help(), id="help-body")

    @staticmethod
    def _render_rows(title: str, rows: Sequence[tuple[str, str]]) -> Text:
        out = Text()
        out.append(f"{title}\n", style="bold underline")
        if not rows:
            out.append("(nothing yet)\n", style="dim")
            return out
        width = max(len(f'"{keys}"') for keys, _ in rows)
        for keys, label in rows:
            out.append(f'  "{keys}"', style="bold cyan")
            out.append(" " * (width - len(f'"{keys}"') + 2))
            out.append(f"{label}\n")
        return out

    def _render_help(self) -> Text:
        out = Text()
        out.append("kubby — keys\n\n", style="bold")
        out.append(self._render_rows("global", self.rows))
        out.append("\n")
        out.append('press "q" or "esc" to close', style="dim")
        return out

    def action_dismiss(self) -> None:
        self.dismiss(None)
