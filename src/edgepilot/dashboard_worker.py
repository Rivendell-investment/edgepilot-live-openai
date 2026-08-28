"""Compatibility entry point for :mod:`edgepilot.dashboard.worker`."""

import sys
from edgepilot.dashboard import worker as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
