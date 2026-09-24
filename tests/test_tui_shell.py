"""Phase 2 gate: the TUI shell launches, renders, cycles focus, helps, quits.

Every test drives a real `KubbyApp` through Textual's pilot, with a
`FakeService` so nothing touches the host.
"""

from __future__ import annotations

import threading

from textual.binding import Binding
from textual.widgets import Static

from kubby.tui.app import KubbyApp, APP_KEYS, render_status
from kubby.tui.panels import (
    ClusterPanel,
    ImagesPanel,
    LogPanel,
    MinikubePanel,
    ToolsPanel,
    format_size,
)
from kubby.tui.popups import HelpScreen
from helpers import wait_until

PANEL_IDS = ("minikube", "tools", "images", "cluster")


class TestShell:
    async def test_startup_renders_every_region(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: fake_service.calls.count("get_cluster_info") > 0)
            await wait_until(lambda: app.system)

            for widget_id in ("status", "keybar", "minikube", "tools", "images", "cluster"):
                assert app.query_one(f"#{widget_id}") is not None
            assert isinstance(app.query_one("#log"), LogPanel)

            # Focus starts on the first panel, and it really has focus.
            assert app.screen.focused is app.query_one(MinikubePanel)
            assert app.screen.focused.has_focus

            # The status line is populated from the service.
            status = app.query_one("#status", Static)
            assert "minikube" in str(status.content)
            assert "v1.30.1" in str(status.content)
            assert "1/1 nodes" in str(status.content)
            assert "APT" in str(status.content)

    async def test_focus_cycles_through_panels(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)

            visited = [app.screen.focused.id]
            for _ in range(len(PANEL_IDS)):
                await pilot.press("tab")
                await pilot.pause()
                visited.append(app.screen.focused.id)
            assert visited == list(PANEL_IDS) + [PANEL_IDS[0]]

            # shift+tab walks backwards.
            await pilot.press("shift+tab")
            await pilot.pause()
            assert app.screen.focused.id == PANEL_IDS[-1]

            # The focused panel is visually distinguished (lazygit style).
            focused = app.screen.focused
            blurred = app.query_one(MinikubePanel)
            assert blurred is not focused
            focused_border = focused.styles.border.top[1]
            blurred_border = blurred.styles.border.top[1]
            assert focused_border is not None and blurred_border is not None
            assert focused_border != blurred_border

    async def test_help_opens_and_shields_quit(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)

            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

            body = app.screen.query_one("#help-body", Static)
            text = str(body.content)
            for key, action, description, _show in APP_KEYS:
                display = app.get_key_display(Binding(key, action, description))
                assert f'"{display}"' in text, key
                assert description in text, description

            # `q` closes the overlay — it must NOT quit the app.
            await pilot.press("q")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
            assert app.is_running

            # ...and now it does quit.
            await pilot.press("q")
            assert await wait_until(lambda: not app.is_running)

    async def test_recheck_refetches_state(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            first_calls = list(fake_service.calls)
            assert "get_cluster_info" in first_calls

            fake_service.cluster["version"] = "v1.31.0"
            await pilot.press("R")
            await wait_until(lambda: app.cluster.get("version") == "v1.31.0")

            status = app.query_one("#status", Static)
            assert "v1.31.0" in str(status.content)
            assert fake_service.calls.count("get_cluster_info") == 2

    async def test_panels_render_service_data(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.images and app.tools)

            tools = app.query_one(ToolsPanel)
            prompts = [str(option.prompt) for option in tools.options]
            assert any("✓" in p and "minikube" in p for p in prompts)
            assert any("✗" in p and "helm" in p for p in prompts)

            cluster = app.query_one(ClusterPanel)
            labels = [str(node.label) for node in cluster.root.children]
            assert labels == ["default  1 pod", "kube-system  2 pods"]
            assert cluster.border_subtitle == "1/1 ready"

            images = app.query_one(ImagesPanel)
            image_prompts = [str(option.prompt) for option in images.options]
            assert "nginx:alpine" in image_prompts[0]
            assert "120.0MB" in image_prompts[0]

            minikube = app.query_one(MinikubePanel)
            assert "running" in str(minikube.query_one("#minikube-body").content)

    async def test_log_lines_arrive_from_service_threads(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.system)
            panel = app.query_one(LogPanel)

            # From the UI thread (direct)...
            fake_service.on_log("first line")
            # ...and from a foreign thread, like a real job's reader.
            thread = threading.Thread(target=lambda: fake_service.on_log("second line"))
            thread.start()
            thread.join()

            await wait_until(lambda: len(panel.history) == 2)
            assert panel.history == ["first line", "second line"]
            assert panel.display is False  # idle → hidden (rule 5)

    async def test_empty_states_when_the_host_has_nothing(self, fake_service):
        fake_service.cluster = {
            "running": False, "error": "kubectl not found on PATH",
            "context": None, "version": None, "nodes": [],
            "namespaces": [], "pod_count": 0,
        }
        fake_service.tools = []
        fake_service.images = []
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.system)

            status = app.query_one("#status", Static)
            assert "no cluster" in str(status.content)
            labels = [str(node.label) for node in app.query_one(ClusterPanel).root.children]
            assert labels == ["no cluster"]
            assert "no tools" in str(
                next(iter(app.query_one(ToolsPanel).options)).prompt
            )
            assert "no local images" in str(
                next(iter(app.query_one(ImagesPanel).options)).prompt
            )


class TestRenderHelpers:
    def test_status_line_without_nodes_or_context(self):
        text = str(render_status(
            {"package_manager_label": "APT", "elevation": "sudo (shell fallback)"},
            {"running": False, "error": "nope"},
        ))
        assert "no cluster" in text
        assert "APT + sudo" in text
        assert "ctx:" not in text and "nodes" not in text  # nothing to show yet

    def test_format_size(self):
        assert format_size(125_829_120) == "120.0MB"
        assert format_size(2_097_152) == "2.0MB"
        assert format_size(512) == "512B"
        assert format_size("1.5GB") == "1.5GB"
        assert format_size("") == ""
        assert format_size(None) == ""
