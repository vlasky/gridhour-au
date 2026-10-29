"""Non-interactive output: the one-liner for status bars, tmux codes, JSON and --watch."""
import hashlib
import json
import sys
import time
from datetime import UTC

from . import plan
from .compose import job_hm
from .grid import iso, load, local, set_zone
from .safe import label
from .themes import C, carbon_color, price_color


def _ical_time(value):
    """Format a datetime as an iCalendar UTC timestamp."""
    return value.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _ical_text(text):
    """Escape special characters in iCalendar text."""
    return (text.replace("\\", "\\\\")
            .replace("\r\n", "\n").replace("\r", "\n")
            .replace("\n", "\\n")
            .replace(";", "\\;").replace(",", "\\,"))


def _ical_uid(name, start, part):
    """Identify an event by job name, local window date and part number."""
    day = local(start).date().isoformat()
    key = "%s|%s|%d" % (name, day, part)
    # This hash identifies an event; it is not a security primitive.
    return hashlib.sha1(
        key.encode("utf-8"), usedforsecurity=False
    ).hexdigest() + "@gridhour-au"


def _ical_fold(line):
    """Fold at 75 UTF-8 bytes without splitting a character."""
    parts = []
    current = ""
    size = 0
    for ch in line:
        width = len(ch.encode("utf-8"))
        if size + width > 75:
            parts.append(current)
            current, size = " ", 1
        current += ch
        size += width
    parts.append(current)
    return "\r\n".join(parts)


def _ical_document(lines):
    """Fold content lines and terminate each with CRLF."""
    return "\r\n".join(_ical_fold(line) for line in lines) + "\r\n"


def _lit(text, mode):
    """Literal text for ``mode``. In tmux a '#' starts a format (#[..], #(command)), so it is doubled."""
    return text.replace("#", "##") if mode == "tmux" else text


def _fg(rgb, text, mode):
    text = _lit(text, mode)
    if mode == "ansi":
        return "\x1b[38;2;%d;%d;%dm%s\x1b[0m" % (rgb + (text,))
    if mode == "tmux":
        return "#[fg=#%02x%02x%02x]%s#[default]" % (rgb + (text,))
    return text


def green_phrase(slots, now, h12=False):
    """'green now', 'green in 2h', or 'greenest 03:00' when nothing in the forecast is green."""
    hit = plan.next_green(slots, now)
    if hit:
        slot, is_now = hit
        return "green now" if is_now else "green in " + plan.fmt_in(slot.start - now), True
    best, _ = plan.extremes(slots, now, "carbon")
    if best is None:
        return "no forecast", False
    if best.start <= now:
        return "greenest now", False
    return "greenest in " + plan.fmt_in(best.start - now), False


def line_output(st, now, color="plain", best=None):
    """⚡ 540g · 9c · green in 2h"""
    fc = st.fc
    s = fc.at(now)
    if s is None or s.carbon is None:
        err = fc.errors[0] if fc.errors else "no data"
        return "⚡ " + _lit(label(err, 80) or "error", color)
    parts = [_fg(carbon_color(s.carbon), "%dg" % s.carbon, color)]
    if s.price is not None:
        parts.append(_fg(price_color(s.price), "%dc" % round(s.price), color))
    if best:
        sc = plan.scores(fc.slots, st.mode)
        w = plan.best_window(fc.slots, sc, best, now)
        if w:
            when = "now" if w.start <= now else "%s (in %s)" % (job_hm(w.start, st.h12), plan.fmt_in(w.start - now))
            pieces = len(w.parts())
            if pieces > 1:
                when += ", %d pieces" % pieces
            parts.append(_fg(C.GREEN, "%s best %s" % (best.describe(), when), color))
        else:
            parts.append(_lit("no %s window yet" % plan.fmt_duration(best.minutes), color))
    else:
        phrase, ok = green_phrase(fc.slots, now, st.h12)
        parts.append(_fg(C.GREEN, phrase, color) if ok else _lit(phrase, color))
    return "⚡ " + " · ".join(parts)


