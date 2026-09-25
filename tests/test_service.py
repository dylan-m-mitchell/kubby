"""Tests for `kubby.service.KubbyService` — the UI-agnostic domain layer.

Everything here runs against fakes: no minikube, no kubectl, no podman, no
network. The point is to pin the behaviors the TUI will lean on — the
single-job lock, callback ordering, and the "completion always fires"
contract. Tool installation is gone, so there is nothing here that elevates.
"""
from __future__ import annotations

import io
import json
import subprocess
import threading

import pytest

from kubby import service as service_mod
from kubby.service import KubbyService


# ---------------------------------------------------------------------------
# helpers / fakes
# ---------------------------------------------------------------------------


def collect():
    """Return (on_log, on_job_done) recorders."""
    lines: list[str] = []
    payloads: list[dict] = []
    return lines, payloads, (lines.append), (payloads.append)


class FakeProc:
    """Configurable `subprocess.Popen` stand-in.

    ``block`` (an Event) makes ``wait(timeout=...)`` park until released, so
    a test can observe the job as "still running".
    """

    def __init__(
        self,
        stdout: str = "",
        stderr: str = "",
        rc: int = 0,
        block: threading.Event | None = None,
    ) -> None:
        self.stdout = io.StringIO(stdout)
        self.stderr = io.StringIO(stderr)
        self.rc = rc
        self.block = block
        self.killed = False

    def wait(self, timeout: float | None = None) -> int:
        if self.block is not None:
            ok = self.block.wait(30 if timeout is None else min(timeout, 30))
            if not ok:
                raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout or 0)
        return self.rc

    def kill(self) -> None:
        self.killed = True
        if self.block is not None:
            self.block.set()


class HangingProc(FakeProc):
    """`wait(timeout=...)` always times out; `wait()` after kill returns."""

    def __init__(self) -> None:
        super().__init__()
        self._waits = 0

    def wait(self, timeout: float | None = None) -> int:
        if timeout is not None:
            self._waits += 1
            raise subprocess.TimeoutExpired(cmd="minikube", timeout=timeout)
        return 0


def start_blocking_job(svc: KubbyService, monkeypatch, action="start"):
    """Start a minikube job that stays alive until the returned Event is set."""
    release = threading.Event()
    proc = FakeProc(stdout="line one\nline two\n", block=release)
    monkeypatch.setattr(service_mod.subprocess, "Popen", lambda *a, **k: proc)
    result = getattr(svc, f"{action}_minikube")()
    assert result["ok"] is True, result
    return release


def wait_until(pred, timeout=5.0) -> bool:
    deadline = threading.Event()
    waited = 0.0
    while waited < timeout:
        if pred():
            return True
        deadline.wait(0.02)
        waited += 0.02
    return pred()


# ---------------------------------------------------------------------------
# minikube jobs
# ---------------------------------------------------------------------------


