"""The edit path through the real TUI: approval, fetch, diff, confirm, apply.

Driven with Textual's pilot, like the other shell tests. The service is the
fake, so these tests are about the *wiring*: which call happens, in what
order, and what the editor shows. The kubectl argv and exit codes are pinned
in ``test_resources.py`` and ``test_service.py``.
"""

from __future__ import annotations

from textual.widgets import Static, TextArea

from conftest import FakeService
from helpers import wait_until
from kubby.tui.app import KubbyApp
from kubby.tui.panels import GraphPanel
from kubby.tui.popups import (
    ConfirmModal,
    DiffConfirmModal,
    EditorScreen,
    HelpScreen,
)


async def _cursor_on_an_object(app: KubbyApp, pilot) -> GraphPanel:
    """Wait for the picture, focus it, and put the cursor on a real object."""
    panel = app.query_one(GraphPanel)
    assert await wait_until(lambda: panel.placement is not None)
    panel.focus()
    await pilot.pause()
    for _ in range(8):
        if panel.selected_ref() is not None:
            return panel
        await pilot.press("j")
        await pilot.pause()
    assert panel.selected_ref() is not None
    return panel


async def _approve_context(app: KubbyApp, pilot) -> None:
    """Press `e`, then approve the exact context the modal names."""
    await pilot.press("e")
    assert await wait_until(lambda: isinstance(app.screen, ConfirmModal))
    await pilot.press("y")


async def _open_editor(app: KubbyApp, pilot):
    panel = await _cursor_on_an_object(app, pilot)
    ref = panel.selected_ref()
    assert ref is not None
    await _approve_context(app, pilot)
    assert await wait_until(lambda: isinstance(app.screen, EditorScreen))
    return ref


class TestEditApproval:
    async def test_edit_is_not_offered_until_the_cursor_is_on_an_object(
        self, fake_service
    ):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            panel = await _cursor_on_an_object(app, pilot)
            # Put it back to the default, no-cursor state. Nothing is under
            # the cursor, so the service must not even be asked for a context.
            panel.selected = None
            panel.render_picture()
            await pilot.pause()
            assert panel.selected_ref() is None
            # A check_action of False removes the binding entirely, so there
            # is no `e` to press and nothing to grey out.
            assert "e" not in app.active_bindings

            calls_before = list(fake_service.calls)
            await pilot.press("e")
            await pilot.pause()
            assert list(fake_service.calls) == calls_before

            await pilot.press("j")
            await pilot.pause()
            assert panel.selected_ref() is not None
            assert app.active_bindings["e"].enabled is True

    async def test_every_edit_names_the_context_and_waits_for_approval(
        self, fake_service
    ):
        fake_service.context_result = {
            "ok": True,
            "context": "staging-cluster",
            "error": None,
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            panel = await _cursor_on_an_object(app, pilot)
            assert panel.selected_ref() is not None

            await pilot.press("e")
            assert await wait_until(lambda: isinstance(app.screen, ConfirmModal))
            message = str(
                app.screen.query_one("#confirm-message", Static).content
            )
            assert "kubectl will act on context: staging-cluster" in message
            # Nothing has been fetched yet: approval comes first.
            assert fake_service.yaml_requests == []

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmModal)
            assert fake_service.yaml_requests == []

            # A second edit asks again; a context name is not a standing yes.
            await pilot.press("e")
            assert await wait_until(lambda: isinstance(app.screen, ConfirmModal))
            assert fake_service.calls.count("get_current_context") == 2
            assert fake_service.yaml_requests == []

    async def test_an_unreadable_context_refuses_the_editor(self, fake_service):
        fake_service.context_result = {
            "ok": False,
            "context": None,
            "error": "error: current-context is not set",
        }
        app = KubbyApp(service=fake_service)
        errors: list[tuple[str, str, str]] = []

        def record_notify(message, *, title="", severity="information", **kwargs):
            errors.append((str(message), title, severity))

        async with app.run_test(size=(100, 40)) as pilot:
            app.notify = record_notify
            await _cursor_on_an_object(app, pilot)
            await pilot.press("e")

            assert await wait_until(lambda: bool(errors))
            message, title, severity = errors[-1]
            assert "current-context is not set" in message
            assert title == "cannot edit"
            assert severity == "error"
            assert not isinstance(app.screen, (ConfirmModal, EditorScreen))
            assert fake_service.yaml_requests == []


