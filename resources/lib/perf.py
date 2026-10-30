"""Timing for one plugin run, written to the log only when the debug setting is on.

Phases are marked in order (imports, init, fetch, ...); counters add up repeated work
(settings reads, sqlite calls, HTTP requests) wherever it happens.
"""

import threading
import time

_LOCK = threading.Lock()
_state = {"t0": None, "wall0": None, "cpu0": None, "last": None}
_phases = []
_counters = {}


def start(t0=None, wall0=None, cpu0=None):
    """Begin timing; pass the values default.py took before its imports."""
    now = time.perf_counter()
    _state["t0"] = t0 if t0 is not None else now
    _state["wall0"] = wall0 if wall0 is not None else time.time()
    _state["cpu0"] = cpu0
    _state["last"] = _state["t0"]


def mark(name):
    """End the current phase and name it."""
    if _state["last"] is None:
        return
    now = time.perf_counter()
    _phases.append((name, now - _state["last"]))
    _state["last"] = now


def add(name, seconds=0.0):
    with _LOCK:
        counter = _counters.setdefault(name, [0, 0.0])
        counter[0] += 1
        counter[1] += seconds


class span:  # noqa: N801 - used like a function: ``with perf.span("sqlite"):``
    __slots__ = ("name", "t")

    def __init__(self, name):
        self.name = name

    def __enter__(self):
        self.t = time.perf_counter()
        return self

    def __exit__(self, *exc):
        add(self.name, time.perf_counter() - self.t)
        return False


def summary(label):
    if _state["t0"] is None:
        return ""
    mark("rest")
    total = time.perf_counter() - _state["t0"]
    wall0 = _state["wall0"]
    started = "{}.{:03d}".format(time.strftime("%H:%M:%S", time.localtime(wall0)),
                                 int(wall0 * 1000) % 1000)
    parts = ["perf {} start={} total={:.0f}ms".format(label, started, total * 1000)]
    if _state["cpu0"] is not None:
        # CPU this thread used before default.py's first line: roughly the interpreter start-up.
        parts.append("pre_script_cpu={:.0f}ms".format(_state["cpu0"] * 1000))
    try:
        parts.append("thread_cpu={:.0f}ms".format(time.thread_time() * 1000))
    except (AttributeError, OSError):
        pass
    phases = " ".join("{}={:.0f}".format(n, s * 1000) for n, s in _phases if s >= 0.0005 or n != "rest")
    parts.append("| " + phases)
    with _LOCK:
        counters = ", ".join("{} {}x {:.0f}ms".format(n, c, s * 1000)
                             for n, (c, s) in sorted(_counters.items()))
    if counters:
        parts.append("| " + counters)
    return " ".join(parts)
