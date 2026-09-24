"""kubby entry point — pywebview GUI adapter + CLI.

Public surface:
- `main(argv=None)`: parses CLI args, opens the GUI (or runs --check).
- `KubbyAPI`: instance passed to pywebview as `js_api`; methods are exposed to
  the HTML/JS frontend as `window.pywebview.api.<method_name>`.

`KubbyAPI` is now a thin adapter: all domain behavior (detect, install,
minikube, cluster, settings) lives in `kubby.service.KubbyService`, and this
class only forwards the service's callbacks into `window.evaluate_js` and
carries the two GUI-only web features (`search_images`, `pull_image`) that
disappear with `kubby/ui/` in Phase 5 of the TUI plan.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from kubby import images as images_mod
from kubby.service import KubbyService

log = logging.getLogger("kubby")

if getattr(sys, "frozen", False):
    # PyInstaller onefile: __file__ resolves to the MEIPASS root, not
    # the package directory. Use sys._MEIPASS explicitly to match the
    # `kubby/ui` datas destination in kubby.spec.
    PKG_DIR = Path(sys._MEIPASS) / "kubby"
else:
    PKG_DIR = Path(__file__).resolve().parent
UI_DIR = PKG_DIR / "ui"
INDEX_HTML = UI_DIR / "index.html"


class KubbyAPI:
    """Methods exposed to the JS frontend as `window.pywebview.api.<name>`.

    pywebview exposes any public method on this class. Private methods (those
    starting with `_`) are not exposed. Calls from JS return Promises.
    """

    def __init__(self) -> None:
        self._window: Any = None  # webview.Window, imported lazily in main()
        # Image search / pull state (GUI-only, deleted in Phase 5)
        self._image_pull_thread: threading.Thread | None = None
        self._image_pull_ref: str | None = None
        self._image_pull_lock = threading.Lock()

        # All domain behavior lives here; callbacks push into the webview.
        self._service = KubbyService(
            on_log=self._push_log,
            on_job_done=self._push_job_done,
        )

    def bind_window(self, window: Any) -> None:
        self._window = window

    # ----- service callbacks → JS -----

    def _push_log(self, line: str) -> None:
        """Forward a service log line to `window.kubby.appendLog(...)`."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                f"window.kubby && window.kubby.appendLog({json.dumps(line)})"
            )
        except Exception:
            log.exception("failed to push log line to window")

    def _push_job_done(self, payload: dict[str, Any]) -> None:
        """Forward minikube completion to `window.kubby.onClusterActionDone(...)`."""
        if self._window is None:
            return
        try:
            self._window.evaluate_js(
                "window.kubby && window.kubby"
                f".onClusterActionDone({json.dumps(payload)})"
            )
        except Exception:
            log.exception("failed to notify JS of minikube job completion")

    # ----- public API methods (callable from JS), delegated to the service -----

    def system_info(self) -> dict[str, Any]:
        """Host info so the UI can label package manager / elevation path."""
        return self._service.system_info()

    def get_status(self) -> list[dict[str, Any]]:
        """Current install status of every managed tool."""
        return self._service.get_status()

    def get_minikube_settings(self) -> dict[str, Any]:
        """Persisted minikube settings, merged with defaults."""
        return self._service.get_minikube_settings()

    def save_minikube_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist minikube settings to disk."""
        return self._service.save_minikube_settings(settings)

    def get_minikube_prerequisites(self) -> dict[str, Any]:
        """Pre-flight check for ``minikube start``."""
        return self._service.get_minikube_prerequisites()

    def start_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube start`` using the persisted settings."""
        return self._service.start_minikube()

    def stop_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube stop`` in a worker thread."""
        return self._service.stop_minikube()

    def delete_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube delete`` in a worker thread.

        The UI is responsible for showing a confirm dialog BEFORE calling
        this — this method does the destructive thing as soon as invoked.
        """
        return self._service.delete_minikube()

    def get_cluster_info(self) -> dict[str, Any]:
        """Information about the current Kubernetes cluster (see the service)."""
        return self._service.get_cluster_info()

    def install_all(self) -> dict[str, Any]:
        """Install every tool that isn't already on PATH (one elevation call)."""
        return self._service.install_all()

    def install_tool(self, key: str) -> dict[str, Any]:
        """Install the given tool, streaming output to the UI log."""
        return self._service.install_tool(key)

    # ---------- image search / pull (GUI-only web feature, deleted in Phase 5) ----------

    def search_images(
        self,
        query: str,
        page: int = 1,
        include_local: bool = True,
        include_remote: bool = True,
    ) -> dict[str, Any]:
        """Search for container images — local (podman) and remote (GHCR).

        Returns ``{"ok": True, "local": [...], "remote": {...}}``.
        Set *include_local* / *include_remote* to ``False`` to skip
        unnecessary work when only one result set is needed.
        """
        local: list[dict[str, Any]] = []
        remote: dict[str, Any] = {"results": [], "total_count": 0, "has_more": False}
        if include_local:
            env = KubbyService._subprocess_env()
            local = (
                images_mod.search_local(query, env=env)
                if query.strip()
                else images_mod.search_local(env=env)
            )
        if include_remote:
            remote = images_mod.search_ghcr(query, page)
        return {"ok": True, "local": local, "remote": remote}

    def pull_image(self, image_ref: str) -> dict[str, Any]:
        """Pull a container image via podman in a background thread.

        Rejects the request if another job (minikube, install, or image
        pull) is already in progress. Progress is streamed to JS via
        ``window.kubby.onImagePullProgress(...)`` and completion via
        ``window.kubby.onImagePullDone(...)``.

        NOTE (flagged, deliberate): the original shared one lock with the
        minikube job so the busy check was atomic. The lock now lives
        inside `KubbyService`, so this asks the service instead — there is
        a theoretical check-then-start race that the single-user GUI (and
        this feature, which Phase 5 deletes) never hits.
        """
        with self._image_pull_lock:
            if self._image_pull_thread is not None and self._image_pull_thread.is_alive():
                return {
                    "ok": False,
                    "error": "another image pull is already in progress — wait for it to finish",
                }
            if self._service.is_job_running():
                kind = self._service.job_kind or "another job"
                return {
                    "ok": False,
                    "error": f"a {kind} is already in progress — wait for it to finish",
                }
            if not shutil.which("podman"):
                return {"ok": False, "error": "podman is not installed"}
            self._image_pull_ref = image_ref
            thread = threading.Thread(
                target=self._run_image_pull,
                args=(image_ref,),
                daemon=True,
                name=f"kubby-pull-{image_ref.split('/')[-1]}",
            )
            self._image_pull_thread = thread
            thread.start()
            return {"ok": True, "started": True, "image_ref": image_ref}

    def _run_image_pull(self, image_ref: str) -> None:
        """Worker thread body: run ``podman pull <image_ref>`` and stream.

        Sends each output line to JS via ``window.kubby.onImagePullProgress``
        and notifies completion via ``window.kubby.onImagePullDone``.
        Always clears state in ``finally``.
        """
        ok = False
        err: str | None = None
        proc: subprocess.Popen[str] | None = None
        try:
            try:
                podman_bin = shutil.which("podman") or "podman"
                proc = subprocess.Popen(
                    [podman_bin, "pull", "--", image_ref],
                    env=KubbyService._subprocess_env(),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    errors="replace",
                )

                def _drain() -> None:
                    for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
                        clean = line.rstrip("\r\n")
                        if not clean:
                            continue
                        try:
                            if self._window is not None:
                                self._window.evaluate_js(
                                    "window.kubby && window.kubby"
                                    f".onImagePullProgress({json.dumps(image_ref)},"
                                    f"{json.dumps(clean)})"
                                )
                        except Exception:
                            pass

                reader = threading.Thread(target=_drain, daemon=True)
                reader.start()

                try:
                    returncode = proc.wait(timeout=600)  # 10 min
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                    reader.join()
                    err = "pull timed out (10 min)"
                else:
                    reader.join()
                    if returncode == 0:
                        ok = True
                    else:
                        err = f"exit code {returncode}"
            except FileNotFoundError:
                err = "podman binary not found on PATH"
        except Exception as e:
            log.exception("image pull raised unexpectedly")
            err = str(e)
        finally:
            with self._image_pull_lock:
                self._image_pull_thread = None
                self._image_pull_ref = None
            completion = {"ok": ok, "error": err, "image_ref": image_ref}
            try:
                if self._window is not None:
                    self._window.evaluate_js(
                        "window.kubby && window.kubby"
                        f".onImagePullDone({json.dumps(completion)})"
                    )
            except Exception:
                log.exception("failed to notify JS of image pull completion")


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
    print(f"elevation:    {info['elevation']}")
    print()
    print("Managed tools")
    print("-------------")
    statuses = service.get_status()
    width = max(len(s["label"]) for s in statuses) + 1
    for s in statuses:
        if s["installed"]:
            status = f"installed ({s['version'] or 'unknown'}) at {s['path']}"
        else:
            status = "NOT FOUND"
        print(f"  {s['label']:<{width}} {status}")
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
    # remainder of this process — `main()` returns on both the error and
    # happy paths, and the GUI loop runs its own logging once started.
    logging.getLogger("webview").setLevel(logging.CRITICAL + 1)

    api = KubbyAPI()
    try:
        # file:// URLs work natively with pywebview on GTK/WKWebView/WebView2.
        # Relative paths in index.html (./app.js, ./style.css) resolve correctly.
        # `create_window` itself does NOT load any platform backend —
        # that happens later in `guilib.initialize()` invoked from
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
        # those to the install-hint helper instead of the generic
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
        # stderr via `print(..., file=...)` and ultimately raises a
        # `WebViewException`. The pre-flight probe above should have
        # caught the "no bindings at all" case before we ever get here,
        # but this `try` remains as a backstop.
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

    We also accept the raw ``"No module named '...'`` form in case a
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
