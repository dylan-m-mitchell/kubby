"""kubby entry point — opens a native window backed by the OS's webview.

Public surface:
- `main(argv=None)`: parses CLI args, opens the GUI (or runs --check).
- `KubbyAPI`: instance passed to pywebview as `js_api`; methods are exposed to
  the HTML/JS frontend as `window.pywebview.api.<method_name>`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from kubby.installer import detector, linux, tools as tools_mod

log = logging.getLogger("kubby")

PKG_DIR = Path(__file__).resolve().parent
UI_DIR = PKG_DIR / "ui"
INDEX_HTML = UI_DIR / "index.html"


class KubbyAPI:
    """Methods exposed to the JS frontend as `window.pywebview.api.<name>`.

    pywebview exposes any public method on this class. Private methods (those
    starting with `_`) are not exposed. Calls from JS return Promises.
    """

    def __init__(self) -> None:
        self._window: webview.Window | None = None

    def bind_window(self, window: webview.Window) -> None:
        self._window = window

    def _emit_log(self, line: str) -> None:
        """Push a log line to the frontend via `window.evaluate_js`."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.kubby && window.kubby.appendLog({json.dumps(line)})"
            )
        except Exception:
            log.exception("failed to push log line to window")

    # ----- public API methods (callable from JS) -----

    def system_info(self) -> dict[str, Any]:
        """Return host info so the UI can label package manager / elevation path."""
        pm_label = "(unknown)"
        try:
            _pm_key, pm_label = linux.detect_package_manager()
        except Exception as e:
            pm_label = f"unsupported ({e})"
        return {
            "platform": platform.system(),
            "release": platform.release(),
            "python": platform.python_version(),
            "package_manager_label": pm_label,
            "elevation": linux.describe_elevation_method(),
        }

    def get_status(self) -> list[dict[str, Any]]:
        """Return current install status of every managed tool."""
        statuses: list[dict[str, Any]] = []
        for key, tool in tools_mod.TOOLS.items():
            installed, version, path = detector.detect(tool)
            statuses.append(
                {
                    "key": key,
                    "label": tool.label,
                    "description": tool.description,
                    "website": tool.website,
                    "installed": installed,
                    "version": version,
                    "path": path,
                }
            )
        return statuses

    def get_cluster_info(self) -> dict[str, Any]:
        """Return information about the current Kubernetes cluster.

        Returns a dict with:
        - running (bool): whether a cluster is reachable
- error (str|None): error message if not running
- context (str|None): active kubectl context name
- version (str|None): server Kubernetes version
- nodes (list[dict]): list of {name, status, roles}
- namespaces (list[str]): namespace names
- pod_count (int): total pods across all namespaces
        """
        def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                args, capture_output=True, text=True, timeout=10,
            )

        # Check if kubectl is available
        if not shutil.which("kubectl"):
            return {"running": False, "error": "kubectl not found on PATH"}

        # Get current context
        ctx = _run(["kubectl", "config", "current-context"])
        if ctx.returncode != 0:
            return {"running": False, "error": ctx.stderr.strip() or "no current context"}
        context = ctx.stdout.strip()

        # Try to reach the cluster
        ver = _run(["kubectl", "version", "--client=false", "-o", "json"])
        if ver.returncode != 0:
            return {"running": False, "error": ver.stderr.strip() or "cannot reach cluster", "context": context}

        version = None
        try:
            ver_data = json.loads(ver.stdout)
            version = ver_data.get("serverVersion", {}).get("gitVersion", "")
        except (json.JSONDecodeError, KeyError):
            pass

        # Fetch nodes, namespaces, and pods in a single kubectl call to
        # minimise subprocess overhead (each call can take ~1-2s on minikube).
        nodes: list[dict[str, Any]] = []
        namespaces: list[str] = []
        pod_count = 0

        bulk = _run([
            "kubectl", "get", "nodes,namespaces,pods", "-A", "-o", "json",
        ])
        if bulk.returncode == 0:
            try:
                bulk_data = json.loads(bulk.stdout)
                for item in bulk_data.get("items", []):
                    kind = item.get("kind", "")
                    if kind == "Node":
                        name = item["metadata"]["name"]
                        roles: list[str] = []
                        for lbl in item["metadata"].get("labels", {}):
                            if lbl.startswith("node-role.kubernetes.io/"):
                                role = lbl.split("/", 1)[1]
                                if role:
                                    roles.append(role)
                        status = "Unknown"
                        for cond in item.get("status", {}).get("conditions", []):
                            if cond.get("type") == "Ready":
                                status = "Ready" if cond.get("status") == "True" else "NotReady"
                        nodes.append({"name": name, "status": status, "roles": roles})
                    elif kind == "Namespace":
                        namespaces.append(item["metadata"]["name"])
                    elif kind == "Pod":
                        pod_count += 1
            except (json.JSONDecodeError, KeyError):
                pass

        return {
            "running": True,
            "error": None,
            "context": context,
            "version": version,
            "nodes": nodes,
            "namespaces": namespaces,
            "pod_count": pod_count,
        }

    def install_tool(self, key: str) -> dict[str, Any]:
        """Install the given tool. Streams command output to the UI log.

        Returns a dict with `ok: bool`, optional `version`, optional `error`, and
        the full `log` joined as a string for convenience. Lines are also pushed
        live via `_emit_log`.
        """
        tool = tools_mod.TOOLS.get(key)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {key!r}"}

        log_lines: list[str] = []

        def emit(line: str) -> None:
            log_lines.append(line)
            self._emit_log(line)

        try:
            pm_key, pm_label = linux.detect_package_manager()
        except Exception as e:
            emit(f"! {e}")
            return {"ok": False, "log": "\n".join(log_lines), "error": str(e)}

        commands = tool.install_commands.get(pm_key)
        if not commands:
            msg = f"No install procedure defined for package manager: {pm_label}"
            emit(f"! {msg}")
            return {"ok": False, "log": "\n".join(log_lines), "error": msg}

        emit(f"Using package manager: {pm_label}")
        emit(f"Will run {len(commands)} command(s) with elevation via "
             f"{linux.describe_elevation_method()}.")

        for cmd in commands:
            emit(f"$ {' '.join(cmd)}")
            try:
                result = linux.run_elevated(cmd)
            except FileNotFoundError as e:
                msg = f"command not found: {e.filename}"
                emit(f"! {msg}")
                return {"ok": False, "log": "\n".join(log_lines), "error": msg}
            except subprocess.TimeoutExpired:
                emit("! command timed out")
                return {
                    "ok": False,
                    "log": "\n".join(log_lines),
                    "error": "install timed out",
                }
            if result.stdout.strip():
                emit(result.stdout.rstrip())
            if result.stderr.strip():
                emit(result.stderr.rstrip())
            if result.returncode != 0:
                emit(f"! command failed with exit code {result.returncode}")
                return {
                    "ok": False,
                    "log": "\n".join(log_lines),
                    "error": f"exit code {result.returncode}",
                }

        # Post-install verification — re-detect on PATH.
        installed, version, path = detector.detect(tool)
        if not installed:
            emit("! post-install check failed: tool still not on PATH")
            return {
                "ok": False,
                "log": "\n".join(log_lines),
                "error": "post-install verification failed",
            }
        emit(f"✓ installed at {path}" + (f" (version {version})" if version else ""))
        return {
            "ok": True,
            "log": "\n".join(log_lines),
            "version": version,
            "path": path,
        }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="kubby",
        description="GUI tool for managing local Kubernetes cluster resources.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print install status for all managed tools and exit (no GUI).",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable pywebview DevTools (right-click → Inspect).",
    )
    return parser.parse_args(argv)


