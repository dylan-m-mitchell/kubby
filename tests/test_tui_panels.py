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
    """1-4 jump straight to a panel; tab is inert."""

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


class TestVimNavigation:
    """j/k everywhere; h/l on the tree only, where a tree has sides."""

    async def test_jk_moves_in_both_list_panels(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            for panel_cls, first, second in (
                (ToolsPanel, "minikube", "helm"),
                (ImagesPanel, "nginx:alpine", "busybox:latest"),
            ):
                panel = app.query_one(panel_cls)
                panel.focus()
                panel.highlighted = 0
                await pilot.pause()

                await pilot.press("j")
                await pilot.pause()
                assert panel.highlighted == 1, panel_cls.__name__
                assert second in str(panel.get_option_at_index(1).prompt)

                await pilot.press("k")
                await pilot.pause()
                assert panel.highlighted == 0, panel_cls.__name__
                assert first in str(panel.get_option_at_index(0).prompt)

    async def test_jk_behave_exactly_like_the_arrows_at_the_ends(self, fake_service):
        # j/k are aliases, not a second dialect: the arrows already wrap at
        # the ends (OptionList.cursor_up uses find_next_enabled, not the
        # no-wrap variant its scrolling uses), so making j/k clamp would make
        # the two keys disagree about the same list.
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            tools = app.query_one(ToolsPanel)
            tools.focus()
            await pilot.pause()
            last = len(tools.options) - 1

            # Start each key on the edge it moves away from.
            for vim_key, arrow_key, start in (("k", "up", 0), ("j", "down", last)):
                results = {}
                for key in (arrow_key, vim_key):
                    tools.highlighted = start
                    await pilot.pause()
                    await pilot.press(key)
                    await pilot.pause()
                    results[key] = tools.highlighted
                # Identical, including the wrap to the far end.
                assert results[vim_key] == results[arrow_key], (vim_key, results)
                assert results[vim_key] == (last if start == 0 else 0), (
                    vim_key,
                    results,
                )

    async def test_jk_moves_in_the_namespace_tree(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            tree = app.query_one(ClusterPanel)
            tree.focus()
            tree.cursor_line = 0
            await pilot.pause()

            await pilot.press("j")
            await pilot.pause()
            assert tree.cursor_line == 1

            await pilot.press("k")
            await pilot.pause()
            assert tree.cursor_line == 0

    async def test_h_collapses_then_climbs_to_the_parent(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            tree = app.query_one(ClusterPanel)
            tree.focus()
            # Start on the second namespace so a parent genuinely exists.
            tree.cursor_line = 2  # kube-system
            await pilot.pause()
            namespace = tree._tree_lines[2].path[-1]
            assert namespace.is_expanded

            # h folds the pods away.
            await pilot.press("h")
            await pilot.pause()
            assert not namespace.is_expanded
            assert tree.cursor_line == 2

            # On a pod, h climbs instead of collapsing. Expand it again
            # first — while collapsed the pod is not a visible row.
            await pilot.press("l")
            await pilot.pause()
            assert namespace.is_expanded
            await pilot.press("j")
            await pilot.pause()
            assert tree.cursor_line == 3
            await pilot.press("h")
            await pilot.pause()
            assert tree.cursor_line == 2

    async def test_l_expands_then_enters_the_first_pod(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            tree = app.query_one(ClusterPanel)
            tree.focus()
            tree.cursor_line = 0
            await pilot.pause()
            namespace = tree._tree_lines[0].path[-1]
            namespace.collapse()
            await pilot.pause()

            await pilot.press("l")
            await pilot.pause()
            assert namespace.is_expanded
            assert tree.cursor_line == 0  # expanding does not move

            # Already expanded: l steps in to the first pod.
            await pilot.press("l")
            await pilot.pause()
            assert tree.cursor_line == 1

    async def test_hl_on_a_pod_are_safe_no_ops(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            tree = app.query_one(ClusterPanel)
            tree.focus()
            tree.cursor_line = 1  # a pod
            await pilot.pause()
            pod = tree._tree_lines[1].path[-1]
            assert not pod.allow_expand

            # l on a leaf: nothing to expand, and it must not crash.
            await pilot.press("l")
            await pilot.pause()
            assert tree.cursor_line == 1

    async def test_hl_do_nothing_in_the_single_column_panels(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            for panel_cls in (ToolsPanel, ImagesPanel):
                panel = app.query_one(panel_cls)
                panel.focus()
                panel.highlighted = 1
                await pilot.pause()
                before = panel.highlighted

                # Deliberate: a one-column list has no horizontal dimension,
                # so there is nothing honest for h/l to do. Asserted so the
                # inert behaviour is intentional rather than a gap.
                for key in ("h", "l"):
                    await pilot.press(key)
                    await pilot.pause()
                assert panel.highlighted == before, panel_cls.__name__
                assert app.screen.focused is panel, panel_cls.__name__

    async def test_nav_keys_are_typed_not_bound_in_the_filter(self, fake_service):
        # The leak guard. hjkl are plain letters; if they reached the app
        # bindings while the filter had focus, every query containing them
        # would become unusable. q already failed this way once.
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.images)
            app.query_one(ImagesPanel).focus()
            await pilot.press("/")
            await pilot.pause()
            bar = app.query_one("#filter-bar", Input)

            await pilot.press("h", "j", "k", "l")
            await pilot.pause()
            assert bar.value == "hjkl"
            assert app.image_filter == "hjkl"
            assert app.screen.focused is bar
            assert app.is_running

    async def test_nav_keys_do_not_fire_through_the_help_overlay(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            app.query_one(ToolsPanel).focus()
            tools = app.query_one(ToolsPanel)
            tools.highlighted = 0
            await pilot.press("?")
            await pilot.pause()

            await pilot.press("j", "h", "l")
            await pilot.pause()
            assert tools.highlighted == 0

    async def test_nav_keys_stay_out_of_the_keybar(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)):
            await wait_until(lambda: app.system)
            bar = keybar(app)
            # ~27 characters of headroom at 100 columns; four nav keys per
            # panel would overflow it, so they live in the help overlay only.
            assert '"j"' not in bar and '"h"' not in bar and '"l"' not in bar
            assert len(bar) < 100

    async def test_help_documents_the_vim_keys(self, fake_service):
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            await pilot.press("?")
            await pilot.pause()
            text = str(app.screen.query_one("#help-body", Static).content)

            for label in (
                "move down / up",          # j/k, all three panels
                "collapse, or out to the parent",   # h on the tree
                "expand, or in to the first pod",   # l on the tree
                "not applicable — one column",      # why h/l is absent on lists
            ):
                assert label in text, label
            # The arrows are documented alongside j/k, since they still work.
            assert "j/k or up/down" in text

    async def test_arrows_still_move(self, fake_service):
        # hjkl is an addition, not a replacement: the arrows were working
        # before and must keep working.
        app = KubbyApp(service=fake_service)
        async with app.run_test(size=(100, 40)) as pilot:
            await wait_until(lambda: app.system)
            tools = app.query_one(ToolsPanel)
            tools.focus()
            tools.highlighted = 0
            await pilot.pause()

            await pilot.press("down")
            await pilot.pause()
            assert tools.highlighted == 1

            await pilot.press("up")
            await pilot.pause()
            assert tools.highlighted == 0

            await pilot.press("end")
            await pilot.pause()
            assert tools.highlighted == len(tools.options) - 1


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
                # Vim movement: j/k everywhere, h/l on the tree only.
                ('"j/k or up/down"', "move down / up"),
                ('"h"', "collapse, or out to the parent"),
                ('"l"', "expand, or in to the first pod"),
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
