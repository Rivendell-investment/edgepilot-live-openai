"""Compatibility alias for :mod:`edgepilot.strategies.discovery`."""

import sys
from edgepilot.strategies import discovery as _implementation

sys.modules[__name__] = _implementation
