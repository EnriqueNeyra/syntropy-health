"""The tests' own clock: time the machine stood still is left out, and time the code took never is."""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

from tests import pauses

FREEZE = """
import os, signal, sys, time
pid = int(sys.argv[1])
os.kill(pid, signal.SIGSTOP)
try:
    time.sleep(1.5)
finally:
    os.kill(pid, signal.SIGCONT)
"""


@pytest.mark.skipif(sys.platform == "win32", reason="nothing there stops a process from outside as SIGSTOP does")
def test_time_the_whole_process_stood_still_isnt_counted():
    started = time.monotonic()
    subprocess.run([sys.executable, "-c", FREEZE, str(os.getpid())], check=True)
    took = time.monotonic() - started
    assert took >= 1.5
    assert pauses.stood_still(started) >= 1.2
    assert pauses.elapsed(started) <= took - 1.2


def test_waiting_is_still_counted():
    started = time.monotonic()
    time.sleep(0.6)
    assert pauses.elapsed(started) >= 0.55


def test_working_is_still_counted():
    started = time.monotonic()
    while time.monotonic() - started < 0.6:
        sum(range(2000))
    assert pauses.elapsed(started) >= 0.55


def test_a_lock_held_by_another_thread_is_still_counted(tmp_path):
    import sqlite3
    import threading
    path = str(tmp_path / "held.db")
    sqlite3.connect(path).execute("CREATE TABLE t(x)").connection.close()
    held = threading.Event()

    def hold():
        conn = sqlite3.connect(path, isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        held.set()
        time.sleep(0.8)
        conn.execute("ROLLBACK")
        conn.close()
    holder = threading.Thread(target=hold)
    holder.start()
    held.wait(5)
    started = time.monotonic()
    conn = sqlite3.connect(path, timeout=5)
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()
    holder.join()
    assert pauses.elapsed(started) >= 0.6
