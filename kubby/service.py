"""UI-agnostic service layer: everything kubby *does*, minus how it looks.

The old ``KubbyAPI`` (the web GUI's bridge) mixed domain behavior
(detect/driver/cluster) with the plumbing that pushed JavaScript
into the browser window. This module keeps that behavior and delivers
progress through two injected callbacks instead::

    service = KubbyService(on_log=..., on_job_done=...)

Callbacks
---------
``on_log(line)``
    One line of streaming output: minikube stdout/stderr, or a
    service-level status message (``"! ..."`` / ``"✓ ..."``).

``on_job_done(payload)``
    A **minikube** job (start/stop/delete) finished, with the same shape
    the frontend always consumed::

        {"ok": bool, "error": str | None, "action": str}

    Fires on success *and* failure — including timeouts and a missing
    binary — so a UI can always clear its busy state.

Threading
---------
Callers run this blocking work on their own threads (a TUI uses a worker
thread and marshals callback output back onto the UI thread). Callbacks
may be invoked from a worker thread — they must be thread-safe and must
not raise (exceptions are logged and swallowed so a UI bug can never kill
a job mid-flight).

kubby does not install anything
-------------------------------
There is no install path here, and deliberately so: kubby never runs
anything as root. ``get_status()`` reports which managed tools are present
and at what version; a missing one is pointed at the tool's own
documentation (``Tool.website``) rather than an install action, and
``get_minikube_prerequisites`` does the same when a missing tool actually
blocks starting a cluster.

Preserved behavior (see the plan's Phase 1 notes):
- ``_kick_job`` holds a lock for the life of the minikube job and rejects
  a second minikube job with ``"...already in progress — wait for it..."``.
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

from kubby import cluster as cluster_mod
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
        """True while a minikube job runs.

        Minikube jobs run on a worker thread, so this is a live check.
        """
        with self._job_lock:
            thread = self._job_thread
            return thread is not None and thread.is_alive()

    # ------------------------------------------------------------------
    # host info + tool status
    # ------------------------------------------------------------------

    def system_info(self) -> dict[str, Any]:
        """Host info so a UI can label the package manager and elevation path."""
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
        """Presence and version of every known tool. Read-only — kubby installs nothing."""
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

        # kubby does not install tools, so every issue here points at the
        # tool's own documentation instead of at an install action.
        if not shutil.which("minikube"):
            issues.append(
                "minikube is not installed — get it from "
                "https://minikube.sigs.k8s.io/"
            )
        if not shutil.which("kubectl"):
            issues.append(
                "kubectl is not installed — get it from "
                "https://kubernetes.io/docs/reference/kubectl/"
            )

        driver = (settings.get("driver") or "").strip()
        if driver and driver not in ("auto",):
            self._check_driver_binary(driver, issues)
        else:
            # Empty / auto: at least one of docker / podman should exist.
            if not (shutil.which("docker") or shutil.which("podman")):
                issues.append(
                    "Neither docker nor podman is installed — minikube "
                    "needs at least one as a container driver. See "
                    "https://minikube.sigs.k8s.io/docs/drivers/."
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
                f"Driver '{driver}' needs '{binary}' on PATH so minikube "
                f"can use it — see https://minikube.sigs.k8s.io/docs/drivers/."
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
            if self._DELETE_ADVICE in line:
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
        # `node` and `owner_*` are read here as well as name/status: the
        # graph needs them, and re-fetching `get pods -A` to get them would
        # double the most expensive call in a refresh. The tree ignores the
        # extra keys.
        pods_by_ns: dict[str, list[dict[str, str]]] = {}
        pod_count = 0
        pods_resp = _run(["kubectl", "get", "pods", "-A", "-o", "json"])
        if pods_resp and pods_resp.returncode == 0:
            try:
                for pod in cluster_mod.parse_pods(json.loads(pods_resp.stdout)):
                    ns = pod["namespace"]
                    pods_by_ns.setdefault(ns, []).append(
                        {
                            "name": pod["name"],
                            "status": pod["phase"],
                            "node": pod["node"],
                            "ip": pod["ip"],
                            "owner_kind": pod["owner_kind"],
                            "owner_name": pod["owner_name"],
                        }
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
    # cluster graph (the topology picture)
    # ------------------------------------------------------------------

    def get_cluster_graph(self, info: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the cluster's *wiring*, not just its inventory.

        Deliberately separate from :meth:`get_cluster_info`: the tree needs
        a third of this, and the extra calls should not be paid for by a
        view that is not on screen. Every call here is independent and
        degrades to empty on failure, exactly as the node/namespace/pod
        calls do — one dead API must not blank the whole picture.

        Pass *info* when the caller already has it. A refresh fetches the
        inventory anyway, and re-running :meth:`get_cluster_info` here would
        repeat three of the round-trips for nothing.

        Costs four extra ``kubectl`` calls on top of the inventory. That is
        only affordable because kubby has no auto-refresh; this runs when the
        user presses ``R``.
        """
        def _json(args: list[str], timeout: int = 15) -> dict[str, Any]:
            """Run kubectl and parse its JSON, or ``{}`` if anything fails."""
            try:
                proc = subprocess.run(
                    args, capture_output=True, text=True, timeout=timeout,
                    env=self._subprocess_env(),
                )
            except (subprocess.TimeoutExpired, OSError):
                log.warning("kubectl failed: %s", " ".join(args))
                return {}
            if proc.returncode != 0 or not proc.stdout.strip():
                return {}
            try:
                data = json.loads(proc.stdout)
            except json.JSONDecodeError:
                log.warning("kubectl returned unparsable JSON: %s", " ".join(args))
                return {}
            return data if isinstance(data, dict) else {}

        if not shutil.which("kubectl"):
            return {"available": False, "error": "kubectl not found on PATH"}

        if info is None:
            info = self.get_cluster_info()
        if not info.get("running"):
            return {"available": False, "error": info.get("error") or "cluster not running"}

        # `get pods -A` already ran as part of the inventory and carries the
        # node and owner fields, so the pods are reused rather than
        # re-fetched. Two shapes have to be reconciled on the way: the
        # inventory keys pods by namespace and calls the phase `status`,
        # while the graph wants both on the pod itself.
        pods = [
            {"namespace": ns.get("name") or "default", **pod}
            for ns in (info.get("namespaces") or [])
            for pod in ns.get("pods") or []
        ]

        model = cluster_mod.build_model(
            nodes=info.get("nodes") or [],
            # The inventory's node list carries only name/status/roles. The
            # architecture layer wants the rest of `nodeInfo` — the OS, the
            # runtime, the CPU and memory a Pod can actually ask for — so the
            # node object is asked for again rather than widened for the
            # tree, which needs none of it.
            node_facts=cluster_mod.parse_node_facts(
                _json(["kubectl", "get", "nodes", "-o", "json"])
            ),
            # The driver the user configured, for the "how minikube runs"
            # line. Empty means auto, which the picture says rather than
            # guessing at.
            driver=str(
                (settings_mod.load()["minikube"].get("driver") or "").strip()
            ),
            namespaces=[str(n.get("name") or "") for n in (info.get("namespaces") or [])],
            pods=pods,
            workloads=cluster_mod.parse_workloads(
                _json(["kubectl", "get", "deploy,statefulset,daemonset", "-A", "-o", "json"])
            ),
            replica_sets=cluster_mod.parse_replica_sets(
                _json(["kubectl", "get", "replicasets", "-A", "-o", "json"])
            ),
            services=cluster_mod.parse_services(_json(["kubectl", "get", "svc", "-A", "-o", "json"])),
            # EndpointSlice, not Endpoints: v1 Endpoints is deprecated as of
            # Kubernetes 1.33 and a current cluster warns about it on stdout.
            endpoint_slices=cluster_mod.parse_endpoint_slices(
                _json(["kubectl", "get", "endpointslices", "-A", "-o", "json"])
            ),
            ingresses=cluster_mod.parse_ingress(_json(["kubectl", "get", "ingress", "-A", "-o", "json"])),
        )
        return {
            "available": True,
            "error": None,
            "context": info.get("context"),
            "version": info.get("version"),
            **model,
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
