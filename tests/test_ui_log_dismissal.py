"""Regression test for the global log's dismiss button.

`kubby/ui/app.js` is not a module, so the JS-side test drives the real
object in a `node` vm sandbox with stubbed DOM globals. Skipped when
``node`` is not on PATH.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
JS_TEST = Path(__file__).parent / "js" / "log_dismissal.test.js"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_dismissed_global_log_stays_dismissed_for_current_job():
    proc = subprocess.run(
        [NODE, str(JS_TEST)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, f"{proc.stdout}\n{proc.stderr}"