class TestTheEditor:
    async def test_it_opens_with_the_fetched_yaml_and_the_exact_context(
        self, fake_service
    ):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            ref = await _open_editor(app, pilot)
            editor = app.screen
            assert isinstance(editor, EditorScreen)

            assert fake_service.yaml_requests == [ref]
            title = str(editor.query_one("#editor-title", Static).content)
            assert f"{ref.kind.lower()}/{ref.name}" in title

            context = str(editor.query_one("#editor-context", Static).content)
            assert f"namespace {ref.namespace}" in context
            assert "context minikube" in context

            body = editor.query_one("#editor-body", TextArea)
            assert body.text == fake_service.resource_yaml

    async def test_escape_closes_without_diffing_or_applying(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_editor(app, pilot)
            editor = app.screen
            assert isinstance(editor, EditorScreen)
            editor.query_one("#editor-body", TextArea).text = "kind: ConfigMap\n"

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, EditorScreen)
            assert fake_service.diffed == []
            assert fake_service.applied == []

    async def test_a_no_op_edit_closes_without_applying(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_editor(app, pilot)
            editor = app.screen
            assert isinstance(editor, EditorScreen)
            original = editor.query_one("#editor-body", TextArea).text

            await pilot.press("ctrl+s")
            assert await wait_until(lambda: not isinstance(app.screen, EditorScreen))

            assert fake_service.diffed == [original]
            assert fake_service.applied == []

    async def test_a_changed_diff_is_shown_and_only_applies_on_confirm(
        self, fake_service
    ):
        fake_service.diff_result = {
            "ok": True,
            "changed": True,
            "diff": "--- live\n+++ edited\n-replicas: 1\n+replicas: 3\n",
            "error": None,
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_editor(app, pilot)
            body = app.screen.query_one("#editor-body", TextArea)
            body.text = fake_service.resource_yaml.replace("replicas: 1", "replicas: 3")

            await pilot.press("ctrl+s")
            assert await wait_until(lambda: isinstance(app.screen, DiffConfirmModal))
            diff = str(app.screen.query_one("#diff-body", Static).content)
            assert "+replicas: 3" in diff
            # Showing the diff must not write anything.
            assert fake_service.applied == []

            # Backing out returns to the editor with the edit intact.
            await pilot.press("n")
            await pilot.pause()
            assert isinstance(app.screen, EditorScreen)
            assert app.screen.query_one("#editor-body", TextArea).text == body.text
            assert fake_service.applied == []

            # A second save, this time confirmed, is the only apply.
            await pilot.press("ctrl+s")
            assert await wait_until(lambda: isinstance(app.screen, DiffConfirmModal))
            await pilot.press("y")
            assert await wait_until(lambda: bool(fake_service.applied))
            assert fake_service.applied == [body.text]
            assert await wait_until(lambda: not isinstance(app.screen, EditorScreen))

            # The picture is re-read after a successful write.
            assert await wait_until(
                lambda: fake_service.calls.count("get_cluster_info") >= 2
            )

    async def test_a_failed_diff_keeps_the_editor_open_with_the_error(
        self, fake_service
    ):
        fake_service.diff_result = {
            "ok": False,
            "changed": False,
            "diff": "",
            "error": "error: no server",
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await _open_editor(app, pilot)
            editor = app.screen
            assert isinstance(editor, EditorScreen)

            await pilot.press("ctrl+s")
            error = editor.query_one("#editor-error", Static)
            assert await wait_until(lambda: "no server" in str(error.content))
            assert isinstance(app.screen, EditorScreen)
            assert fake_service.applied == []

    async def test_the_help_documents_the_edit_key(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.cluster)
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            text = str(app.screen.query_one("#help-body", Static).content)
            assert '"e"' in text
            assert "edit the object under the cursor (graph)" in text


def test_fake_service_defaults_are_the_happy_path():
    """A guard on the fixture the tests above read as the baseline."""
    service = FakeService()
    assert service.get_current_context()["ok"] is True
    assert service.diff_resource("kind: Pod\n")["changed"] is False
    assert service.apply_resource("kind: Pod\n")["ok"] is True