class TestMinikubeJobs:
    def test_job_streams_logs_in_order_then_reports_done(self, monkeypatch):
        lines, payloads, on_log, on_done = collect()
        release = threading.Event()
        proc = FakeProc(stdout="alpha\nbeta\n", stderr="gamma\n", block=release)
        monkeypatch.setattr(service_mod.subprocess, "Popen", lambda *a, **k: proc)
        svc = KubbyService(on_log=on_log, on_job_done=on_done)

        assert svc.start_minikube() == {"ok": True, "started": True, "action": "start"}
        assert svc.is_job_running() is True
        assert svc.job_kind == "start"

        release.set()
        assert wait_until(lambda: not svc.is_job_running())

        # The `$ argv` line leads, then both streams' lines in arrival order,
        # then the success marker.
        assert lines[0].startswith("$ minikube start")
        assert "alpha" in lines and "beta" in lines and "gamma" in lines
        assert lines.index("alpha") < lines.index("beta")
        assert lines[-1] == "✓ minikube start succeeded"

        assert payloads == [{"ok": True, "error": None, "action": "start"}]
        assert svc.job_kind is None

    def test_second_job_rejected_while_first_is_alive(self, monkeypatch):
        svc = KubbyService()
        release = threading.Event()
        proc = FakeProc(block=release)
        monkeypatch.setattr(service_mod.subprocess, "Popen", lambda *a, **k: proc)
        first = svc.start_minikube()
        assert first["ok"] is True

        second = svc.start_minikube()
        assert second["ok"] is False
        assert "already in progress" in second["error"]
        assert "start" in second["error"]

        third = svc.delete_minikube()
        assert third["ok"] is False

        release.set()
        assert wait_until(lambda: not svc.is_job_running())
        # Slot is free again.
        monkeypatch.setattr(
            service_mod.subprocess, "Popen", lambda *a, **k: FakeProc()
        )
        assert svc.stop_minikube()["ok"] is True
        assert wait_until(lambda: not svc.is_job_running())

    def test_done_fires_on_nonzero_exit(self, monkeypatch):
        lines, payloads, on_log, on_done = collect()
        monkeypatch.setattr(
            service_mod.subprocess, "Popen",
            lambda *a, **k: FakeProc(stderr="boom\n", rc=3),
        )
        svc = KubbyService(on_log=on_log, on_job_done=on_done)
        svc.start_minikube()
        assert wait_until(lambda: not svc.is_job_running())
        assert payloads == [{"ok": False, "error": "exit code 3", "action": "start"}]
        assert any("failed (exit 3)" in line for line in lines)

    def test_failure_names_the_stage_and_minikubes_own_advice(self, monkeypatch):
        # Real output from a genuine failure: minikube repeats the error
        # three times inside box-drawing banners, and the one actionable
        # sentence is buried mid-wall. The summary is what makes that
        # readable without scrolling.
        stderr = (
            "* Failed to start podman container. "
            'Running "minikube delete" may fix it: driver start: exit status 125\n'
            "stderr:\n"
            "Error: no such container\n"
            "X Exiting due to GUEST_PROVISION: error provisioning guest: "
            "Failed to start host\n"
        )
        lines, payloads, on_log, on_done = collect()
        monkeypatch.setattr(
            service_mod.subprocess, "Popen",
            lambda *a, **k: FakeProc(stderr=stderr, rc=80),
        )
        svc = KubbyService(on_log=on_log, on_job_done=on_done)
        svc.start_minikube()
        assert wait_until(lambda: not svc.is_job_running())

        assert payloads[0]["error"] == "exit code 80"
        assert any(
            "failed at: GUEST_PROVISION: error provisioning guest" in line
            for line in lines
        ), lines
        assert any("try: minikube delete, then start again" in line for line in lines)

    def test_failure_summary_is_omitted_when_minikube_says_nothing_useful(
        self, monkeypatch
    ):
        # A plain failure must not gain invented advice.
        lines, _payloads, on_log, on_done = collect()
        monkeypatch.setattr(
            service_mod.subprocess, "Popen",
            lambda *a, **k: FakeProc(stderr="boom\n", rc=3),
        )
        svc = KubbyService(on_log=on_log, on_job_done=on_done)
        svc.start_minikube()
        assert wait_until(lambda: not svc.is_job_running())
        assert not any("failed at:" in line or "try:" in line for line in lines)

    def test_summary_does_not_mutate_the_streamed_output(self, monkeypatch):
        # The full log is the record; the summary is additive only.
        stderr = "X Exiting due to HOST_PROVISION: something broke\n"
        lines, _payloads, on_log, on_done = collect()
        monkeypatch.setattr(
            service_mod.subprocess, "Popen",
            lambda *a, **k: FakeProc(stderr=stderr, rc=7),
        )
        svc = KubbyService(on_log=on_log, on_job_done=on_done)
        svc.start_minikube()
        assert wait_until(lambda: not svc.is_job_running())
        assert any(line == stderr.strip() for line in lines), lines

    def test_done_fires_on_timeout(self, monkeypatch):
        _lines, payloads, on_log, on_done = collect()
        monkeypatch.setattr(
            service_mod.subprocess, "Popen", lambda *a, **k: HangingProc()
        )
        svc = KubbyService(on_log=on_log, on_job_done=on_done)
        svc.start_minikube()
        assert wait_until(lambda: not svc.is_job_running())
        assert payloads[0]["ok"] is False
        assert "timed out" in payloads[0]["error"]

    def test_done_fires_when_binary_missing(self, monkeypatch):
        _lines, payloads, on_log, on_done = collect()

        def raise_missing(*a, **k):
            raise FileNotFoundError(2, "No such file or directory")

        monkeypatch.setattr(service_mod.subprocess, "Popen", raise_missing)
        svc = KubbyService(on_log=on_log, on_job_done=on_done)
        svc.start_minikube()
        assert wait_until(lambda: not svc.is_job_running())
        assert payloads == [
            {"ok": False, "error": "minikube binary not found on PATH", "action": "start"}
        ]

    def test_unknown_action_rejected(self):
        svc = KubbyService()
        assert svc._kick_job("explode") == {"ok": False, "error": "unknown action: 'explode'"}

    def test_raising_consumer_cannot_kill_a_job(self, monkeypatch):
        """A buggy UI callback must not abort the job thread."""
        payloads: list[dict] = []

        def bad_log(line: str) -> None:
            raise RuntimeError("consumer exploded")

        release = threading.Event()
        monkeypatch.setattr(
            service_mod.subprocess, "Popen",
            lambda *a, **k: FakeProc(stdout="still here\n", block=release),
        )
        svc = KubbyService(on_log=bad_log, on_job_done=payloads.append)
        svc.start_minikube()
        release.set()
        assert wait_until(lambda: not svc.is_job_running())
        assert payloads and payloads[0]["ok"] is True


# ---------------------------------------------------------------------------
# cluster info
# ---------------------------------------------------------------------------