def _print_check_report() -> int:
    print("kubby self-check")
    print("================")
    print(f"project:      {PKG_DIR.parent}")
    print(f"python:       {platform.python_version()}")
    print(f"platform:     {platform.system()} {platform.release()}")
    try:
        _pm, label = linux.detect_package_manager()
        print(f"package mgr:  {label}")
    except Exception as e:
        print(f"package mgr:  unsupported ({e})")
    print(f"elevation:    {linux.describe_elevation_method()}")
    print()
    print("Managed tools")
    print("-------------")
    width = max(len(t.label) for t in tools_mod.TOOLS.values()) + 1
    for _key, tool in tools_mod.TOOLS.items():
        installed, version, path = detector.detect(tool)
        if installed:
            status = f"installed ({version or 'unknown'}) at {path}"
        else:
            status = "NOT FOUND"
        print(f"  {tool.label:<{width}} {status}")
    return 0


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    args = _parse_args(raw)

    if args.check:
        return _print_check_report()

    if not INDEX_HTML.exists():
        print(f"error: UI assets missing at {INDEX_HTML}", file=sys.stderr)
        return 2

    # Short-circuit BEFORE pywebview tries its platform backends:
    # `webview/guilib.py` iterates GTK → QT → CEF in turn, writing
    # `print(..., file=sys.stderr)` tracebacks for each failure, and then
    # raises a generic `WebViewException` (NOT an `ImportError`) once all
    # backends have failed. We probe the two well-known backends ourselves
    # up-front so the user sees ONE clean install hint instead of a wall
    # of stderr tracebacks. The `try/except` arms further down remain as
    # a backstop for the rare case where a backend imports OK but later
    # init() fails — `_is_missing_deps_exception()` re-routes those to
    # the same install hint.
    missing = _probe_native_deps()
    if missing is not None:
        return _missing_native_deps(ImportError(missing))

    # pywebview is imported lazily so a missing native binding (the GTK
    # Python bindings `gi` / `gtk` / `webkit2gtk`) surfaces here as an
    # actionable install hint instead of a startup traceback.
    try:
        import webview
    except ImportError as e:
        return _missing_native_deps(e)

    # pywebview 6.x's platform-backend dispatcher (webview/guilib.py)
    # calls its own `logger.exception(...)` from inside WebViewException
    # even when WE catch the resulting exception downstream. That writes
    # the underlying ImportError traceback to stderr via the root logger
    # *before* our `except Exception` arm runs here, which would otherwise
    # wash a real traceback over the user's terminal before our friendly
    # install hint scrolls in. Silence the `webview` logger for the
    # remainder of this process — `main()` returns on both error and
    # happy paths, and the GUI loop runs its own logging once started.
    logging.getLogger("webview").setLevel(logging.CRITICAL + 1)

    api = KubbyAPI()
    try:
        # file:// URLs work natively with pywebview on GTK/WKWebView/WebView2.
        # Relative paths in index.html (./app.js, ./style.css) resolve correctly.
        # `create_window` itself does NOT load any platform backend —
        # that happens later, in `guilib.initialize()` invoked from
        # `webview.start()` below. We keep this `try` here because the
        # same call will still surface real display/session failures on
        # systems where all dependencies are present.
        window = webview.create_window(
            title="kubby",
            url=f"file://{INDEX_HTML}",
            js_api=api,
            width=980,
            height=680,
            min_size=(760, 520),
            resizable=True,
        )
    except ImportError as e:
        # Defensive: even if `_probe_native_deps` was happy, a backend
        # `ImportError` could in principle escape `create_window` — route
        # those to the install-hint helper rather than the generic
        # display-missing message below.
        return _missing_native_deps(e)
    except Exception as e:
        if _is_missing_deps_exception(e):
            return _missing_native_deps(e)
        print(
            f"error: failed to create window: {e}\n"
            "(are you running on a desktop session with a display?)",
            file=sys.stderr,
        )
        return 1

    api.bind_window(window)
    try:
        # THIS is where pywebview 6.x actually loads a platform backend:
        # `webview.start(...)` -> `guilib.initialize(gui)` ->
        # `import_gtk()` / `import_qt()` / `import_cef()`. If none of the
        # backends succeed, pywebview prints per-backend tracebacks to
        # stderr via `print(..., file=...)` and ultimately raises
        # `WebViewException`. The pre-flight probe above should have
        # caught the common "no bindings at all" case before we ever
        # get here, but this `try` remains as a backstop.
        if args.debug:
            webview.start(debug=True)
        else:
            with _suppress_c_stderr():
                webview.start(debug=False)
    except ImportError as e:
        return _missing_native_deps(e)
    except Exception as e:
        if _is_missing_deps_exception(e):
            return _missing_native_deps(e)
        print(
            f"error: failed to start webview: {e}\n"
            "(are you running on a desktop session with a display?)",
            file=sys.stderr,
        )
        return 1
    return 0


