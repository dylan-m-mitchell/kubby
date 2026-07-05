"""Allow `python -m kubui` invocation."""

from kubui.app import main

if __name__ == "__main__":
    raise SystemExit(main())
