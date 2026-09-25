"""kubby entry point — CLI + Textual TUI launcher.

Public surface:
- `main(argv=None)`: parses CLI args, launches the TUI (or runs ``--check``).

History: this module used to own the browser window (``KubbyAPI`` plus
the HTML/JS frontend that lived in ``kubby/ui/``). The TUI replaced both —
all domain behavior lives in `kubby.service.KubbyService`, and
`kubby.tui.KubbyApp` talks to that service directly, so nothing here
touches a browser anymore.
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import sys
from pathlib import Path

from kubby.service import KubbyService

if getattr(sys, "frozen", False):
    # PyInstaller onefile: __file__ resolves to the MEIPASS root, not the
    # package directory. kubby.spec keeps `kubby/tui/styles.tcss` at
    # `kubby/tui/` inside the bundle so the same relative layout holds.
    PKG_DIR = Path(sys._MEIPASS) / "kubby"
else:
    PKG_DIR = Path(__file__).resolve().parent


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kubby",
        description="Terminal UI for managing local Kubernetes cluster resources.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help=(
            "Print which managed tools are present (and where to get any "
            "that aren't), then exit (no TUI)."
        ),
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Enable Textual devtools (requires `textual-dev` plus a running "
            "`textual dev` server to stream to)."
        ),
    )
    return parser.parse_args(argv)


def _print_check_report() -> int:
    """Print the self-check report.

    Sourced from `KubbyService` so the output shape stays identical to the
    pre-TUI version (it was previously built from `platform` +
    `tools_mod.TOOLS` directly).
    """
    service = KubbyService()
    info = service.system_info()
    print("kubby self-check")
    print("================")
    print(f"project:      {PKG_DIR.parent}")
    print(f"python:       {info['python']}")
    print(f"platform:     {info['platform']} {info['release']}")
    print(f"package mgr:  {info['package_manager_label']}")
    # Host context, not a kubby capability: kubby installs nothing and never
    # elevates itself, but minikube may still ask for a password, so it is
    # worth knowing which prompt to expect.
    print(f"root prompt:  {info['elevation']}")
    print()
    print("Managed tools")
    print("-------------")
    statuses = service.get_status()
    width = max(len(s["label"]) for s in statuses) + 1
    for s in statuses:
        if s["installed"]:
            status = f"installed ({s['version'] or 'unknown'}) at {s['path']}"
        else:
            # The registry's website is the whole answer now — kubby will not
            # install it, so say where to get it.
            status = f"NOT FOUND — get it from {s.get('website') or 'upstream'}"
        print(f"  {s['label']:<{width}} {status}")
    return 0


def _enable_devtools_feature() -> None:
    """Turn on Textual's ``devtools`` feature before the App is built.

    Textual reads feature flags from ``$TEXTUAL`` when the App is
    constructed — this is the TUI equivalent of the GUI's ``--debug``
    (WebKit DevTools). With ``textual-dev`` installed the app streams
    its log to a running ``textual dev`` server; without it the flag is
    a no-op, so say that up front instead of silently doing nothing.
    """
    flags = {flag.strip() for flag in os.environ.get("TEXTUAL", "").split(",")}
    flags.discard("")
    flags.add("devtools")
    os.environ["TEXTUAL"] = ",".join(sorted(flags))
    if importlib.util.find_spec("textual_dev") is None:
        print(
            "note: --debug needs textual-dev (`uv pip install textual-dev`)\n"
            "      and a running `textual dev` server to stream to",
            file=sys.stderr,
        )


def _run_tui(debug: bool) -> int:
    """Launch the TUI and translate startup failures into a clean message."""
    # A TUI needs a real terminal on both ends. Without this guard Textual
    # happily renders into a pipe and then waits for keys nobody can send —
    # `kubby > out.txt` would hang forever.
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print(
            "error: kubby needs an interactive terminal — run it from your "
            "shell, not through a pipe or with output redirected "
            "(`kubby --check` needs none).",
            file=sys.stderr,
        )
        return 1

    if debug:
        _enable_devtools_feature()

    # Imported here, not at module top: `kubby --check` (and the frozen
    # smoke test) should never pay for Textual's import.
    from kubby.tui import KubbyApp

    try:
        KubbyApp().run()
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print(
            f"error: failed to start kubby: {e}\n"
            "(kubby renders in a terminal — is this a real one?)",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = _parse_args(raw)

    if args.check:
        return _print_check_report()

    return _run_tui(debug=args.debug)


if __name__ == "__main__":
    raise SystemExit(main())