def ical_output(st, now):
    """Export the best job windows as an iCalendar document."""
    fc = st.fc
    sc = plan.scores(fc.slots, st.mode)
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//gridhour-au//Job windows//EN",
    ]
    for job in st.jobs:
        w = plan.best_window(fc.slots, sc, job, now)
        if w is None:
            continue

        name = label(job.name) or "Job"
        carbon = "unknown" if w.carbon is None else "%.0f gCO2e/kWh (estimated)" % w.carbon
        price = "unknown" if w.price is None else "%.2f c/kWh (wholesale spot)" % w.price
        description = "Best window average: carbon %s; price %s" % (carbon, price)
        parts = w.parts()
        for number, (first, last) in enumerate(parts, start=1):
            summary = "%s (gridhour-au)" % name
            if len(parts) > 1:
                summary += " (part %d of %d)" % (number, len(parts))

            lines.extend([
                "BEGIN:VEVENT",
                "UID:" + _ical_uid(name, w.start, number),
                "DTSTAMP:" + _ical_time(now),
                "DTSTART:" + _ical_time(fc.slots[first].start),
                "DTEND:" + _ical_time(fc.slots[last].end),
                "SUMMARY:" + _ical_text(summary),
                "DESCRIPTION:" + _ical_text(description),
                "END:VEVENT",
            ])

    lines.append("END:VCALENDAR")
    return _ical_document(lines)


def json_output(st, now, best=None):
    fc = st.fc
    sc = plan.scores(fc.slots, st.mode)
    s = fc.at(now)
    hit = plan.next_green(fc.slots, now)
    until = plan.green_until(fc.slots, now)

    def slot_json(x):
        return {"from": iso(x.start), "to": iso(x.end), "carbon": x.carbon, "index": x.index, "price": x.price,
                "mix": x.mix}

    def window_json(w):
        if w is None:
            return None
        return {"from": iso(w.start), "to": iso(w.end), "starts_in_minutes": _mins(w.start - now),
                "carbon": None if w.carbon is None else round(w.carbon, 1),
                "price": None if w.price is None else round(w.price, 2),
                "parts": [{"from": iso(fc.slots[a].start), "to": iso(fc.slots[b].end)} for a, b in w.parts()]}

    jobs = []
    for job in st.jobs:
        w = plan.best_window(fc.slots, sc, job, now)
        g_now, p_now = plan.window_now(fc.slots, job, now)
        due = plan.deadline_at(job, now)
        jobs.append({"name": job.name, "minutes": job.minutes, "deadline": job.deadline,
                     "deadline_at": iso(due) if due else None, "split": job.split, "best": window_json(w),
                     "if_started_now": {"carbon": None if g_now is None else round(g_now, 1),
                                        "price": None if p_now is None else round(p_now, 2)}})
    doc = {
        "schema": 1,
        "at": iso(now),
        "region": {"id": fc.region_id, "name": fc.region, "postcode": fc.postcode},
        "units": {"carbon": "gCO2e/kWh, estimated", "price": "c/kWh, wholesale spot, excluding GST"},
        "mode": plan.effective_mode(fc.slots, st.mode),
        "now": slot_json(s) if s else None,
        "green_now": bool(s and s.green),
        "green_until": iso(until) if until else None,
        "next_green": None if not hit else {"from": iso(hit[0].start),
                                            "in_minutes": _mins(hit[0].start - now)},
        "jobs": jobs,
        "slots": [slot_json(x) for x in fc.slots if x.carbon is not None or x.price is not None],
        "fetched": iso_ts(fc.fetched),
        "errors": fc.errors,
    }
    if best is not None:
        doc["best"] = window_json(plan.best_window(fc.slots, sc, best, now))
    return json.dumps(doc, indent=1, ensure_ascii=True)     # \uXXXX for anything outside ASCII, C1 included


def _mins(delta):
    return max(0, int(delta.total_seconds() // 60))


def iso_ts(ts):
    from datetime import datetime
    return iso(datetime.fromtimestamp(ts, UTC)) if ts else None


def watch(st, utcnow, color="ansi", best=None, refresh=600):
    """The one-liner, rewritten in place every few seconds, refetching every ``refresh`` seconds."""
    last = 0.0
    width = 0
    try:
        while True:
            now = utcnow()
            if time.time() - last > refresh:
                st.fc = load(now, st.postcode, st.region_id, st.prices)
                set_zone(st.fc.region_id)
                last = time.time()
            text = line_output(st, now, color, best)
            sys.stdout.write("\r" + text + " " * max(0, width - len(text)))
            sys.stdout.flush()
            width = len(text)
            time.sleep(15)
    except KeyboardInterrupt:
        sys.stdout.write("\n")
