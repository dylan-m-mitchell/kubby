"""Shared test doubles.

`FakeService` is a duck-typed stand-in for `kubby.service.KubbyService`:
the TUI only calls methods and only assigns `on_log` / `on_job_done`, so
the fake records what it was asked for and answers from canned data.
Tests mutate `service.cluster` / `service.tools` between refreshes to
exercise re-check without touching the host.
"""

from __future__ import annotations

import time
from typing import Any

import pytest


class FakeService:
    """Canned, host-free `KubbyService` replacement."""

    def __init__(self) -> None:
        # The app assigns these; callables so tests can fire them directly.
        self.on_log: Any = lambda line: None
        self.on_job_done: Any = lambda payload: None

        #: every method invoked, in order — proves what the UI actually asked for
        self.calls: list[str] = []

        self.system: dict[str, Any] = {
            "platform": "Linux",
            "release": "6.0.0",
            "python": "3.12.3",
            "package_manager_label": "APT (Debian/Ubuntu)",
            "elevation": "pkexec (graphical polkit prompt)",
        }
        self.tools: list[dict[str, Any]] = [
            {
                "key": "minikube",
                "label": "minikube",
                "description": "",
                "website": "",
                "installed": True,
                "version": "1.38.1",
                "path": "/usr/local/bin/minikube",
            },
            {
                "key": "helm",
                "label": "helm",
                "description": "",
                "website": "",
                "installed": False,
                "version": None,
                "path": None,
            },
            {
                "key": "podman",
                "label": "podman",
                "description": "",
                "website": "",
                "installed": True,
                "version": "4.9.3",
                "path": "/usr/bin/podman",
            },
            {
                "key": "kubectl",
                "label": "kubectl",
                "description": "",
                "website": "",
                "installed": True,
                "version": "1.36.2",
                "path": "/usr/local/bin/kubectl",
            },
        ]
        self.cluster: dict[str, Any] = {
            "running": True,
            "error": None,
            "context": "minikube",
            "version": "v1.30.1",
            "nodes": [
                {"name": "minikube", "status": "Ready", "roles": ["control-plane"]},
            ],
            "namespaces": [
                {"name": "default", "pods": [
                    {"name": "web-2-def", "status": "Running"},
                ]},
                {"name": "kube-system", "pods": [
                    {"name": "coredns-7x", "status": "Running"},
                    {"name": "metrics-1", "status": "CrashLoopBackOff"},
                ]},
            ],
            "pod_count": 3,
        }
        self.images: list[dict[str, Any]] = [
            {"name": "docker.io/library/nginx:alpine", "tags": ["alpine"],
             "created": "", "size": 125_829_120, "local": True},
            {"name": "quay.io/prometheus/busybox:latest", "tags": ["latest"],
             "created": "", "size": 2_097_152, "local": True},
        ]

        self.job_running = False
        self.job_kind: str | None = None
        #: what install/start/stop/delete calls should report
        self.job_result: dict[str, Any] = {"ok": True, "started": True}
        self.install_result: dict[str, Any] = {"ok": True, "installed": ["helm"],
                                               "failed": [], "skipped": 0}
        #: seconds an install "takes" — gives pilot tests a busy window
        self.install_delay = 0.0
        self.prerequisites: dict[str, Any] = {"ok": True, "issues": [],
                                              "settings_path": "/tmp/settings.json"}
        self.settings: dict[str, Any] = {"minikube": {
            "driver": "podman", "rootless": False, "cpus": "4",
            "memory": "4000", "kubernetes_version": "", "addons": [],
        }}
        self.settings_saved: list[dict[str, Any]] = []
        #: flip to True to make a fetch raise, as a real service might
        self.raise_on_cluster = False

    # ----- data -------------------------------------------------------

    def system_info(self) -> dict[str, Any]:
        self.calls.append("system_info")
        return dict(self.system)

    def get_status(self) -> list[dict[str, Any]]:
        self.calls.append("get_status")
        return [dict(tool) for tool in self.tools]

    def get_cluster_info(self) -> dict[str, Any]:
        self.calls.append("get_cluster_info")
        if self.raise_on_cluster:
            raise RuntimeError("kubectl exploded")
        return {
            **self.cluster,
            "nodes": [dict(n) for n in self.cluster["nodes"]],
            "namespaces": [
                {**ns, "pods": [dict(p) for p in ns.get("pods", [])]}
                for ns in self.cluster["namespaces"]
            ],
        }

    def list_local_images(self, query: str | None = None) -> list[dict[str, Any]]:
        self.calls.append("list_local_images")
        images = self.images
        if query:
            needle = query.lower()
            images = [i for i in images if needle in i["name"].lower()]
        return [dict(image) for image in images]

    # ----- settings / preflight ---------------------------------------

    def get_minikube_settings(self) -> dict[str, Any]:
        self.calls.append("get_minikube_settings")
        return {"minikube": dict(self.settings["minikube"])}

    def save_minikube_settings(self, settings: dict[str, Any]) -> dict[str, Any]:
        self.calls.append("save_minikube_settings")
        if not isinstance(settings, dict):
            return {"ok": False, "error": "settings must be a JSON object"}
        self.settings_saved.append(settings)
        self.settings = {"minikube": dict(settings.get("minikube", {}))}
        return {"ok": True}

    def get_minikube_prerequisites(self) -> dict[str, Any]:
        self.calls.append("get_minikube_prerequisites")
        return dict(self.prerequisites)

    # ----- jobs --------------------------------------------------------

    def _kick(self, action: str) -> dict[str, Any]:
        if self.job_running:
            kind = self.job_kind or "another command"
            return {"ok": False,
                    "error": f"a {kind} is already in progress — wait for it to finish"}
        self.job_running = True
        self.job_kind = action
        self.calls.append(f"{action}_minikube")
        result = dict(self.job_result)
        result.setdefault("action", action)
        return result

    def start_minikube(self) -> dict[str, Any]:
        return self._kick("start")

    def stop_minikube(self) -> dict[str, Any]:
        return self._kick("stop")

    def delete_minikube(self) -> dict[str, Any]:
        return self._kick("delete")

    def finish_job(self, ok: bool = True, error: str | None = None) -> None:
        """Simulate the job thread completing (fires `on_job_done`)."""
        action = self.job_kind
        self.job_running = False
        self.job_kind = None
        self.on_job_done({"ok": ok, "error": error, "action": action})

    # ----- installs ----------------------------------------------------

    def install_tool(self, key: str) -> dict[str, Any]:
        if self.job_running:
            kind = self.job_kind or "another job"
            return {"ok": False, "log": "",
                    "error": f"a {kind} is already in progress — wait for it to finish"}
        self.calls.append(f"install_tool:{key}")
        return self._simulate_install(f"tool {key}")

    def install_all(self) -> dict[str, Any]:
        if self.job_running:
            kind = self.job_kind or "another job"
            return {"ok": False, "installed": [], "failed": [], "skipped": 0,
                    "error": f"a {kind} is already in progress — wait for it to finish"}
        self.calls.append("install_all")
        return self._simulate_install("every tool")

    def _simulate_install(self, what: str) -> dict[str, Any]:
        """Stream two lines like the real installer, then return its summary."""
        if self.install_delay:
            time.sleep(self.install_delay)
        self.on_log(f"$ installing {what}")
        self.on_log("done")
        return dict(self.install_result)

    # ----- job state ---------------------------------------------------

    def is_job_running(self) -> bool:
        return self.job_running

    def is_install_active(self) -> bool:
        return False


@pytest.fixture
def fake_service() -> FakeService:
    return FakeService()
