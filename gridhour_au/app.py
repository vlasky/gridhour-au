"""The interactive session.

Everything that can happen arrives as an event on one queue: key presses from a reader thread,
"resize" from the SIGWINCH handler, "data" when a background fetch finishes, and a "tick" when
the queue stays quiet until the next whole second. The main loop takes one event, updates the
state, draws a frame and writes only the rows that differ from the previous frame.
"""
import os
import queue
import shutil
import signal
import sys
import threading
import time
from datetime import UTC, datetime

from .compose import compose
from .grid import empty_forecast, load, set_zone
from .keys import handle_key
from .themes import C

RELOAD = 5 * 60         # rebuild the window from cache this often; the network is hit only when it is stale


def utcnow():
    return datetime.now(UTC)


# ---------------------------------------------------------------- background data

_fetch_lock = threading.Lock()


def fetch(st, force=False, notify=None):
    """Load in a thread. Routine refreshes are skipped while one is running; a forced one (new
    postcode, R) always starts, and the newest request wins."""
    with _fetch_lock:
        if st.loading and not force:
            return
        st.gen += 1
        gen = st.gen
        st.loading = True

    def work():
        fc, failure = None, None
        try:
            fc = load(utcnow(), st.postcode, st.region_id, st.prices, force=force)
        except Exception as e:  # noqa: BLE001 - a background failure must reach the status line, not stderr
            failure = "update failed (%s)" % type(e).__name__
        with _fetch_lock:
            if gen == st.gen:
                if fc is not None:
                    st.fc = fc
                    set_zone(fc.region_id)
                    if fc.errors:
                        st.say(fc.errors[0], C.AMBER, 6)
                    elif force:
                        st.say("forecast updated", C.GREEN)
                else:
                    st.say(failure, C.RED, 8)
                st.loading = False
        if notify:
            notify("data")

    threading.Thread(target=work, name="gridhour-fetch", daemon=True).start()


# ---------------------------------------------------------------- keyboard

class KeyDecoder:
    """Cuts a byte stream into keys: plain characters, CSI sequences (ESC [ ... final) and
    SS3 sequences (ESC O x). A sequence split across two reads is held until it is complete;
    an ESC with nothing after it in the same read is the Escape key itself."""

    def __init__(self):
        self.pending = ""

    def feed(self, text):
        self.pending += text
        keys = []
        while self.pending:
            key, used = self._next(self.pending)
            if used == 0:
                break               # an unfinished sequence, wait for more bytes
            keys.append(key)
            self.pending = self.pending[used:]
        return keys

    def flush(self):
        """Whatever is still pending is taken literally (a lone ESC, say)."""
        keys = list(self.pending)
        self.pending = ""
        return keys

    @staticmethod
    def _next(s):
        if s[0] != "\x1b":
            return s[0], 1
        if len(s) == 1:
            return None, 0
        if s[1] == "O":
            return (s[:3], 3) if len(s) >= 3 else (None, 0)
        if s[1] != "[":
            return "\x1b", 1        # ESC followed by an ordinary key: two keys
        for j in range(2, len(s)):
            if "\x40" <= s[j] <= "\x7e":
                return s[:j + 1], j + 1
        return None, 0


def read_keys(fd, events):
    """Reader thread: decoded keys go onto the event queue."""
    dec = KeyDecoder()
    while True:
        try:
            data = os.read(fd, 1024)
        except OSError:
            return
        if not data:
            return
        for k in dec.feed(data.decode("utf-8", "ignore")):
            events.put(("key", k))
        if dec.pending == "\x1b":       # nothing followed the ESC in this read
            for k in dec.flush():
                events.put(("key", k))


# ---------------------------------------------------------------- terminal

class Terminal:
    """Cbreak mode on the alternate screen with the cursor hidden; all of it undone on exit."""

    # autowrap off (?7l): if a row is ever wider than the terminal it is cut, not wrapped and scrolled
    ENTER = "\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[2J"
    LEAVE = "\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l"

    def __init__(self, fd, out):
        self.fd, self.out, self.saved = fd, out, None

    def __enter__(self):
        import termios
        import tty
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        self.out.write(self.ENTER)
        self.out.flush()
        return self

    def __exit__(self, *exc):
        import termios
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        self.out.write(self.LEAVE)
        self.out.flush()
        return False


class Screen:
    """Remembers what is on screen and sends only the rows that changed, in one write."""

    def __init__(self, out):
        self.out = out
        self.shown = []
        self.size = None

    def show(self, lines, size):
        parts = []
        if size != self.size:
            parts.append("\x1b[2J")
            self.shown, self.size = [], size
        for y, line in enumerate(lines):
            if y >= len(self.shown) or self.shown[y] != line:
                parts.append("\x1b[%d;1H" % (y + 1) + line)
        if parts:
            self.out.write("".join(parts))
            self.out.flush()
        self.shown = lines


# ---------------------------------------------------------------- loop

def run(st):
    fd, out = sys.stdin.fileno(), sys.stdout
    # SimpleQueue.put is reentrant, so the SIGWINCH handler may call it while the main thread is
    # inside get(); a plain Queue would deadlock on its own lock there.
    events = queue.SimpleQueue()
    previous = signal.signal(signal.SIGWINCH, lambda *a: events.put(("resize", None)))
    try:
        with Terminal(fd, out):
            threading.Thread(target=read_keys, args=(fd, events), name="gridhour-keys", daemon=True).start()
            _loop(st, events, Screen(out))
    finally:
        signal.signal(signal.SIGWINCH, previous if previous is not None else signal.SIG_DFL)


def _loop(st, events, screen):
    notify = lambda kind: events.put((kind, None))  # noqa: E731
    # until the first fetch lands, name the place asked for (and use its clock), not the default
    st.fc = empty_forecast(st.postcode, st.region_id)
    set_zone(st.fc.region_id)
    fetch(st, notify=notify)
    reloaded, hour = time.monotonic(), utcnow().hour
    while True:
        live = utcnow()
        if time.monotonic() - reloaded > RELOAD or live.hour != hour:
            reloaded, hour = time.monotonic(), live.hour
            fetch(st, notify=notify)
        W, H = shutil.get_terminal_size((100, 30))
        now = st.frozen or live.replace(microsecond=0)
        screen.show(compose(W, H, st, now, live.replace(microsecond=0)).lines(), (W, H))
        try:
            kind, value = events.get(timeout=max(0.05, 1.0 - live.microsecond / 1e6))
        except queue.Empty:
            continue                                    # the clock ticked, draw again
        batch = [(kind, value)]
        while True:                                     # take everything already waiting, draw once
            try:
                batch.append(events.get_nowait())
            except queue.Empty:
                break
        for kind, value in batch:
            if kind != "key":
                continue
            action = handle_key(st, value, utcnow())
            if action == "quit":
                return
            if action in ("fetch", "refetch"):
                reloaded = time.monotonic()
                fetch(st, force=action == "refetch", notify=notify)
