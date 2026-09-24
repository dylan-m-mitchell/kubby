"""Phase 5 gate: the entry point after the web GUI is gone.

`kubby --check` must keep printing the same report the pre-TUI binary
printed (it is the CI smoke test), and `--debug` must map onto Textual's
own feature flag instead of the deleted WebKit DevTools.
"""

from __future__ import annotations

import os
import sys

from kubby import app as app_mod


class TestCheckReport:
    async def test_check_prints_the_report_and_exits_zero(self, capsys):
        assert app_mod.main(["--check"]) == 0

        out = capsys.readouterr().out
        assert out.startswith("kubby self-check\n================\n")
        # Header fields the smoke test relies on.
        for field in ("project:", "python:", "platform:", "package mgr:", "elevation:"):
            assert field in out
        assert "Managed tools" in out
        # Every managed tool is listed with an installed/not-found status.
        for label in ("minikube", "helm", "podman", "kubectl"):
            assert label in out

    async def test_check_never_launches_the_tui(self, capsys, monkeypatch):
        # Belt and braces: `--check` must return before `_run_tui` runs.
        def explode(*_args, **_kwargs):
            raise AssertionError("_run_tui must not run for --check")

        monkeypatch.setattr(app_mod, "_run_tui", explode)
        assert app_mod.main(["--check"]) == 0
        assert "kubby self-check" in capsys.readouterr().out


class TestDebugFlag:
    async def test_debug_flag_parses(self):
        assert app_mod._parse_args(["--debug"]).debug is True
        assert app_mod._parse_args([]).debug is False

    async def test_debug_enables_the_devtools_feature(self, monkeypatch):
        # Textual reads $TEXTUAL when the App is constructed, so the flag
        # has to be in the environment *before* KubbyApp() is built.
        monkeypatch.setenv("TEXTUAL", "inline")
        app_mod._enable_devtools_feature()
        assert set(os.environ["TEXTUAL"].split(",")) == {"inline", "devtools"}

    async def test_debug_feature_keeps_preexisting_flags(self, monkeypatch):
        monkeypatch.delenv("TEXTUAL", raising=False)
        app_mod._enable_devtools_feature()
        assert os.environ["TEXTUAL"] == "devtools"


class TestTuiLaunch:
    async def test_needs_a_terminal(self, monkeypatch, capsys):
        # Without this guard Textual renders into a pipe and waits for keys
        # forever (`kubby > out.txt` would hang).
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(sys.stdout, "isatty", lambda: False)

        assert app_mod.main([]) == 1
        assert "interactive terminal" in capsys.readouterr().err

    async def test_debug_is_parsed_before_the_tui_starts(self, monkeypatch):
        # `--debug` only flips TEXTUAL's feature flags; the actual launch
        # (`_run_tui`) is covered by the smoke tests, not unit tests, since
        # it needs a real terminal.
        monkeypatch.delenv("TEXTUAL", raising=False)
        captured: dict[str, object] = {}

        def fake_run_tui(*, debug: bool) -> int:
            captured["debug"] = debug
            return 0

        monkeypatch.setattr(app_mod, "_run_tui", fake_run_tui)
        assert app_mod.main(["--debug"]) == 0
        assert captured["debug"] is True
