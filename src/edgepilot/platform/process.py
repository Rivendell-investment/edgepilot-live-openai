"""Windows-safe child process spawning.

The local service is launched with ``DETACHED_PROCESS``, so it owns no console.
On Windows a console application spawned from a console-less parent is given a
*brand new* console window, which is visible to the user. The Dashboard polls
the auth endpoints, and every poll can shell out to ``whoami``/``icacls`` or to
the runtime worker, so that turns into a stream of popup windows.

``CREATE_NO_WINDOW`` suppresses the console allocation without changing how the
child is otherwise wired up. It is a no-op on POSIX, where the flag does not
exist and no console is ever allocated.
"""

from __future__ import annotations

import os
import subprocess

# Defined only on Windows builds of the stdlib; the literal keeps type checkers
# and non-Windows imports happy.
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)


def no_window_flags(extra: int = 0) -> int:
    """``creationflags`` that keep a console child invisible; ``0`` off Windows.

    ``extra`` lets a caller keep flags it already needs, e.g.
    ``no_window_flags(subprocess.CREATE_NEW_PROCESS_GROUP)``. Do not combine it
    with ``DETACHED_PROCESS`` — that flag already suppresses the console, and
    Windows rejects the pair.
    """
    if os.name != "nt":
        return 0
    return extra | CREATE_NO_WINDOW
