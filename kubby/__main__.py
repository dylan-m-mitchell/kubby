"""Allow `python -m kubby` invocation."""

from kubby.app import main

if __name__ == "__main__":
    raise SystemExit(main())
