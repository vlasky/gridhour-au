"""Drive the real interactive loop in a pseudo-terminal: keys, a resize, a clean exit."""
import os
import select
import signal
import struct
import subprocess
import sys
import threading
import time

import pytest
from conftest import NOW, fake_fetch

from gridhour_au import grid, state

fcntl = pytest.importorskip("fcntl")
termios = pytest.importorskip("termios")


def run_app(keys, size=(40, 120)):
    grid.load(NOW, "2000", fetch=fake_fetch)      # seed the cache the child reads offline
    env = dict(os.environ, GRIDHOUR_AU_OFFLINE="1", PYTHONPATH=os.getcwd())
    # Popen rather than pty.fork(): forking a multi-threaded process (pytest) in Python can
    # deadlock the child, while subprocess forks and execs in C.
    fd, tty = os.openpty()
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", size[0], size[1], 0, 0))
    proc = subprocess.Popen([sys.executable, "-m", "gridhour_au"], stdin=tty, stdout=tty, stderr=tty, env=env,
                            start_new_session=True)
    os.close(tty)
    out = bytearray()

    def pump(secs):
        end = time.time() + secs
        while time.time() < end:
            if select.select([fd], [], [], 0.05)[0]:
                try:
                    out.extend(os.read(fd, 65536))
                except OSError:
                    return

    pump(0.8)
    for k in keys:
        if k == "RESIZE":
            fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", 16, 70, 0, 0))
            proc.send_signal(signal.SIGWINCH)
        else:
            os.write(fd, k.encode())
        pump(0.15)
    os.write(fd, b"q")
    pump(0.5)
    try:
        code = proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        pytest.fail("gridhour-au did not quit")
    finally:
        pump(0.2)
        os.close(fd)
    return code, out.decode("utf-8", "ignore")


def test_interactive_session():
    code, out = run_app(["\x1b[C", "j", "+", "w", "?", "x", "a", *"Dryer 1h", "\r", "m", "c", "RESIZE", "r",
                           "RESIZE", "RESIZE", "\x1b[1;2C", "RESIZE"])     # resizes once deadlocked the loop
    assert code == 0, out[-2000:]
    assert "Traceback" not in out
    assert "\x1b[?1049h" in out and out.rstrip().endswith("\x1b[?1049l")   # alternate screen in and out
    assert "added Dryer" in out


def test_the_first_frame_names_the_region_asked_for():
    st = state.State()
    st.region_id = "SA1"
    st.save()
    code, out = run_app([])
    assert code == 0 and "South Australia" in out and "New South Wales" not in out


def test_the_time_zone_follows_the_forecast_shown(st, monkeypatch):
    """While a new region loads, the old region's forecast is still on screen, in its own zone."""
    from gridhour_au import app
    loaded, release, done = threading.Event(), threading.Event(), []

    def load(now, postcode, region, prices, force=False):
        fc = grid.load(NOW, postcode, region, prices, force=force, fetch=fake_fetch)
        if region == "SA1":
            loaded.set()
            release.wait(5)             # a slow network, after the reports that came quickly
        return fc

    def finished(n):
        while len(done) < n:
            time.sleep(0.01)

    monkeypatch.setattr(app, "load", load)
    st.postcode, st.region_id = None, "QLD1"
    app.fetch(st, notify=done.append)
    finished(1)
    assert st.fc.region_id == "QLD1" and grid.local(NOW).tzname() == "AEST"
    st.region_id = "SA1"
    app.fetch(st, force=True, notify=done.append)
    assert loaded.wait(5)
    assert st.fc.region_id == "QLD1" and grid.local(NOW).tzname() == "AEST"
    release.set()
    finished(2)
    assert st.fc.region_id == "SA1" and grid.local(NOW).tzname() == "ACDT"