@contextlib.contextmanager
def _suppress_c_stderr():
    """Temporarily redirect fd 2 (C-level stderr) to /dev/null.

    Used around ``webview.start()`` to swallow harmless Mesa ZINK warnings
    (e.g. ``MESA: error: ZINK: failed to choose pdev``) that the Python
    ``redirect_stderr`` context manager cannot capture because they are
    written directly by the C library.
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    old_stderr = os.dup(2)
    os.dup2(devnull, 2)
    os.close(devnull)
    try:
        yield
    finally:
        os.dup2(old_stderr, 2)
        os.close(old_stderr)


def _is_missing_deps_exception(err: Exception) -> bool:
    """True if `err` looks like a missing-deps failure from pywebview.

    pywebview wraps backend ImportErrors inside its own
    `webview.util.WebViewException`, whose message looks like::

        "You must have either QT or GTK with Python extensions installed
         in order to use pywebview."

    We also accept the raw ``"No module named '...'"`` form in case a
    backend error ever escapes as plain ImportError.
    """
    msg = str(err)
    return "QT or GTK" in msg or "No module named" in msg


def _probe_native_deps() -> str | None:
    """Return None if at least one recognized pywebview backend is importable.

    pywebview 6.x's platform-backend dispatcher (webview/guilib.py)
    iterates GTK → QT → CEF, writing noisy `print(..., file=sys.stderr)`
    "X cannot be loaded" tracebacks when each backend fails to import,
    and ultimately raises a generic `WebViewException`. We probe the two
    well-known backends (GTK uses `gi`, Qt uses `qtpy`) and short-circuit
    BEFORE letting pywebview try, so the user gets ONE clean install
    hint instead of a wall of tracebacks.

    Returns:
        str | None: None when at least one backend is importable, else a
            message string suitable as the `err` argument to
            `_missing_native_deps()`.
    """
    try:
        import gi  # noqa: F401
        gi.require_version("Gtk", "3.0")
        try:
            gi.require_version("WebKit2", "4.1")
        except ValueError:
            gi.require_version("WebKit2", "4.0")
        from gi.repository import Gtk, WebKit2  # noqa: F401
        return None
    except (ImportError, ValueError):
        pass
    try:
        import qtpy  # type: ignore[import-not-found]  # noqa: F401
        return None
    except ImportError:
        pass
    return "neither GTK (`gi`+Gtk+WebKit2) nor Qt (`qtpy`) Python bindings are importable"


def _missing_native_deps(err: Exception) -> int:
    print(
        f"error: kubby's GUI dependencies are missing: {err}\n"
        "pywebview's GTK backend needs OS-level GTK + WebKit2GTK bindings.\n"
        "Install the packages for your distro:\n\n"
        "  Debian/Ubuntu  sudo apt-get install -y "
        "python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-webkit2-4.1\n"
        "                  (Debian 11 / Ubuntu 20.04 or 22.04: use gir1.2-webkit2-4.0 instead)\n"
        "  Fedora/RHEL    sudo dnf install -y "
        "python3-gobject gtk3 webkit2gtk4.1\n"
        "  Arch           sudo pacman -S --needed "
        "python-gobject gtk3 webkit2gtk-4.1\n\n"
        "Then re-run `kubby`.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