class TestClusterInfo:
    @staticmethod
    def _cp(stdout="", stderr="", rc=0):
        return subprocess.CompletedProcess(args=[], returncode=rc,
                                           stdout=stdout, stderr=stderr)

    @pytest.fixture
    def kubectl(self, monkeypatch):
        """Fake kubectl: nodes + namespaces answer, pods fails."""
        monkeypatch.setattr(service_mod.shutil, "which",
                            lambda name: "/usr/bin/kubectl" if name == "kubectl" else None)

        nodes = {"items": [{
            "metadata": {
                "name": "minikube",
                "labels": {"node-role.kubernetes.io/control-plane": ""},
            },
            "status": {"conditions": [{"type": "Ready", "status": "True"}]},
        }]}
        namespaces = {"items": [{"metadata": {"name": "default"}},
                                {"metadata": {"name": "kube-system"}}]}

        def fake_run(args, **kwargs):
            joined = " ".join(args)
            if joined == "kubectl config current-context":
                return self._cp(stdout="minikube\n")
            if joined.startswith("kubectl version"):
                return self._cp(stdout=json.dumps(
                    {"serverVersion": {"gitVersion": "v1.30.1"}}))
            if "get nodes" in joined:
                return self._cp(stdout=json.dumps(nodes))
            if "get namespaces" in joined:
                return self._cp(stdout=json.dumps(namespaces))
            if "get pods" in joined:
                return self._cp(rc=1, stderr="error: the server is currently unable")
            return self._cp(rc=1, stderr="unexpected")

        monkeypatch.setattr(service_mod.subprocess, "run", fake_run)
        return fake_run

    def test_tolerates_failing_pod_query(self, kubectl):
        svc = KubbyService()
        info = svc.get_cluster_info()

        assert info["running"] is True
        assert info["context"] == "minikube"
        assert info["version"] == "v1.30.1"
        # nodes + namespaces survived even though `get pods -A` failed
        assert [n["name"] for n in info["nodes"]] == ["minikube"]
        assert info["nodes"][0]["status"] == "Ready"
        assert "control-plane" in info["nodes"][0]["roles"]
        assert [ns["name"] for ns in info["namespaces"]] == ["default", "kube-system"]
        assert info["pod_count"] == 0
        assert all(ns["pods"] == [] for ns in info["namespaces"])

    def test_missing_kubectl(self, monkeypatch):
        monkeypatch.setattr(service_mod.shutil, "which", lambda name: None)
        assert KubbyService().get_cluster_info() == {
            "running": False, "error": "kubectl not found on PATH",
        }

    def test_unreachable_cluster_keeps_context(self, monkeypatch):
        monkeypatch.setattr(service_mod.shutil, "which", lambda name: "/usr/bin/kubectl")

        def fake_run(args, **kwargs):
            joined = " ".join(args)
            if joined == "kubectl config current-context":
                return TestClusterInfo._cp(stdout="minikube\n")
            if joined.startswith("kubectl version"):
                return TestClusterInfo._cp(rc=1, stderr="connection refused")
            return TestClusterInfo._cp(rc=1, stderr="")

        monkeypatch.setattr(service_mod.subprocess, "run", fake_run)
        info = KubbyService().get_cluster_info()
        assert info["running"] is False
        assert info["error"] == "connection refused"
        assert info["context"] == "minikube"


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


class TestMisc:
    def test_list_local_images_forwards_query_and_env(self, monkeypatch):
        seen: dict = {}

        def fake_search_local(query=None, env=None):
            seen["query"] = query
            seen["env"] = env
            return [{"name": "nginx:alpine", "local": True}]

        monkeypatch.setattr(service_mod.images_mod, "search_local", fake_search_local)
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/real/libs")

        svc = KubbyService()
        out = svc.list_local_images("nginx")

        assert out == [{"name": "nginx:alpine", "local": True}]
        assert seen["query"] == "nginx"
        assert seen["env"]["LD_LIBRARY_PATH"] == "/real/libs"

        svc.list_local_images()
        assert seen["query"] is None

    def test_subprocess_env_restores_original_ld_library_path(self, monkeypatch):
        monkeypatch.setenv("LD_LIBRARY_PATH", "/bundled")
        monkeypatch.setenv("LD_LIBRARY_PATH_ORIG", "/original")
        env = KubbyService._subprocess_env()
        assert env["LD_LIBRARY_PATH"] == "/original"

        monkeypatch.delenv("LD_LIBRARY_PATH_ORIG")
        env = KubbyService._subprocess_env()
        assert "LD_LIBRARY_PATH" not in env

    def test_system_info_shape(self):
        info = KubbyService().system_info()
        assert set(info) == {
            "platform", "release", "python",
            "package_manager_label", "elevation",
        }
        assert info["python"]  # non-empty

    def test_settings_round_trip_through_service(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            service_mod.settings_mod, "CONFIG_DIR", tmp_path / "kubby"
        )
        monkeypatch.setattr(
            service_mod.settings_mod, "CONFIG_FILE",
            tmp_path / "kubby" / "settings.json",
        )
        svc = KubbyService()
        loaded = svc.get_minikube_settings()
        assert "minikube" in loaded
        assert svc.save_minikube_settings({"minikube": loaded["minikube"]}) == {"ok": True}
        assert svc.save_minikube_settings(["not a dict"]) == {  # type: ignore[arg-type]
            "ok": False,
            "error": "settings must be a JSON object",
        }
