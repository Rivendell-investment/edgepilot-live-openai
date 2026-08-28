"""Compatibility alias for :mod:`edgepilot.dashboard.http`."""

import sys
from edgepilot.dashboard import http as _implementation

sys.modules[__name__] = _implementation
