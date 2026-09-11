"""Allow ``python -m bqio`` to launch the diagnostic CLI."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
