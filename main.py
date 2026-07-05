"""Thin launcher so legacy `uv run main.py` still works after the package split.

Install/live development use `uv run kubby` (see pyproject.toml [project.scripts]).
"""

from kubby.app import main

if __name__ == "__main__":
    raise SystemExit(main())
