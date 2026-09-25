"""Phase 3 gate: panel actions, popups, and the log-dismissal rules.

Same harness as the shell tests — a real `KubbyApp` driven through
Textual's pilot with `FakeService` standing in for the host.
"""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Input, Static

from helpers import wait_until
from kubby.tui.app import KubbyApp
from kubby.tui.panels import ClusterPanel, ImagesPanel, LogPanel, MinikubePanel, ToolsPanel
from kubby.tui.popups import ConfirmModal, HelpScreen, PrereqModal


def keybar(app: KubbyApp) -> str:
    return str(app.query_one("#keybar", Static).content)


def status(app: KubbyApp) -> str:
    return str(app.query_one("#status", Static).content)


class TestClusterTree:
    async def test_pods_expand_under_their_namespaces(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.images)
            tree = app.query_one(ClusterPanel)
            default, kube_system = tree.root.children

            assert [str(node.label) for node in tree.root.children] == [
                "default  1 pod",
                "kube-system  2 pods",
            ]
            # Plan sketch: pods visible without a keypress (expanded by default).
            assert default.is_expanded and kube_system.is_expanded
            assert [str(p.label) for p in default.children] == ["web-2-def  Running"]
            assert [str(p.label) for p in kube_system.children] == [
                "coredns-7x  Running",
                "metrics-1  CrashLoopBackOff",
            ]

    async def test_pod_status_drives_the_colours(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.images)
            _default, kube_system = app.query_one(ClusterPanel).root.children
            healthy: Text = kube_system.children[0].label
            broken: Text = kube_system.children[1].label

            # The status suffix is styled; the pod name is not.
            assert healthy.spans[-1].style == "green"
            assert broken.spans[-1].style == "red"

    async def test_space_toggles_a_namespace(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            tree = app.query_one(ClusterPanel)
            tree.focus()
            await pilot.pause()
            assert tree.cursor_line == 0
            default = tree.root.children[0]

            await pilot.press("space")
            await pilot.pause()
            assert not default.is_expanded

            await pilot.press("space")
            await pilot.pause()
            assert default.is_expanded

            # enter expands too (Tree.auto_expand) and reports the selection.
            await pilot.press("enter")
            await pilot.pause()
            assert not default.is_expanded


class TestMinikubeActions:
    async def test_start_runs_preflight_then_starts_the_job(self, fake_service):
        fake_service.cluster["running"] = False
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            log = app.query_one(LogPanel)
            assert log.display is False  # idle → hidden (rule 5)

            await pilot.press("s")
            assert await wait_until(lambda: app.busy)
            assert "get_minikube_prerequisites" in fake_service.calls
            assert "start_minikube" in fake_service.calls
            assert (
                fake_service.calls.index("get_minikube_prerequisites")
                < fake_service.calls.index("start_minikube")
            )

            # The log opens for the job and the header says what's running.
            assert log.display is True and log.job_active
            assert "… start" in status(app)

            # While busy: the keys are greyed and a second press does nothing.
            assert app.active_bindings["s"].enabled is False
            calls_before = list(fake_service.calls)
            await pilot.press("s")
            await pilot.pause()
            assert list(fake_service.calls) == calls_before

            # Output streams from a service thread while the job runs.
            fake_service.on_log("$ minikube start --driver=podman")
            assert await wait_until(lambda: log.history[-1:] == ["$ minikube start --driver=podman"])
            assert '"x" hide log' in keybar(app)

            fake_service.finish_job(ok=True)
            assert await wait_until(lambda: not app.busy)
            assert log.display is False  # success → idle hides it again
            assert "start" not in status(app)

    async def test_preflight_failure_opens_the_modal_and_blocks_start(self, fake_service):
        fake_service.cluster["running"] = False
        fake_service.prerequisites = {
            "ok": False,
            "issues": ["no usable container driver", "~/.kube not writable"],
            "settings_path": "/home/u/.config/kubby/settings.json",
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)

            await pilot.press("s")
            assert await wait_until(lambda: isinstance(app.screen, PrereqModal))
            assert "start_minikube" not in fake_service.calls
            assert app.busy is False

            issues = str(app.screen.query_one("#prereq-issues", Static).content)
            assert "no usable container driver" in issues
            assert "~/.kube not writable" in issues

            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, PrereqModal)
            assert app.is_running

    async def test_stop_only_when_running(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            # Cluster is up by default: stop is offered, start is not.
            assert app.active_bindings["S"].enabled is True
            assert "s" not in app.active_bindings  # start hidden while running

            await pilot.press("S")
            assert await wait_until(lambda: "stop_minikube" in fake_service.calls)
            assert await wait_until(lambda: app.busy)

    async def test_delete_needs_a_second_key(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)

            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            assert "delete_minikube" not in fake_service.calls

            await pilot.press("n")  # back out
            await pilot.pause()
            assert not isinstance(app.screen, ConfirmModal)
            assert "delete_minikube" not in fake_service.calls

            await pilot.press("d")
            await pilot.pause()
            await pilot.press("d")  # the hint's own letter commits
            assert await wait_until(lambda: "delete_minikube" in fake_service.calls)
            assert await wait_until(lambda: app.busy)
            assert app.busy_label == "delete"

    async def test_delete_also_accepts_the_generic_yes(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("d")
            await pilot.pause()
            assert isinstance(app.screen, ConfirmModal)
            await pilot.press("y")
            assert await wait_until(lambda: "delete_minikube" in fake_service.calls)


class TestInstallActions:
    async def test_install_keys_are_greyed_out_while_a_job_runs(self, fake_service):
        fake_service.cluster["running"] = False
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("s")
            assert await wait_until(lambda: app.busy)

            app.query_one(ToolsPanel).focus()
            await pilot.pause()
            calls_before = list(fake_service.calls)
            await pilot.press("i", "I")
            await pilot.pause()
            assert list(fake_service.calls) == calls_before  # nothing installed
            assert app.active_bindings["i"].enabled is False  # greyed, still listed
            assert '"i" install' in keybar(app)

    async def test_install_streams_and_then_finishes(self, fake_service):
        fake_service.install_delay = 0.2  # a window where the UI is busy
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            tools = app.query_one(ToolsPanel)
            tools.focus()
            await pilot.pause()
            tools.highlighted = 1  # helm

            await pilot.press("i")
            assert await wait_until(lambda: app.busy)
            log = app.query_one(LogPanel)
            assert log.display is True and log.job_active
            assert "install_tool:helm" in fake_service.calls
            # Greyed while it streams — asserted inside the busy window.
            assert app.active_bindings["i"].enabled is False
            assert '"i" install' in keybar(app)

            assert await wait_until(lambda: len(log.history) >= 2)
            assert log.history[0] == "$ installing tool helm"

            assert await wait_until(lambda: not app.busy)
            assert log.display is False
            # _finish_job clears busy *before* spawning the refresh worker,
            # so the count can still be 1 for a moment — poll for it rather
            # than racing the thread the way a bare assert does.
            assert await wait_until(lambda: fake_service.calls.count("get_status") >= 2)

    async def test_install_all_covers_every_tool(self, fake_service):
        fake_service.install_delay = 0.1
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            app.query_one(ToolsPanel).focus()
            await pilot.pause()

            await pilot.press("I")
            assert await wait_until(lambda: "install_all" in fake_service.calls)
            assert await wait_until(lambda: not app.busy)

    async def test_a_failed_install_pins_the_log_open(self, fake_service):
        fake_service.install_result = {
            "ok": False, "installed": [], "failed": ["helm"], "skipped": 0,
            "error": "checksum mismatch",
        }
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            tools = app.query_one(ToolsPanel)
            tools.focus()
            await pilot.pause()
            tools.highlighted = 1  # helm

            await pilot.press("i")
            assert await wait_until(lambda: not app.busy and "install_tool:helm" in fake_service.calls)

            log = app.query_one(LogPanel)
            assert log.display is True and log.pinned  # rule 5: failure pins

            await pilot.press("x")
            await pilot.pause()
            assert log.display is False and not log.pinned  # rule 3: hide clears


class TestLogDismissal:
    """The semantics the GUI regression test pinned down, re-proved in the TUI."""

    async def test_dismissal_is_scoped_to_the_current_job(self, fake_service):
        fake_service.cluster["running"] = False
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            log = app.query_one(LogPanel)

            assert log.display is False  # rule 5: idle → hidden

            await pilot.press("s")
            assert await wait_until(lambda: app.busy)
            assert log.display is True  # rule 5: busy → visible

            await pilot.press("x")
            await pilot.pause()
            assert log.display is False  # rule 1: dismissed for this job
            assert "x" not in app.active_bindings  # nothing left to dismiss

            # Lines keep recording while the panel is hidden.
            fake_service.on_log("boom: driver not found")
            assert await wait_until(lambda: "boom: driver not found" in log.history)

            fake_service.finish_job(ok=False, error="driver not found")
            assert await wait_until(lambda: not app.busy)
            assert log.display is False  # rule 2: dismissal beats the failure pin
            assert log.pinned is False  # rule 3: hiding cleared the pin

            # rule 4: the next job starts with a clean slate.
            await pilot.press("s")
            assert await wait_until(lambda: app.busy)
            assert log.display is True
            assert log.dismissed is False

    async def test_a_failed_job_pins_the_log_when_it_was_not_dismissed(self, fake_service):
        fake_service.cluster["running"] = False
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            log = app.query_one(LogPanel)

            await pilot.press("s")
            assert await wait_until(lambda: app.busy)
            fake_service.finish_job(ok=False, error="exit status 1")
            assert await wait_until(lambda: not app.busy)

            assert log.display is True and log.pinned  # rule 5

            await pilot.press("x")
            await pilot.pause()
            assert log.display is False  # rule 3
            assert not log.pinned


class TestImageFilter:
    async def test_slash_filters_live_and_text_keys_do_not_leak(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            images = app.query_one(ImagesPanel)
            images.focus()
            await pilot.pause()
            assert len(images.options) == 2

            await pilot.press("/")
            await pilot.pause()
            bar = app.query_one("#filter-bar", Input)
            assert bar.display is True
            assert app.screen.focused is bar
            # The panel's own "/" binding is consumed by the input while it
            # has focus, so it can't re-trigger itself mid-query — and the
            # keybar switches to the key that actually closes the bar.
            assert '"/" filter' not in keybar(app)
            assert '"esc" close filter' in keybar(app)

            # q / x / R are ordinary characters while typing — none of them
            # may quit, dismiss the log or re-check the host.
            calls_before = list(fake_service.calls)
            await pilot.press("q", "u", "a", "y")
            assert app.is_running
            assert list(fake_service.calls) == calls_before
            assert await wait_until(lambda: app.image_filter == "quay")

            prompts = [str(option.prompt) for option in images.options]
            assert prompts == ["busybox:latest  2.0MB"]

            await pilot.press("enter")  # keep the filter, leave the bar
            await pilot.pause()
            assert bar.display is False
            assert app.screen.focused is images
            assert app.image_filter == "quay"
            assert len(images.options) == 1

            await pilot.press("/")  # re-open, then esc drops the query
            await pilot.pause()
            await pilot.press("escape")
            assert await wait_until(lambda: app.image_filter == "")
            assert len(images.options) == 2
            assert app.screen.focused is images

    async def test_no_match_gives_an_explanation(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            app.query_one(ImagesPanel).focus()
            await pilot.pause()

            await pilot.press("/")
            await pilot.press("z", "z", "z")
            assert await wait_until(
                lambda: len(app.query_one(ImagesPanel).options) == 1
            )
            prompt = str(next(iter(app.query_one(ImagesPanel).options)).prompt)
            assert "no image matches" in prompt


class TestNumberKeyTabs:
    """1-4 jump straight to a panel; tab still cycles as before."""

    async def test_each_number_jumps_to_its_panel(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            panels = {
                "1": MinikubePanel,
                "2": ToolsPanel,
                "3": ImagesPanel,
                "4": ClusterPanel,
            }
            # Start on minikube, then walk the numbers out of order so a
            # binding that always focused the same widget would fail.
            app.query_one(MinikubePanel).focus()
            await pilot.pause()

            for key, panel_cls in panels.items():
                await pilot.press(key)
                assert await wait_until(lambda c=panel_cls: app.screen.focused is app.query_one(c))
                # The number keys stay out of the keybar: four entries would
                # push the focused panel's own keys off a 100-column line.
                assert "minikube panel" not in keybar(app)

    async def test_number_keys_are_ordinary_text_while_filtering(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            images = app.query_one(ImagesPanel)
            images.focus()
            await pilot.press("/")
            await pilot.pause()
            bar = app.query_one("#filter-bar", Input)
            assert app.screen.focused is bar

            # A digit typed into the filter must not steal focus to minikube.
            await pilot.press("1", "2")
            await pilot.pause()
            assert app.screen.focused is bar
            assert await wait_until(lambda: app.image_filter == "12")

    async def test_number_keys_do_not_reach_through_a_modal(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)

            await pilot.press("1")
            await pilot.pause()
            # ModalScreen stops the non-priority binding chain, so the
            # underlying screen keeps focus and the overlay stays up.
            assert isinstance(app.screen, HelpScreen)

    async def test_help_lists_the_number_keys(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("?")
            await pilot.pause()
            text = str(app.screen.query_one("#help-body", Static).content)
            for key, label in (
                ('"1"', "minikube panel"),
                ('"2"', "tools panel"),
                ('"3"', "images panel"),
                ('"4"', "namespaces panel"),
            ):
                assert key in text, key
                assert label in text, label


class TestPanelTitles:
    """Each panel shows its own jump key, and that key is the one bound."""

    async def test_panel_titles_match_their_jump_keys(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.system)
            bound = {b.key for b in app.BINDINGS}
            for panel_cls, expected in (
                (MinikubePanel, "(1) minikube"),
                (ToolsPanel, "(2) tools"),
                (ImagesPanel, "(3) images"),
                (ClusterPanel, "(4) namespaces"),
            ):
                panel = app.query_one(panel_cls)
                # Textual keeps a border title as a *string* carrying Rich
                # markup, so the styled number arrives as something like
                # "[bold cyan]1 [/bold cyan]minikube". Parse it back to the
                # words the user actually reads.
                title = Text.from_markup(str(panel.border_title)).plain
                assert title == expected, f"{panel_cls.__name__}: {title!r}"
                # The digit in the title is the digit that jumps there —
                # the whole point of showing it.
                assert panel.JUMP_KEY in bound, panel_cls.__name__

    async def test_titles_survive_a_data_refresh(self, fake_service):
        # set_cluster/set_tools rebuild panel content; a title clobbered by a
        # refresh is how the number silently disappears.
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("R")
            assert await wait_until(
                lambda: fake_service.calls.count("get_status") >= 2
            )
            def title_of(panel_cls: type) -> str:
                return Text.from_markup(
                    str(app.query_one(panel_cls).border_title)
                ).plain

            assert title_of(ClusterPanel) == "(4) namespaces"
            assert title_of(MinikubePanel) == "(1) minikube"

    async def test_tab_is_not_bound(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            assert "tab" not in {b.key for b in app.BINDINGS}
            assert "shift+tab" not in {b.key for b in app.BINDINGS}

            # Pressing it moves focus nowhere, which is what "unmapped" has
            # to mean in practice — not "cycles as before".
            app.query_one(MinikubePanel).focus()
            await pilot.pause()
            await pilot.press("tab")
            await pilot.pause()
            assert app.screen.focused is app.query_one(MinikubePanel)


class TestKeybarAndHelp:
    async def test_keybar_follows_the_focused_panel(self, fake_service):
        fake_service.cluster["running"] = False
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)

            text = keybar(app)
            for expected in ('"s" start', '"R" re-check', '"?" help', '"q" quit'):
                assert expected in text, expected
            assert '"i" install' not in text
            assert '"S" stop' not in text  # cluster is down: nothing to stop
            assert '"x" hide log' not in text  # idle: nothing to dismiss

            app.query_one(ToolsPanel).focus()
            await pilot.pause()
            text = keybar(app)
            assert '"i" install' in text and '"I" install all' in text
            assert '"s" start' not in text

            # Keys that are greyed out stay listed but visibly disabled.
            app.query_one(MinikubePanel).focus()
            await pilot.pause()
            await pilot.press("s")
            assert await wait_until(lambda: app.busy)
            assert app.active_bindings["s"].enabled is False
            assert '"s" start' in keybar(app)

    async def test_help_lists_every_panel(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)

            await pilot.press("?")
            await pilot.pause()
            assert isinstance(app.screen, HelpScreen)
            text = str(app.screen.query_one("#help-body", Static).content)

            for key, label in (
                # (the overlay pads columns, so keys and labels are checked apart)
                ('"s"', "start"), ('"S"', "stop"), ('"d"', "delete"),  # minikube
                ('"o"', "settings"),
                ('"i"', "install"), ('"I"', "install all"),             # tools
                ('"/"', "filter"),                                      # images
                ('"enter/space"', "expand / collapse"),                 # namespaces
                ('"q"', "quit"), ('"?"', "help"), ('"R"', "re-check"),
                ('"x"', "hide log"),
                ('"esc"', "close filter"),
                # Numbers jump between panels; tab is deliberately unmapped.
                ('"1"', "minikube panel"), ('"2"', "tools panel"),
                ('"3"', "images panel"), ('"4"', "namespaces panel"),
            ):
                assert key in text, key
                assert label in text, label
            # An unmapped tab leaves nothing behind in the reference.
            assert "tab" not in text
            await pilot.press("escape")
            await pilot.pause()
            assert not isinstance(app.screen, HelpScreen)
