"""Tool detection: is `tool` installed? if so, what version, and at which PATH?"""
from __future__ import annotations

import shutil
import subprocess

from kubby.installer.tools import Tool


def detect(tool: Tool) -> tuple[bool, str | None, str | None]:
    """Return `(installed, version, path)`.

    `installed=True, version=None` means the binary was found but we couldn't
    parse its version. `installed=False` means the binary is missing.
    """
    path = shutil.which(tool.key)
    if not path:
        return False, None, None

    try:
        result = subprocess.run(
            [tool.key, *tool.version_args],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        # binary exists on PATH but won't run cleanly — treat as installed
        # without a parsable version.
        return True, None, path

    if result.returncode != 0:
        # The binary ran but its version subcommand failed. Still consider
        # the tool installed; just don't have a version to show.
        return True, None, path

    output = (result.stdout or "") + (result.stderr or "")
    version = tool.parse_version(output)
    return True, version, path
