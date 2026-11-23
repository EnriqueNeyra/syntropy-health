"""Time this whole process stood still, so a test that times the app doesn't blame it for a frozen machine.

A shared CI machine sometimes stops for seconds at a time. In November 2026 two tests failed that way on Linux, 9 and
11 seconds into something that takes milliseconds, with every write already kept off the event loop and the network
blocked, and neither failed again when run again.

A thread that only sleeps keeps ticking through whatever the app does: waiting for a lock, the disk or another thread
lets it run. It's late only when nothing in the process ran at all. So the gaps between its ticks are time no code here
could have used, and ``elapsed`` leaves them out. Slow code is still slow: its time passes with the ticks arriving.
"""

from __future__ import annotations

import threading
import time

TICK = 0.02
LATE = 0.25         # a tick this late wasn't waiting its turn behind other threads, which costs milliseconds

_pauses: list[tuple[float, float]] = []      # (from, until), on time.monotonic()'s clock
_ticked = threading.Condition()
_ticks = 0
_thread: threading.Thread | None = None


def _tick() -> None:
    global _ticks
    last = time.monotonic()
    while True:
        time.sleep(TICK)
        now = time.monotonic()
        if now - last > LATE:
            _pauses.append((last + TICK, now))
        last = now
        with _ticked:
            _ticks += 1
            _ticked.notify_all()


def start() -> None:
    global _thread
    if _thread is None:
        _thread = threading.Thread(target=_tick, name="machine-pauses", daemon=True)
        _thread.start()


def stood_still(since: float, until: float | None = None) -> float:
    """Seconds between two ``time.monotonic()`` readings in which nothing in this process ran."""
    start()
    until = time.monotonic() if until is None else until
    # A pause that has only just ended isn't written down until the ticking thread runs again, so wait for it to.
    with _ticked:
        seen = _ticks
        _ticked.wait_for(lambda: _ticks >= seen + 2, timeout=2.0)
    return sum(max(0.0, min(end, until) - max(begin, since)) for begin, end in list(_pauses))


def elapsed(since: float) -> float:
    """Seconds since a ``time.monotonic()`` reading, less any in which the whole process stood still."""
    now = time.monotonic()
    if now - since < LATE - TICK:        # too short to hold a pause, and most of what's timed is
        return now - since
    return now - since - stood_still(since, now)
