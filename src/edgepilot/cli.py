"""Public import and ``python -m`` entry point for the Live CLI."""

import sys
from edgepilot.application import cli as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
