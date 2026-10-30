"""Tally plugin entry point."""

import sys
import time

# Taken before any add-on import so the debug timing log can show import cost.
_T0, _WALL0 = time.perf_counter(), time.time()
try:
    _CPU0 = time.thread_time()
except (AttributeError, OSError):
    _CPU0 = None

from resources.lib import perf  # noqa: E402

perf.start(_T0, _WALL0, _CPU0)

from resources.lib import router  # noqa: E402

if __name__ == "__main__":
    perf.mark("imports")
    router.run(sys.argv)
