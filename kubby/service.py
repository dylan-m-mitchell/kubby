"""UI-agnostic service layer: everything kubby *does*, minus how it looks.

The old ``KubbyAPI`` (the web GUI's bridge) mixed domain behavior
(detect/install/driver/cluster) with the plumbing that pushed JavaScript
into the browser window. This module keeps that behavior and delivers
progress through two injected callbacks instead::

    service = KubbyService(on_log=..., on_job_done=...)

Callbacks
---------
``on_log(line)``
    One line of streaming output: minikube stdout/stderr, install script
    output, or a service-level status message (``"! ..."`` / ``"✓ ..."``).

``on_job_done(payload)``
    A **minikube** job (start/stop/delete) finished, with the same shape
    the frontend always consumed::

        {"ok": bool, "error": str | None, "action": str}

    Fires on success *and* failure — including timeouts and a missing
    binary — so a UI can always clear its busy state. Install methods do
    **not** report through this callback; they are synchronous and return
    their summary dict (``install_all`` / ``install_tool``) directly.

Threading
---------
Callers run this blocking work on their own threads (a TUI uses a worker
thread and marshals callback output back onto the UI thread). Callbacks
may be invoked from a worker thread — they must be thread-safe and must
not raise (exceptions are logged and swallowed so a UI bug can never kill
a job mid-flight).

Preserved behavior (see the plan's Phase 1 notes):
- ``_kick_job`` holds a lock for the life of the minikube job and rejects
  a second minikube job with ``"...already in progress — wait for it..."``.
- ``install_all`` / ``install_tool`` refuse while any job is running, and
  refuse to overlap each other (the single-install-at-a-time rule the JS
  frontend enforced).
- ``install_all`` merges every missing tool into ONE ``sh -c`` script run
  under ONE elevation call, then re-detects each tool to report per-tool
  success rather than trusting the script's exit code alone.
- Streaming uses ``Popen(text, bufsize=1, encoding="utf-8",
  errors="replace")`` with a reader thread per stream, a 15-minute
  timeout, and kill+reap before giving up.
- ``_subprocess_env()`` un-scrambles PyInstaller's ``LD_LIBRARY_PATH`` so
  child binaries don't load the bundled libraries (exit 127 otherwise).
"""
from __future__ import annotations

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import threading
from typing import Any, Callable

from kubby import images as images_mod
from kubby import settings as settings_mod
from kubby.installer import (
    detector,
    linux,
    minikube as minikube_mod,
    tools as tools_mod,
)

log = logging.getLogger("kubby")

_NOOP_LOG: Callable[[str], None] = lambda line: None
_NOOP_DONE: Callable[[dict], None] = lambda payload: None


