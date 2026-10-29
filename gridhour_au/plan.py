"""Scoring half-hour slots and finding the best window to run a job.

A job is a name and a duration, optionally with a deadline ("by 07:00") and permission to run in
pieces ("split"). Each slot gets a score from 0 (best in the window) to 1 (worst), from carbon,
price or both. The best window is the set of slots with the lowest mean score that starts no
earlier than the current half hour, ends by the deadline if there is one, and is one continuous
run unless the job may be split.
"""
import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from .grid import SLOT, floor_slot, zone
from .safe import label

MODES = ("green", "cheap", "both")
MODE_LABEL = {"green": "greenest", "cheap": "cheapest", "both": "green + cheap"}


@dataclass
class Job:
    name: str
    minutes: int
    deadline: str = None        # "07:00", local time: finish by the next one; None = any time
    split: bool = False         # may run in pieces (EV, battery, hot water), not one continuous block

    @property
    def slots(self):
        return max(1, -(-self.minutes // 30))

    def to_json(self):
        out = {"name": self.name, "minutes": self.minutes}
        if self.deadline:
            out["deadline"] = self.deadline
        if self.split:
            out["split"] = True
        return out

    def describe(self):
        """'4h by 07:00, split'"""
        text = fmt_duration(self.minutes)
        if self.deadline:
            text += " by " + self.deadline
        return text + (", split" if self.split else "")


DEFAULT_JOBS = [Job("Washing", 120), Job("Dishwasher", 180), Job("EV charge", 240, "07:00", True),
                Job("Batch job", 60)]


@dataclass
class Window:
    job: Job
    first: int          # index of the first slot
    last: int           # index of the last slot (inclusive)
    score: float
    carbon: float       # mean gCO2/kWh, None if unknown
    price: float        # mean c/kWh, None if unknown
    start: object
    end: object
    indices: tuple = None   # every chosen slot; first..last unless the job is split

    def __post_init__(self):
        if self.indices is None:
            self.indices = tuple(range(self.first, self.last + 1))

    def covers(self, k):
        return k in self.indices

    def parts(self):
        """The chosen slots as runs of consecutive indices: [(first, last), ...]."""
        runs = []
        for k in self.indices:
            if runs and k == runs[-1][1] + 1:
                runs[-1] = (runs[-1][0], k)
            else:
                runs.append((k, k))
        return runs


def parse_duration(text):
    """'2h', '1h30', '90m', '1.5h', '45' (minutes) -> minutes, rounded up to the half hour."""
    s = text.strip().lower().replace(" ", "")
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+)(?:m|min)?)?", s)
    if not s or not m or not (m.group(1) or m.group(2)):
        raise ValueError("durations look like 2h, 1h30, 90m or 1.5h")
    mins = float(m.group(1) or 0) * 60 + int(m.group(2) or 0)
    if m.group(1) is None and m.group(2) and not s.endswith(("m", "min")) and int(m.group(2)) <= 12:
        mins = int(m.group(2)) * 60            # a bare small number means hours
    mins = int(-(-mins // 30) * 30)
    if not 30 <= mins <= 24 * 60:
        raise ValueError("a job runs between 30 minutes and 24 hours")
    return mins


def parse_clock(text):
    """'07:00', '7:00', '7am', '7.30pm', '19:30' -> 'HH:MM'. Raises ValueError."""
    s = text.strip().lower().replace(".", ":")
    m = re.fullmatch(r"([0-9]{1,2})(?::([0-9]{2}))?\s*(am|pm)?", s, re.ASCII)
    if not m:
        raise ValueError("deadlines look like 07:00, 7am or 19:30")
    h, mins, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm:
        if not 1 <= h <= 12:
            raise ValueError("deadlines look like 07:00, 7am or 19:30")
        h = h % 12 + (12 if ampm == "pm" else 0)
    elif m.group(2) is None:
        raise ValueError("give the deadline as 07:00 or 7am")
    if h > 23 or mins > 59 or mins % 30:
        raise ValueError("a deadline is on the hour or half hour, e.g. 07:00 or 06:30")
    return "%02d:%02d" % (h, mins)


def parse_job(text):
    """'Dryer 1h30' or 'EV charge 4h by 07:00 split' -> Job."""
    words = text.strip().split()
    split = bool(words) and words[-1].lower() == "split"
    if split:
        words.pop()
    deadline = None
    if len(words) >= 2 and words[-2].lower() == "by":
        deadline = parse_clock(words.pop())
        words.pop()
    tail = (" by " + deadline if deadline else "") + (" split" if split else "")
    if len(words) < 2:
        if words and tail:                          # "EV by 07:00 split": the duration is missing
            raise ValueError("how long does it run? e.g. '%s 4h%s'" % ((label(words[0], 18) or "EV"), tail))
        raise ValueError("type a name and a duration, e.g. 'Dryer 1h30' or 'EV 4h by 07:00 split'")
    try:
        minutes = parse_duration(words[-1])
    except ValueError:
        if tail:
            raise ValueError("how long does it run? e.g. '%s 4h%s'" % (label(" ".join(words), 18), tail)) from None
        raise
    words.pop()
    name = (label(" ".join(words), 18) or "").strip()
    if not name:
        raise ValueError("give the job a name")
    return Job(name, minutes, deadline, split)


def deadline_at(job, now):
    """The next time the job's deadline falls after ``now``, in UTC, or None."""
    if not job.deadline:
        return None
    h, m = (int(v) for v in job.deadline.split(":"))
    tz = zone()
    day = now.astimezone(tz).date()
    for add in (0, 1, 2):
        local = datetime(day.year, day.month, day.day, h, m, tzinfo=tz) + timedelta(days=add)
        t = local.astimezone(now.tzinfo)
        if t > now:
            return t
    return None


def fmt_duration(mins):
    h, m = divmod(int(mins), 60)
    if not h:
        return "%dm" % m
    return "%dh%02d" % (h, m) if m else "%dh" % h


def _norm(values):
    known = [v for v in values if v is not None]
    if not known:
        return [None] * len(values)
    lo, hi = min(known), max(known)
    span = (hi - lo) or 1.0
    return [None if v is None else (v - lo) / span for v in values]


def effective_mode(slots, mode):
    """Price modes fall back to green when there are no prices at all."""
    if mode != "green" and not any(s.price is not None for s in slots):
        return "green"
    return mode


def scores(slots, mode):
    mode = effective_mode(slots, mode)
    c = _norm([s.carbon for s in slots])
    if mode == "green":
        return c
    p = _norm([s.price for s in slots])
    if mode == "cheap":
        return p
    return [None if a is None or b is None else (a + b) / 2 for a, b in zip(c, p, strict=True)]


def _mean(vals):
    vals = [v for v in vals if v is not None]
    return sum(vals) / len(vals) if vals else None


def now_index(slots, now):
    t = floor_slot(now)
    for i, s in enumerate(slots):
        if s.start == t:
            return i
    return 0 if slots and now < slots[0].start else len(slots)


def best_window(slots, sc, job, now):
    """The best-scoring slots for ``job``: from the current half hour on, finished by the deadline
    if there is one, and one continuous run unless the job may be split."""
    n = job.slots
    first = now_index(slots, now)
    deadline = deadline_at(job, now)
    limit = len(slots)
    if deadline is not None:
        limit = sum(1 for s in slots if s.start + SLOT <= deadline)
    best = None                                 # the best continuous run
    for i in range(first, limit - n + 1):
        run = sc[i:i + n]
        if any(v is None for v in run):
            continue
        m = sum(run) / n
        if best is None or m < best[1] - 1e-9:
            best = (i, m)
    chosen = tuple(range(best[0], best[0] + n)) if best else None
    mean = best[1] if best else None
    if job.split:
        pool = sorted((sc[k], k) for k in range(first, limit) if sc[k] is not None)
        if len(pool) >= n:
            pieces = tuple(sorted(k for _, k in pool[:n]))
            m = sum(sc[k] for k in pieces) / n
            if mean is None or m < mean - 1e-9:    # pieces only when they actually beat one block
                chosen, mean = pieces, m
    if chosen is None:
        return None
    picked = [slots[k] for k in chosen]
    return Window(job, chosen[0], chosen[-1], mean, _mean([s.carbon for s in picked]),
                  _mean([s.price for s in picked]), picked[0].start, picked[-1].start + SLOT, chosen)


def window_now(slots, job, now):
    """Mean carbon and price if the job started right now (for 'saves 40%')."""
    i = now_index(slots, now)
    run = slots[i:i + job.slots]
    return _mean([s.carbon for s in run]), _mean([s.price for s in run])


def next_green(slots, now):
    """(slot, is_now) for the first green slot from the current half hour on, or None."""
    i = now_index(slots, now)
    for j in range(i, len(slots)):
        if slots[j].green:
            return slots[j], j == i
    return None


def green_until(slots, now):
    """End of the green run that contains now, or None if now is not green."""
    i = now_index(slots, now)
    if i >= len(slots) or not slots[i].green:
        return None
    j = i
    while j + 1 < len(slots) and slots[j + 1].green:
        j += 1
    return slots[j].end


def extremes(slots, now, attr):
    """(lowest slot, highest slot) by ``attr`` from now on."""
    fut = [s for s in slots[now_index(slots, now):] if getattr(s, attr) is not None]
    if not fut:
        return None, None
    return min(fut, key=lambda s: getattr(s, attr)), max(fut, key=lambda s: getattr(s, attr))


def fmt_in(delta):
    """timedelta -> 'now', '25m', '2h', '3h30', '1d4h'."""
    mins = int(round(delta.total_seconds() / 60))
    if mins <= 0:
        return "now"
    if mins < 60:
        return "%dm" % mins
    h, m = divmod(mins, 60)
    if h >= 24:
        d, h = divmod(h, 24)
        return "%dd%dh" % (d, h)
    m = m // 30 * 30 if m else 0
    return "%dh%02d" % (h, m) if m else "%dh" % h
