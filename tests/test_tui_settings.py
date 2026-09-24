"""Phase 4 gate: the settings screen and the preflight → settings hand-off.

The settings form mirrors the GUI's modal, so these tests assert the same
things the web form guarantees: values prefill, bad numbers are rejected
with the GUI's own messages, good ones are persisted with every untouched
field preserved, and a failed write stays on screen.
"""

from __future__ import annotations

from textual.widgets import Checkbox, Input, Select, Static

from helpers import wait_until
from kubby.tui.app import KubbyApp
from kubby.tui.popups import PrereqModal, SettingsScreen
from kubby.tui.panels import MinikubePanel


def keybar(app: KubbyApp) -> str:
    return str(app.query_one("#keybar", Static).content)


def error_text(app: KubbyApp) -> str:
    return str(app.screen.query_one("#settings-error", Static).content)


async def open_settings(app: KubbyApp, pilot) -> SettingsScreen:
    """Press `o` on the (default-focused) minikube panel."""
    await pilot.press("o")
    assert await wait_until(
        lambda: isinstance(app.screen, SettingsScreen)
    ), f"settings never opened (screen={app.screen.__class__.__name__})"
    return app.screen


class TestOpenAndCancel:
    async def test_key_and_prefill(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            # Listed with the panel's other keys, always (greyed when busy).
            assert '"o" settings' in keybar(app)

            await open_settings(app, pilot)
            assert "get_minikube_settings" in fake_service.calls
            # Prefilled from FakeService's persisted settings.
            assert app.screen.query_one("#set-driver", Select).value == "podman"
            assert app.screen.query_one("#set-cpus", Input).value == "4"
            assert app.screen.query_one("#set-memory", Input).value == "4000"
            assert app.screen.query_one("#set-rootless", Checkbox).value is False
            assert app.screen.query_one("#set-addon-default", Checkbox).value is False
            # The form says where it writes, like the GUI's modal does.
            assert "settings.json" in str(app.screen.query_one("#settings-path", Static).content)

    async def test_escape_writes_nothing(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)
            app.screen.query_one("#set-cpus", Input).value = "999"

            await pilot.press("escape")
            assert await wait_until(
                lambda: not isinstance(app.screen, SettingsScreen)
            )
            assert fake_service.settings_saved == []
            assert "save_minikube_settings" not in fake_service.calls

    async def test_is_greyed_while_a_job_runs(self, fake_service):
        fake_service.cluster["running"] = False  # `s` is only enabled when down
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("s")  # start → busy
            assert await wait_until(lambda: app.busy)

            app.query_one(MinikubePanel).focus()
            await pilot.pause()
            assert app.active_bindings["o"].enabled is False  # greyed, listed
            await pilot.press("o")
            await pilot.pause()
            assert not isinstance(app.screen, SettingsScreen)
            assert "get_minikube_settings" not in fake_service.calls


class TestValidation:
    async def test_bad_cpus_is_rejected_with_the_guis_message(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)
            app.screen.query_one("#set-cpus", Input).value = "many"

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert "CPUs must be a positive number" in error_text(app)
            assert isinstance(app.screen, SettingsScreen)  # still editing
            assert fake_service.settings_saved == []

    async def test_bad_memory_is_rejected_with_the_guis_message(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)
            app.screen.query_one("#set-cpus", Input).value = "2"  # fine
            app.screen.query_one("#set-memory", Input).value = "lots"

            await pilot.press("ctrl+s")
            await pilot.pause()
            assert 'Memory must look like "2g"' in error_text(app)
            assert isinstance(app.screen, SettingsScreen)
            assert fake_service.settings_saved == []

    async def test_blank_numbers_are_allowed(self, fake_service):
        # The GUI allows empty fields ("let minikube decide").
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)
            app.screen.query_one("#set-cpus", Input).value = ""
            app.screen.query_one("#set-memory", Input).value = ""

            await pilot.press("ctrl+s")
            # No error: the save is accepted and the form closes.
            assert await wait_until(lambda: fake_service.settings_saved)
            assert await wait_until(
                lambda: not isinstance(app.screen, SettingsScreen)
            )
            assert fake_service.settings_saved[-1]["minikube"]["cpus"] == ""


class TestSave:
    async def test_saves_edits_and_keeps_the_rest(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)
            app.screen.query_one("#set-cpus", Input).value = "8"
            app.screen.query_one("#set-addon-ingress", Checkbox).value = True

            await pilot.press("ctrl+s")
            assert await wait_until(lambda: fake_service.settings_saved)
            saved = fake_service.settings_saved[-1]["minikube"]
            assert saved["cpus"] == "8"
            assert saved["addons"] == ["ingress"]
            # Untouched fields survive the round trip.
            assert saved["driver"] == "podman"
            assert saved["memory"] == "4000"
            assert saved["rootless"] is False
            assert saved["kubernetes_version"] == ""
            # Success closes the form.
            assert await wait_until(
                lambda: not isinstance(app.screen, SettingsScreen)
            )
            # …and the world is re-read like any other job-ish change.
            assert fake_service.calls.count("get_minikube_settings") >= 1

    async def test_enter_in_a_text_field_saves_too(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)
            app.screen.query_one("#set-k8s", Input).value = "v1.31.0"
            app.screen.query_one("#set-k8s", Input).focus()
            await pilot.pause()

            await pilot.press("enter")
            assert await wait_until(lambda: fake_service.settings_saved)
            assert fake_service.settings_saved[-1]["minikube"][
                "kubernetes_version"
            ] == "v1.31.0"

    async def test_write_failure_stays_on_screen(self, fake_service):
        def explode(_payload):
            return {"ok": False, "error": "settings.json: read-only file system"}

        fake_service.save_minikube_settings = explode
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await open_settings(app, pilot)

            await pilot.press("ctrl+s")
            assert await wait_until(
                lambda: "read-only file system" in error_text(app)
            )
            assert isinstance(app.screen, SettingsScreen)
            assert fake_service.settings_saved == []
            # The save hint comes back so the user can try again.
            assert '"esc"' in str(app.screen.query_one("#settings-hints", Static).content)


class TestPreflightHandoff:
    async def test_prereq_modal_opens_settings(self, fake_service):
        fake_service.cluster["running"] = False  # `s` is only enabled when down
        fake_service.prerequisites = {
            "ok": False,
            "issues": ["docker is not installed — install it from the Tools panel."],
            "settings_path": "/tmp/settings.json",
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("s")  # start → preflight fails
            assert await wait_until(lambda: isinstance(app.screen, PrereqModal))
            text = str(app.screen.query_one("#prereq-issues", Static).content)
            assert "docker is not installed" in text

            await pilot.press("o")
            # The prereq modal is replaced, not left underneath.
            assert await wait_until(
                lambda: isinstance(app.screen, SettingsScreen)
            )
            assert not isinstance(app.screen, PrereqModal)

    async def test_prereq_modal_still_closes(self, fake_service):
        fake_service.cluster["running"] = False  # `s` is only enabled when down
        fake_service.prerequisites = {
            "ok": False,
            "issues": ["kubectl is not installed — install it from the Tools panel."],
            "settings_path": "/tmp/settings.json",
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(110, 44)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("s")
            assert await wait_until(lambda: isinstance(app.screen, PrereqModal))

            await pilot.press("escape")
            await wait_until(lambda: not isinstance(app.screen, PrereqModal))
            assert "get_minikube_settings" not in fake_service.calls
            assert not app.busy  # nothing was kicked off
