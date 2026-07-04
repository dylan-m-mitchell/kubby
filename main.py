"""Thin launcher so legacy `uv run main.py` still works after the package split.

Install/live development use `uv run kubui` (see pyproject.toml [project.scripts]).
"""

from kubui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
