"""Compatibility alias for :mod:`edgepilot.identity.facade`."""

import sys
from edgepilot.identity import facade as _implementation

sys.modules[__name__] = _implementation
