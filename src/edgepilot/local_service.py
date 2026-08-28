"""Compatibility entry point for :mod:`edgepilot.service.local_service`."""

import sys
from edgepilot.service import local_service as _implementation


if __name__ == "__main__":
    raise SystemExit(_implementation.main())
else:
    sys.modules[__name__] = _implementation
