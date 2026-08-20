"""Compatibility entry point; prefer ``python -m watcher``."""

from watcher.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