class KubbyService:
    """Domain operations with callback-based progress reporting."""

    def __init__(
        self,
        on_log: Callable[[str], None] = _NOOP_LOG,
        on_job_done: Callable[[dict], None] = _NOOP_DONE,
    ) -> None:
        # Public (re)assignable: a UI builds the service first, then attaches
        # its own sinks — `service.on_log = self._handle_line`.
        self.on_log = on_log
        self.on_job_done = on_job_done

        # Single-job bookkeeping for minikube start/stop/delete.
        self._job_lock = threading.Lock()
        self._job_thread: threading.Thread | None = None
        self._job_kind: str | None = None

        # Install slot: set for the synchronous duration of
        # install_all()/install_tool() so two installs can't run at once
        # and no install overlaps a minikube job.
        self._install_active = False

    # ------------------------------------------------------------------
    # callbacks
    # ------------------------------------------------------------------

    def _emit(self, line: str) -> None:
        """Push one log line to the consumer. Never raises."""
        try:
            self.on_log(line)
        except Exception:
            log.exception("on_log callback raised")

    def _notify_done(self, payload: dict[str, Any]) -> None:
        """Push a minikube-job completion payload. Never raises."""
        try:
            self.on_job_done(payload)
        except Exception:
            log.exception("on_job_done callback raised")

    # ------------------------------------------------------------------
    # job state (UIs use this to disable actions)
    # ------------------------------------------------------------------

    @property
    def job_kind(self) -> str | None:
        """Action of the in-flight minikube job, or None when idle."""
        return self._job_kind

    def is_job_running(self) -> bool:
        """True while a minikube job (or, see below, an install) runs.

        Minikube jobs run on a worker thread, so this is a live check.
        """
        with self._job_lock:
            thread = self._job_thread
            return bool(
                (thread is not None and thread.is_alive()) or self._install_active
            )

    def is_install_active(self) -> bool:
        """True while install_all()/install_tool() is executing."""
        with self._job_lock:
            return self._install_active

    def _reserve_install(self) -> str | None:
        """Claim the install slot; return an error message if unavailable."""
        with self._job_lock:
            thread = self._job_thread
            if thread is not None and thread.is_alive():
                kind = self._job_kind or "another command"
                return f"a {kind} is already in progress — wait for it to finish"
            if self._install_active:
                return "an install is already in progress — wait for it to finish"
            self._install_active = True
            return None

    def _release_install(self) -> None:
        with self._job_lock:
            self._install_active = False

    # ------------------------------------------------------------------
    # host info + tool status
    # ------------------------------------------------------------------

    def system_info(self) -> dict[str, Any]:
        """Host info so a UI can label package manager / elevation path."""
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
        """Current install status of every managed tool."""
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

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def get_minikube_settings(self) -> dict[str, Any]:
        """Persisted minikube settings merged with defaults."""
        return settings_mod.load()

    def save_minikube_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        """Persist settings; ``{"ok": True}`` or ``{"ok": False, "error"}``."""
        try:
            settings_mod.save(settings)
            return {"ok": True}
        except (OSError, ValueError) as e:
            return {"ok": False, "error": str(e)}

    def get_minikube_prerequisites(self) -> dict[str, Any]:
        """Pre-flight check for ``minikube start``.

        Returns ``{"ok", "issues", "settings_path"}``. Daemon-level health
        (is docker actually running?) is left to minikube's own output.
        """
        settings = settings_mod.load()["minikube"]
        issues: list[str] = []

        if not shutil.which("minikube"):
            issues.append("minikube is not installed — install it from the Tools panel.")
        if not shutil.which("kubectl"):
            issues.append("kubectl is not installed — install it from the Tools panel.")

        driver = (settings.get("driver") or "").strip()
        if driver and driver not in ("auto",):
            self._check_driver_binary(driver, issues)
        else:
            # Empty / auto: at least one of docker / podman should exist.
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

    def _check_driver_binary(self, driver: str, issues: list[str]) -> None:
        """Append a missing-driver issue if the driver binary isn't on PATH.

        ``none`` needs no external binary. For the rest we look up the
        *actual* binary minikube invokes (``qemu-kvm`` for kvm2), not the
        driver name itself.
        """
        if driver == "none":
            return
        driver_binaries = {"docker": "docker", "podman": "podman", "kvm2": "qemu-kvm"}
        binary = driver_binaries.get(driver, driver)
        if not shutil.which(binary):
            issues.append(
                f"Driver '{driver}' needs '{binary}' on PATH — install it "
                f"from the Tools panel so minikube can use it."
            )

    # ------------------------------------------------------------------
    # minikube jobs
    # ------------------------------------------------------------------

    #: minikube's own one-line failure summary, e.g.
    #: ``X Exiting due to GUEST_PROVISION: error provisioning guest: ...``.
    #: Its output is long, repeats itself three times over, and is wrapped in
    #: box-drawing characters; this is the one line worth pulling out, because
    #: "exit code 80" on its own tells the reader nothing at all.
    _EXIT_RE = re.compile(r"Exiting due to ([A-Z_]+):\s*(.+?)\s*$")
    #: minikube knows its own remedy for the most common local failure — a
    #: machine left in a state podman cannot start. It says so, mid-wall-of-
    #: text, and then exits.
    _DELETE_ADVICE = 'Running "minikube delete" may fix it'

    def _emit_failure_summary(self, lines: list[str]) -> None:
        """Add the readable part of a failed job to the log.

        The full stream is already in the log panel, unmodified. This only
        adds what is worth reading at a glance: which stage failed, and
        whether minikube told us how to fix it.
        """
        reason = ""
        advice = ""
        for line in lines:
            match = self._EXIT_RE.search(line)
            if match:
                reason = f"{match.group(1)}: {match.group(2)}"
            elif self._DELETE_ADVICE in line:
                advice = "minikube delete, then start again"
        if reason:
            self._emit(f"  failed at: {reason}")
        if advice:
            self._emit(f"  try: {advice}")


    def start_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube start`` using the persisted settings."""
        return self._kick_job("start")

    def stop_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube stop`` in a worker thread."""
        return self._kick_job("stop")

    def delete_minikube(self) -> dict[str, Any]:
        """Kick off ``minikube delete`` in a worker thread.

        The UI must confirm with the user BEFORE calling this — it does
        the destructive thing as soon as it's invoked.
        """
        return self._kick_job("delete")

    def _kick_job(self, action: str) -> dict[str, Any]:
        """Build argv from settings and dispatch to a worker thread.

        Rejects re-entry while another minikube job is active so the user
        can't trigger stop + delete simultaneously from a double-click.
        """
        with self._job_lock:
            if self._job_thread is not None and self._job_thread.is_alive():
                kind = self._job_kind or "another command"
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

            self._job_kind = action
            thread = threading.Thread(
                target=self._run_minikube_job,
                args=(action, argv),
                daemon=True,
                name=f"kubby-minikube-{action}",
            )
            self._job_thread = thread
            thread.start()
            return {"ok": True, "started": True, "action": action}

    def _run_minikube_job(self, action: str, argv: list[str]) -> None:
        """Worker thread body: run ``argv`` and stream results.

        Always notifies the consumer of completion (success or failure).
        The ``finally`` guard clears state so the next ``_kick_job`` can
        run even after an unexpected exception.
        """
        self._emit(f"$ {' '.join(argv)}")
        ok = False
        err: str | None = None
        proc: subprocess.Popen[str] | None = None
        # Kept so a failure can be summarised; the log panel already has
        # every line, this is only for picking the useful ones back out.
        seen: list[str] = []
        seen_lock = threading.Lock()
        try:
            try:
                # Stream stdout/stderr incrementally so the UI sees live
                # progress during long-running commands like `minikube start`.
                # Popen with line-buffered text mode + a reader thread per
                # stream, then wait() in this thread.
                proc = subprocess.Popen(
                    argv,
                    env=self._subprocess_env(),
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
                        text = line.rstrip("\r\n")
                        with seen_lock:
                            seen.append(text)
                        self._emit(text)

                t_out = threading.Thread(target=_drain, args=(proc.stdout,), daemon=True)
                t_err = threading.Thread(target=_drain, args=(proc.stderr,), daemon=True)
                t_out.start()
                t_err.start()

                try:
                    returncode = proc.wait(timeout=900)  # 15 min
                except subprocess.TimeoutExpired:
                    proc.kill()
                    # proc.kill() sends SIGKILL — the child is guaranteed to
                    # die. Bare wait() reaps it so its pipes get EOF and the
                    # reader threads terminate; then join() suffices.
                    proc.wait()
                    t_out.join()
                    t_err.join()
                    msg = "minikube command timed out (15 min)"
                    self._emit(f"! {msg}")
                    err = msg
                else:
                    t_out.join()
                    t_err.join()
                    if returncode == 0:
                        self._emit(f"✓ minikube {action} succeeded")
                        ok = True
                    else:
                        self._emit(f"! minikube {action} failed (exit {returncode})")
                        err = f"exit code {returncode}"
                        self._emit_failure_summary(seen)
            except FileNotFoundError:
                msg = "minikube binary not found on PATH"
                self._emit(f"! {msg}")
                err = msg
        except Exception as e:
            log.exception("minikube job raised unexpectedly")
            self._emit(f"! unexpected error: {e}")
            err = str(e)
        finally:
            with self._job_lock:
                self._job_kind = None
                self._job_thread = None
            self._notify_done({"ok": ok, "error": err, "action": action})

    # ------------------------------------------------------------------
    # cluster state
    # ------------------------------------------------------------------

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
                    env=self._subprocess_env(),
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
            return {
                "running": False,
                "error": (ctx.stderr.strip() if ctx else "timeout") or "no current context",
            }
        context = ctx.stdout.strip()

        # Try to reach the cluster
        ver = _run(["kubectl", "version", "-o", "json"])
        if ver is None or ver.returncode != 0:
            return {
                "running": False,
                "error": (ver.stderr.strip() if ver else "timeout") or "cannot reach cluster",
                "context": context,
            }

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

    # ------------------------------------------------------------------
    # local images (podman, no network)
    # ------------------------------------------------------------------

    def list_local_images(self, query: str | None = None) -> list[dict[str, Any]]:
        """Local podman images, optionally filtered by *query*.

        Returns ``[]`` when podman is absent or fails. Never touches the
        network — remote registry search was removed with the GUI.
        """
        env = self._subprocess_env()
        if query and query.strip():
            return images_mod.search_local(query, env=env)
        return images_mod.search_local(env=env)

    # ------------------------------------------------------------------
    # installs
    # ------------------------------------------------------------------

    @staticmethod
    def _build_install_shell(
        tool: tools_mod.Tool, pm_key: str | None
    ) -> str | None:
        """Return a single shell command line that installs `tool`.

        Returns ``None`` when the tool has no install procedure or the
        package manager can't be resolved.
        """
        if tool.install_script:
            return tool.install_script
        if tool.pkg_name and pm_key is not None:
            try:
                argv = linux.pkg_install_argv(pm_key, tool.pkg_name)
            except ValueError:
                return None
            return " ".join(argv)
        return None

    def install_all(self) -> dict[str, Any]:
        """Install every tool that isn't already on PATH.

        All install commands are combined into a **single shell script**
        that runs under **one elevation call** (pkexec or sudo), so the
        user only authenticates once regardless of how many tools need
        installing.

        Returns a summary dict with ``ok``, ``installed``, ``failed``,
        ``skipped`` (and ``error`` on early failure).
        """
        denied = self._reserve_install()
        if denied is not None:
            return {
                "ok": False,
                "installed": [],
                "failed": [],
                "skipped": 0,
                "error": denied,
            }
        try:
            return self._install_all_locked()
        finally:
            self._release_install()

    def _install_all_locked(self) -> dict[str, Any]:
        # Discover what needs installing
        needed: list[tuple[str, tools_mod.Tool]] = []
        for key, tool in tools_mod.TOOLS.items():
            installed, _ver, _path = detector.detect(tool)
            if not installed:
                needed.append((key, tool))

        if not needed:
            self._emit("All tools are already installed.")
            return {
                "ok": True,
                "installed": [],
                "failed": [],
                "skipped": len(tools_mod.TOOLS),
            }

        # If any tool uses pkg_name, detect the package manager once.
        pm_key: str | None = None
        pm_label: str = ""
        for _key, tool in needed:
            if tool.pkg_name:
                try:
                    pm_key, pm_label = linux.detect_package_manager()
                except Exception as e:
                    self._emit(f"! {e}")
                    return {
                        "ok": False,
                        "installed": [],
                        "failed": [k for k, _ in needed],
                        "skipped": len(tools_mod.TOOLS) - len(needed),
                        "error": str(e),
                    }
                break

        # Build a single combined shell script for all tools.
        script_lines: list[str] = ["set -e"]
        for _key, tool in needed:
            script_lines.append(f"\n# --- {tool.label} ---")
            shell_cmd = self._build_install_shell(tool, pm_key)
            if shell_cmd is None:
                self._emit(f"! No install procedure defined for {tool.label}")
                return {
                    "ok": False,
                    "installed": [],
                    "failed": [k for k, _ in needed],
                    "skipped": len(tools_mod.TOOLS) - len(needed),
                    "error": f"no install procedure for {tool.label}",
                }
            script_lines.append(shell_cmd)
        combined_script = "\n".join(script_lines)

        self._emit(
            f"Installing {len(needed)} tool(s) via "
            f"{linux.describe_elevation_method()}…"
        )
        if pm_label:
            self._emit(f"Package manager: {pm_label}")

        # Run the combined script under a single elevation call.
        log_lines: list[str] = []

        def emit(line: str) -> None:
            log_lines.append(line)
            self._emit(line)

        try:
            returncode = self._run_elevated_streaming(
                ("sh", "-c", combined_script), emit
            )
        except FileNotFoundError as e:
            msg = f"command not found: {e.filename}"
            self._emit(f"! {msg}")
            return {
                "ok": False,
                "installed": [],
                "failed": [k for k, _ in needed],
                "skipped": len(tools_mod.TOOLS) - len(needed),
                "error": msg,
            }
        except subprocess.TimeoutExpired:
            self._emit("! command timed out")
            return {
                "ok": False,
                "installed": [],
                "failed": [k for k, _ in needed],
                "skipped": len(tools_mod.TOOLS) - len(needed),
                "error": "install timed out",
            }

        if returncode != 0:
            self._emit(f"! combined script failed with exit code {returncode}")
            # Post-verify to determine which tools succeeded vs failed.
            installed_keys: list[str] = []
            failed_keys: list[str] = []
            for key, tool in needed:
                inst, _ver, _path = detector.detect(tool)
                if inst:
                    installed_keys.append(key)
                else:
                    failed_keys.append(key)
            self._emit(
                f"Done — {len(installed_keys)} installed, "
                f"{len(failed_keys)} failed."
            )
            return {
                "ok": len(failed_keys) == 0,
                "installed": installed_keys,
                "failed": failed_keys,
                "skipped": len(tools_mod.TOOLS) - len(needed),
            }

        # All commands succeeded — verify each tool.
        installed_keys: list[str] = []
        failed_keys: list[str] = []
        for key, tool in needed:
            inst, ver, path = detector.detect(tool)
            if inst:
                self._emit(
                    f"✓ {tool.label} installed at {path}"
                    + (f" (version {ver})" if ver else "")
                )
                installed_keys.append(key)
            else:
                self._emit(f"! {tool.label} post-install check failed")
                failed_keys.append(key)

        self._emit(
            f"\nDone — {len(installed_keys)} installed, "
            f"{len(failed_keys)} failed."
        )
        return {
            "ok": len(failed_keys) == 0,
            "installed": installed_keys,
            "failed": failed_keys,
            "skipped": len(tools_mod.TOOLS) - len(needed),
        }

    def install_tool(self, key: str) -> dict[str, Any]:
        """Install the given tool, streaming output to ``on_log``.

        Returns a dict with `ok: bool`, optional `version`, optional
        `error`, and the full `log` joined as a string for convenience.
        """
        denied = self._reserve_install()
        if denied is not None:
            return {"ok": False, "log": "", "error": denied}
        try:
            return self._install_tool_locked(key)
        finally:
            self._release_install()

    def _install_tool_locked(self, key: str) -> dict[str, Any]:
        tool = tools_mod.TOOLS.get(key)
        if tool is None:
            return {"ok": False, "error": f"Unknown tool: {key!r}"}

        log_lines: list[str] = []

        def emit(line: str) -> None:
            log_lines.append(line)
            self._emit(line)

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
                returncode = self._run_elevated_streaming(cmd, emit)
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
            if returncode != 0:
                emit(f"! command failed with exit code {returncode}")
                return {
                    "ok": False,
                    "log": "\n".join(log_lines),
                    "error": f"exit code {returncode}",
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

    # ------------------------------------------------------------------
    # subprocess helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _subprocess_env() -> dict[str, str]:
        """Return a copy of the current environment for spawning binaries.

        PyInstaller's onefile bootloader sets ``LD_LIBRARY_PATH`` to its
        bundled library directory, which can cause system binaries
        (podman, minikube, kubectl) to load the wrong shared libraries
        and fail with exit code 127. Restore the original value if
        PyInstaller saved it; otherwise drop it entirely.
        """
        env = os.environ.copy()
        if "LD_LIBRARY_PATH_ORIG" in env:
            env["LD_LIBRARY_PATH"] = env["LD_LIBRARY_PATH_ORIG"]
        else:
            env.pop("LD_LIBRARY_PATH", None)
        return env

    def _run_elevated_streaming(
        self, cmd: tuple[str, ...], emit: Callable[[str], None]
    ) -> int:
        """Run `cmd` under elevation, streaming stdout+stderr live via `emit`.

        Uses a reader thread so ``proc.wait(timeout=...)`` can interrupt a
        hung process even when it produces no output. Returns the process
        exit code. Raises `subprocess.TimeoutExpired` if the command runs
        longer than ``linux._INSTALL_TIMEOUT_S``.
        """
        argv = linux.wrap_elevated(cmd)
        proc = subprocess.Popen(
            argv,
            env=self._subprocess_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            encoding="utf-8",
            errors="replace",
        )

        def _drain() -> None:
            for line in iter(proc.stdout.readline, ""):  # type: ignore[union-attr]
                emit(line.rstrip("\r\n"))

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        try:
            returncode = proc.wait(timeout=linux._INSTALL_TIMEOUT_S)
            reader.join()
            return returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            reader.join()
            raise
