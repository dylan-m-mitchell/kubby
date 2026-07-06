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
import threading
from pathlib import Path
from typing import Any

from kubby import settings as settings_mod

from kubby.installer import (
    detector,
    linux,
    minikube as minikube_mod,
    tools as tools_mod,
)

log = logging.getLogger("kubby")

if getattr(sys, "frozen", False):
    # PyInstaller onefile: __file__ is unreliable for path resolution
    # because the bootloader may flatten the entry-script path. Use
    # sys._MEIPASS (the temp extraction directory) instead.
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
        self._window: webview.Window | None = None
        # Track the currently-running worker thread for minikube start/stop/delete
        # so we (a) reject double-clicks and (b) keep `_current_job_kind`
        # available for any synchronous status checks from JS.
        self._minikube_thread: threading.Thread | None = None
        self._minikube_lock = threading.Lock()
        self._current_job_kind: str | None = None

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

    # ---------- minikube management ----------

    def get_minikube_settings(self) -> dict[str, Any]:
        """Return persisted minikube settings, merged with defaults.

        Always returns a fully-populated dict so the UI can render the form
        even on a fresh install (no settings file yet).
        """
        return settings_mod.load()

    def save_minikube_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Validate and persist minikube settings to disk.

        Returns ``{"ok": True}`` on success or ``{"ok": False, "error": ...}``
        on OSError/ValueError. Per-field validation lives in the UI layer.
        """
        try:
            settings_mod.save(settings)
            return {"ok": True}
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    def get_minikube_prerequisites(self) -> dict[str, Any]:
        """Pre-flight check for ``minikube start``.

        Returns a dict with:
        - ``ok`` (bool): True when no blockers found
        - ``issues`` (list[str]): human-readable list of problems
        - ``settings_path`` (str): where settings live, for the UI to display

        Covers minikube + kubectl on PATH and, for non-``auto`` drivers, that
        the driver binary itself is on PATH. Daemon-level health (e.g. is
        docker actually running?) is left to minikube's own error output,
        which we stream back live.
        """
        settings = settings_mod.load()["minikube"]
        issues: list[str] = []

        if not shutil.which("minikube"):
            issues.append(
                "minikube is not installed — install it from the Docs tab."
            )
        if not shutil.which("kubectl"):
            issues.append(
                "kubectl is not installed — install it from the Docs tab."
            )

        driver = (settings.get("driver") or "").strip()
        if driver and driver not in ("auto",):
            self._check_driver_binary(driver, issues)
        else:
            # Empty / auto: at least one of docker / podman should be present,
            # otherwise minikube will pick a driver we may not have configured.
            if not (shutil.which("docker") or shutil.which("podman")):
                issues.append(
                    "Neither docker nor podman is installed — minikube "
                    "needs at least one as a container driver."
                )

        return {
            "ok": not issues,
            "issues": issues,
            "settings_path": str(settings_mod.CONFIG_FILE),
        }

    def start_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube start`` using the persisted settings."""
        return self._kick_job("start")

    def stop_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube stop`` in a worker thread."""
        return self._kick_job("stop")

    def delete_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube delete`` in a worker thread.

        The UI is responsible for showing a confirm dialog BEFORE calling
        this — this method does the destructive thing as soon as invoked.
        """
        return self._kick_job("delete")

    def _check_driver_binary(self, driver: str, issues: list[str]) -> None:
        """Append a missing-driver issue if the driver binary isn't on PATH.

        minikube's ``none`` driver doesn't need an external binary so it's
        exempt. For other drivers we look up the *actual* binary minikube
        invokes (``docker``, ``podman``, ``qemu-kvm`` for the kvm2 driver)
        — not the driver name itself, which is rarely a real executable.
        """
        if driver == "none":
            return
        driver_binaries = {"docker": "docker", "podman": "podman", "kvm2": "qemu-kvm"}
        binary = driver_binaries.get(driver, driver)
        if not shutil.which(binary):
            issues.append(
                f"Driver '{driver}' needs '{binary}' on PATH — install it "
                f"from the Docs tab so minikube can use it."
            )

    def _kick_job(self, action: str) -> dict[str, Any]:
        """Build argv from settings and dispatch to a worker thread.

        Rejects re-entry while another job is active so the user can't
        trigger stop + delete simultaneously from a double-click.
        """
        with self._minikube_lock:
            if self._minikube_thread is not None and self._minikube_thread.is_alive():
                kind = self._current_job_kind or "another command"
                return {
                    "ok": False,
                    "error": f"a {kind} is already in progress — wait for it to finish",
                }
            settings = settings_mod.load()["minikube"]
            if action == "start":
                argv = minikube_mod.start_args(settings)
            elif action == "stop":
                argv = minikube_mod.stop_args()
            elif action == "delete":
                argv = minikube_mod.delete_args()
            else:
                return {"ok": False, "error": f"unknown action: {action!r}"}

            self._current_job_kind = action
            thread = threading.Thread(
                target=self._run_minikube_job,
                args=(action, argv),
                daemon=True,
                name=f"kubby-minikube-{action}",
            )
            self._minikube_thread = thread
            thread.start()
            return {"ok": True, "started": True, "action": action}

    def _run_minikube_job(self, action: str, argv: list[str]) -> None:
        """Worker thread body: run ``argv`` and stream results.

        Always notifies JS of completion (success or failure) via
        ``window.kubby.onClusterActionDone``. Even on exception the
        ``finally``-guard clears state so the next ``_kick_job`` can run.
        """
        self._emit_log(f"$ {' '.join(argv)}")
        ok = False
        err: str | None = None
        proc: subprocess.Popen[str] | None = None
        try:
            try:
                # Stream stdout/stderr incrementally so the UI sees live
                # progress during long-running commands like `minikube start`.
                # We use Popen with line-buffered text mode and a reader
                # thread per stream, then wait() in this thread.
                proc = subprocess.Popen(
                    argv,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    bufsize=1,
                    encoding="utf-8",
                    # minikube output occasionally includes non-UTF8 bytes
                    # (ANSI escapes, stray locale flag) — replace with U+FFFD
                    # instead of letting UnicodeDecodeError escape this worker.
                    errors="replace",
                )

                def _drain(stream) -> None:
                    for line in iter(stream.readline, ""):
                        self._emit_log(line.rstrip("\r\n"))

                t_out = threading.Thread(target=_drain, args=(proc.stdout,), daemon=True)
                t_err = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
                t_out.start()
                t_err.start()

                try:
                    returncode = proc.wait(timeout=900)  # 15 min
                except subprocess.TimeoutExpired:
                    proc.kill()
                    # proc.kill() sends SIGKILL — the child is guaranteed
                    # to die. Bare wait() reaps it so its pipes get EOF
                    # and the reader threads can terminate; once that
                    # happens the threads return from iter() immediately
                    # so a bare join() (no timeout) is sufficient.
                    proc.wait()
                    t_out.join()
                    t_err.join()
                    msg = "minikube command timed out (15 min)"
                    self._emit_log(f"! {msg}")
                    err = msg
                else:
                    t_out.join()
                    t_err.join()
                    if returncode == 0:
                        self._emit_log(f"✓ minikube {action} succeeded")
                        ok = True
                    else:
                        self._emit_log(
                            f"! minikube {action} failed (exit {returncode})"
                        )
                        err = f"exit code {returncode}"
            except FileNotFoundError:
                msg = "minikube binary not found on PATH"
                self._emit_log(f"! {msg}")
                err = msg
        except Exception as e:
            log.exception("minikube job raised unexpectedly")
            self._emit_log(f"! unexpected error: {e}")
            err = str(e)
        finally:
            with self._minikube_lock:
                self._current_job_kind = None
                self._minikube_thread = None
            completion = {"ok": ok, "error": err, "action": action}
            try:
                if self._window is not None:
                    self._window.evaluate_js(
                        "window.kubby && window.kubby"
                        f".onClusterActionDone({json.dumps(completion)})"
                    )
            except Exception:
                log.exception("failed to notify JS of minikube job completion")


    def get_cluster_info(self) -> dict[str, Any]:
        """Return information about the current Kubernetes cluster.

        Returns a dict with:
        - running (bool): whether a cluster is reachable
        - error (str|None): error message if not running
        - context (str|None): active kubectl context name
        - version (str|None): server Kubernetes version
        - nodes (list[dict]): list of {name, status, roles}
        - namespaces (list[dict]): list of {name, pods: [{name, status}]}
        - pod_count (int): total pods across all namespaces
        """
        def _run(args: list[str], timeout: int = 15) -> subprocess.CompletedProcess[str] | None:
            try:
                return subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                )
            except subprocess.TimeoutExpired:
                log.warning("kubectl timed out: %s", " ".join(args))
                return None

        # Check if kubectl is available
        if not shutil.which("kubectl"):
            return {"running": False, "error": "kubectl not found on PATH"}

        # Get current context
        ctx = _run(["kubectl", "config", "current-context"])
        if ctx is None or ctx.returncode != 0:
            return {"running": False, "error": (ctx.stderr.strip() if ctx else "timeout") or "no current context"}
        context = ctx.stdout.strip()

        # Try to reach the cluster
        ver = _run(["kubectl", "version", "-o", "json"])
        if ver is None or ver.returncode != 0:
            return {"running": False, "error": (ver.stderr.strip() if ver else "timeout") or "cannot reach cluster", "context": context}

        version = None
        try:
            ver_data = json.loads(ver.stdout)
            version = ver_data.get("serverVersion", {}).get("gitVersion", "")
        except (json.JSONDecodeError, KeyError):
            pass

        # Fetch each resource type separately for reliability. A single bulk
        # `kubectl get nodes,namespaces,pods` call can return partial results
        # or silently omit items depending on the cluster / kubectl version.
        # Separate calls also give us per-namespace pod breakdown.

        # --- nodes ---
        nodes: list[dict[str, Any]] = []
        nodes_resp = _run(["kubectl", "get", "nodes", "-o", "json"])
        if nodes_resp and nodes_resp.returncode == 0:
            try:
                for item in json.loads(nodes_resp.stdout).get("items", []):
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
            except (json.JSONDecodeError, KeyError):
                pass

        # --- namespaces ---
        ns_names: list[str] = []
        ns_resp = _run(["kubectl", "get", "namespaces", "-o", "json"])
        if ns_resp and ns_resp.returncode == 0:
            try:
                for item in json.loads(ns_resp.stdout).get("items", []):
                    ns_names.append(item["metadata"]["name"])
            except (json.JSONDecodeError, KeyError):
                pass

        # --- pods ---
        pods_by_ns: dict[str, list[dict[str, str]]] = {}
        pod_count = 0
        pods_resp = _run(["kubectl", "get", "pods", "-A", "-o", "json"])
        if pods_resp and pods_resp.returncode == 0:
            try:
                for item in json.loads(pods_resp.stdout).get("items", []):
                    ns = item["metadata"]["namespace"]
                    pod_name = item["metadata"]["name"]
                    phase = item.get("status", {}).get("phase", "Unknown")
                    pods_by_ns.setdefault(ns, []).append(
                        {"name": pod_name, "status": phase}
                    )
                    pod_count += 1
            except (json.JSONDecodeError, KeyError):
                pass

        # Build namespace list with per-namespace pod data, preserving the
        # order returned by kubectl (alphabetical).
        namespaces: list[dict[str, Any]] = []
        for ns in ns_names:
            namespaces.append({
                "name": ns,
                "pods": pods_by_ns.get(ns, []),
            })

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

        # Two install paths, used in order of preference:
        # 1. `install_script` — upstream-provided `sh -c` snippet, PM-agnostic.
        # 2. `pkg_name` — host-PM install for tools with no upstream installer.
        if tool.install_script:
            commands: list[tuple[str, ...]] = [("sh", "-c", tool.install_script)]
            emit(
                f"Running upstream installer for {tool.label} via "
                f"{linux.describe_elevation_method()}."
            )
        elif tool.pkg_name:
            try:
                pm_key, pm_label = linux.detect_package_manager()
            except Exception as e:
                emit(f"! {e}")
                return {"ok": False, "log": "\n".join(log_lines), "error": str(e)}
            try:
                commands = [linux.pkg_install_argv(pm_key, tool.pkg_name)]
            except ValueError as e:
                msg = str(e)
                emit(f"! {msg}")
                return {"ok": False, "log": "\n".join(log_lines), "error": msg}
            emit(f"Using package manager: {pm_label}")
        else:
            msg = f"No install procedure defined for {tool.label}"
            emit(f"! {msg}")
            return {"ok": False, "log": "\n".join(log_lines), "error": msg}

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
