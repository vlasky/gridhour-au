"""Turn the state into a frame.

Full layout, top to bottom: title bar; a mirrored 48 hour chart with carbon rising from the
time axis and price hanging below it; the generation mix and one row per job, all on the same
time axis so the cursor runs straight down through everything; then a status line and keys.
"""
import time
from datetime import timedelta

from . import plan
from .canvas import Canvas
from .grid import SLOT, WINDOW_SLOTS, floor_slot, local
from .themes import C, carbon_color, fuel_color, mix, price_color, score_color

EIGHTHS = " ▁▂▃▄▅▆▇█"
GUTTER = 22
MAX_W, MAX_H = 1000, 400        # a frame bigger than this is drawn at this size
WINDOW = SLOT * WINDOW_SLOTS
FUEL_NAMES = {"wind": "Wind", "solar": "Grid solar", "rooftop": "Rooftop PV", "gas": "Gas", "hydro": "Hydro",
              "coal": "Coal", "battery": "Battery", "other": "Other"}
PRICE_SCALE_MAX = 50            # c/kWh: a price spike would flatten every other bar, so taller ones are cut


# ---------------------------------------------------------------- formatting

def hm(t, h12=False):
    lt = local(t)
    if not h12:
        return lt.strftime("%H:%M")
    h = lt.hour % 12 or 12
    return ("%d:%02d" % (h, lt.minute)) + ("am" if lt.hour < 12 else "pm")


def job_hm(t, h12=False):
    """Disambiguate both occurrences of a repeated local time."""
    lt = local(t)
    ambiguous = lt.replace(fold=0).utcoffset() != lt.replace(fold=1).utcoffset()
    return hm(t, h12) + (" " + lt.tzname() if ambiguous else "")


def day_hm(t, now, h12=False):
    """'14:30', 'tomorrow 02:00' style but short: 'Sun 02:00' when it is not today."""
    lt, ln = local(t), local(now)
    s = job_hm(t, h12)
    return s if lt.date() == ln.date() else lt.strftime("%a ") + s


def fmt_price(p, digits=1):
    if p is None:
        return "–"
    return ("%.*fc" % (digits, p)).replace("-0.0c", "0.0c")


def tz_name(t):
    return local(t).tzname()


# ---------------------------------------------------------------- geometry

class Axis:
    """Maps screen columns of the timeline to times and back."""

    def __init__(self, x0, width, start):
        self.x0, self.w, self.start = x0, max(1, width), start

    def slot(self, col):
        return min(WINDOW_SLOTS - 1, int((col + 0.5) * WINDOW_SLOTS / self.w))

    def col(self, t):
        f = (t - self.start) / WINDOW
        return int(f * self.w) if 0 <= f < 1 else None

    def time(self, col):
        return self.start + WINDOW * ((col + 0.5) / self.w)

    def span(self, a, b):
        """Columns covering slots a..b inclusive. On a narrow screen there are fewer columns than
        half hours, so a short window may own no column at all; it then gets the column where it
        starts, rather than none (which used to crash the label next to it)."""
        cols = [c for c in range(self.w) if a <= self.slot(c) <= b]
        return cols or [min(self.w - 1, a * self.w // WINDOW_SLOTS)]


def nice_ceiling(v, floor):
    v = max(v, floor)
    for step in (5, 10, 20, 25, 50, 100):
        if v / step <= 6:
            return -(-v // step) * step
    return -(-v // 100) * 100


# ---------------------------------------------------------------- entry

def compose(W, H, st, now, live):
    W, H = max(1, min(W, MAX_W)), max(1, min(H, MAX_H))
    cv = Canvas(W, H)
    for y in range(H):
        cv.fill(0, y, W, C.BG)
    if W < 40 or H < 8:
        cv.put(0, 0, "gridhour-au needs at least 40x8"[:W], C.DIM)
        return cv
    fc = st.fc
    slots = fc.slots
    mode = plan.effective_mode(slots, st.mode)
    sc = plan.scores(slots, mode)
    wins = [plan.best_window(slots, sc, j, live) for j in st.jobs]
    compact = st.compact == "on" or (st.compact == "auto" and (W < 80 or H < 22))
    draw_top(cv, st, fc, now, live, W)
    axis = Axis(GUTTER, W - GUTTER - 1, slots[0].start if slots else floor_slot(now))
    cur = fc.at(now)
    win = wins[st.sel] if wins and st.sel < len(wins) else None
    y = 1
    footer = 2
    if not slots or all(s.carbon is None for s in slots):
        msg = "fetching the forecast…" if st.loading or not fc.errors else fc.errors[0]
        cv.put(max(0, (W - len(msg)) // 2), H // 2, msg[:W], C.AMBER if fc.errors else C.DIM)
        draw_footer(cv, st, fc, now, live, None, None, W, H)
        return cv
    jobs_h = len(st.jobs) + 1
    fuels = top_fuels(slots, 5) if st.mix else []
    if compact:
        y = 2
        y = strip_row(cv, y, axis, "Carbon", fmt_g(cur), carbon_strip(slots, live), now, live, C.TEXT)
        if fc.has_prices:
            y = strip_row(cv, y, axis, "Price", fmt_price(cur.price if cur else None), price_strip(slots, live),
                          now, live, C.TEXT)
        hour_ticks(cv, y, axis, st.h12, small=True)
        y += 1
        y = draw_jobs(cv, y + 1, axis, st, slots, sc, wins, mode, now, live, H - footer - y - 2)
    else:
        mix_h = (len(fuels) + 1) if fuels and H >= 30 else 0
        if not mix_h:
            fuels = []
        fixed = 1 + 1 + 1 + 1 + 1 + 1 + mix_h + jobs_h + 1 + footer
        room = max(6, H - fixed - 1)
        hp = max(2, room * 2 // 5) if fc.has_prices else 0
        hc = room - hp
        y = 2
        draw_bracket(cv, y, axis, win, st.h12)
        y += 1
        draw_chart(cv, y, axis, slots, hc, hp, win, now, live, st.h12)
        y += hc + 1 + hp
        cursor_row(cv, y, axis, now, live, st.h12)
        y += 1
        info_line(cv, y, cur, now, st.h12, W)
        y += 2
        if fuels:
            section(cv, y, "generation mix", W)
            y += 1
            for f in fuels:
                y = fuel_row(cv, y, axis, slots, f, cur, now, live)
        draw_jobs(cv, y, axis, st, slots, sc, wins, mode, now, live, H - footer - y)
    draw_footer(cv, st, fc, now, live, win, slots, W, H)
    return cv


# ---------------------------------------------------------------- pieces

def fmt_g(s):
    return "–" if s is None or s.carbon is None else "%dg" % s.carbon


def chip(cv, x, y, text, fg, bg, bold=True):
    """Text on a coloured plate with a space either side. Returns the column after it."""
    text = " %s " % text
    cv.put(x, y, text, fg, bg, bold)
    return x + len(text)


def draw_top(cv, st, fc, now, live, W):
    x = chip(cv, 0, 0, "ϟ gridhour-au", C.INK, C.ACCENT)     # not ⚡: that one is two columns wide
    where = [fc.region]
    if fc.postcode:
        where.append(fc.postcode)
    where.append(plan.MODE_LABEL[plan.effective_mode(fc.slots, st.mode)])
    lt = local(now)
    clock = hm(now, st.h12) + " " + lt.tzname()
    if st.frozen:
        d = now - floor_slot(live)
        sign = "+" if d >= timedelta(0) else "-"
        clock = "%s%s → %s" % (sign, plan.fmt_in(abs(d)).replace("now", "0m"), clock)
        cfg, cbg = C.INK, C.AMBER
    else:
        cfg, cbg = C.WHITE, C.SEL_BG
    rx = W - len(clock) - 2
    chip(cv, rx, 0, clock, cfg, cbg)
    if st.loading:
        rx -= 11
        cv.put(rx, 0, "updating…", C.DIM)
    date = lt.strftime("%a %-d %b")
    if rx - len(date) - 2 > x + 10:
        rx -= len(date) + 2
        cv.put(rx, 0, date, C.DIM)
    # drop trailing pieces until the place fits, rather than cutting a word in half
    while where and x + 1 + len("  ".join(where)) > rx - 2:
        where.pop()
    for i, w in enumerate(where):
        cv.put(x + 1, 0, w, C.TEXT if i == 0 else C.DIM, None, i == 0)
        x += len(w) + 2


def draw_bracket(cv, y, axis, win, h12):
    if not win:
        return
    cols = axis.span(win.first, win.last)
    if not cols:
        return
    a, b = cols[0], cols[-1]
    x0 = axis.x0
    color = C.CLEAN
    if b > a:
        cv.put(x0 + a, y, "╭" + "─" * (b - a - 1) + "╮", color)
    else:
        cv.put(x0 + a, y, "▾", color)
    label = " %s %s–%s " % (win.job.name, job_hm(win.start, h12), job_hm(win.end, h12))
    if len(win.parts()) > 1:
        label = " %s, %d pieces, %s–%s " % (win.job.name, len(win.parts()),
                                           job_hm(win.start, h12), job_hm(win.end, h12))
    if len(label) <= b - a - 1:
        cv.put(x0 + a + (b - a + 1 - len(label)) // 2, y, label, C.WHITE, None, True)
    else:
        lx = x0 + b + 2 if x0 + b + 2 + len(label) < cv.w else x0 + a - len(label) - 1
        cv.put(max(x0, lx), y, label.strip(), color)


def draw_chart(cv, y0, axis, slots, hc, hp, win, now, live, h12):
    """Carbon bars rise from the axis row, price bars hang below it."""
    x0 = axis.x0
    cmax = nice_ceiling(max((s.carbon or 0) for s in slots), 100)
    prices = [abs(s.price) for s in slots if s.price is not None]
    pmax = nice_ceiling(min(max(prices), PRICE_SCALE_MAX), 10) if prices else 10
    win_cols = {c for c in range(axis.w) if win.covers(axis.slot(c))} if win else set()
    cur_col = axis.col(floor_slot(now))
    now_col = axis.col(floor_slot(live))
    win_bg = mix(C.BG, C.CLEAN, 0.08)
    axis_y = y0 + hc
    # gutter labels
    for frac, label in ((1.0, "%d" % cmax), (0.5, "%d" % (cmax // 2))):
        yy = y0 + int(round((1 - frac) * (hc - 1)))
        cv.put(GUTTER - len(label) - 2, yy, label, C.DIM)
    cv.put(1, y0, "gCO₂e/kWh est.", C.DIM)
    if hp:
        cv.put(1, axis_y + hp, "spot c/kWh", C.DIM)
        label = "%d" % pmax
        cv.put(GUTTER - len(label) - 2, axis_y + hp, label, C.DIM)
    for c in range(axis.w):
        x = x0 + c
        s = slots[axis.slot(c)]
        past = s.start < floor_slot(live)
        bg = C.SEL_BG if c == cur_col else (win_bg if c in win_cols else C.BG)
        for r in range(hc):
            cv.fill(x, y0 + r, 1, bg)
        for r in range(hp):
            cv.fill(x, axis_y + 1 + r, 1, bg)
        if s.carbon is not None:
            col = carbon_color(s.carbon)
            if c == cur_col:
                col = mix(col, C.WHITE, 0.3)
            elif past:
                col = mix(col, C.BG, 0.6)
            h = int(round(s.carbon / cmax * hc * 8))
            for r in range(hc):
                fill = max(0, min(8, h - r * 8))
                if fill == 8:
                    cv.fill(x, y0 + hc - 1 - r, 1, col)     # a coloured cell, not █: no seams between cells
                elif fill:
                    cv.put(x, y0 + hc - 1 - r, EIGHTHS[fill], col, bg)
        if hp and s.price is not None:
            col = price_color(s.price)
            if past:
                col = mix(col, C.BG, 0.6)
            h = max(1, int(round(abs(s.price) / pmax * hp * 8)))
            for r in range(hp):
                fill = max(0, min(8, h - r * 8))
                y = axis_y + 1 + r
                # hanging bars end in a top-aligned block; there are only ▔ (1/8) and ▀ (1/2), so the end
                # snaps to them rather than faking other heights with inverted colours, which leaves a
                # dark step with a coloured sliver in many fonts
                if fill >= 6:
                    cv.fill(x, y, 1, col)
                elif fill >= 3:
                    cv.put(x, y, "▀", col, bg)
                elif fill:
                    cv.put(x, y, "▔", col, bg)
        elif hp:
            cv.put(x, axis_y + 1, "·", C.BORDER, bg)
    hour_ticks(cv, axis_y, axis, h12)
    if now_col is not None:
        cv.put(x0 + now_col, axis_y, "┃", C.WHITE, C.BG, True)


def hour_ticks(cv, y, axis, h12, small=False):
    """Hour labels along the time axis, the day name at midnight."""
    x0 = axis.x0
    for c in range(axis.w):
        cv.put(x0 + c, y, "─", C.BORDER)
    step = 3 if axis.w >= 90 else 6
    if axis.w < 32 or small and axis.w < 60:     # under 32 columns, 6-hourly labels crowd out the day names
        step = 12
    last = -10
    labels = []                     # (column, text, colour, kind): an hour, a day name or a zone
    for c in range(axis.w):
        t0 = axis.start + WINDOW * (c / axis.w)
        t1 = axis.start + WINDOW * ((c + 1) / axis.w)
        # the first local whole hour in this column that is due a label: below 48 columns a column
        # spans more than an hour, and its first whole hour (23:00, say) may not be. Adelaide's
        # hours fall on UTC half hours.
        b = t0.replace(minute=t0.minute - t0.minute % 30, second=0, microsecond=0)
        if b < t0:
            b += timedelta(minutes=30)
        while b < t1 and (local(b).minute or local(b).hour % step):
            b += timedelta(minutes=30)
        if b >= t1:
            continue
        boundary = local(b)
        if boundary.hour == 0:
            label, color = boundary.strftime("%a"), C.WHITE
        elif h12:
            label, color = "%d%s" % (boundary.hour % 12 or 12, "a" if boundary.hour < 12 else "p"), C.DIM
        else:
            label, color = "%02d" % boundary.hour, C.DIM
        if c - last < len(label) + 1 or c + len(label) > axis.w:
            continue
        labels.append((c, label, color, "day" if boundary.hour == 0 else "hour"))
        last = c

    # Mark a clock change with the new zone's name, as close after it as fits. Clocks here change
    # at 02:00 or 03:00, on top of an hour label, so the mark takes the place of hour labels it
    # would touch, but not of a day name or another mark; if one is in the way, there is no mark.
    for k in range(1, WINDOW_SLOTS):
        t = axis.start + SLOT * k
        c = axis.col(t)
        if tz_name(t) == tz_name(t - SLOT) or c is None:
            continue
        text = tz_name(t)
        for start in range(c, min(c + max(2, axis.w // 48), axis.w - len(text)) + 1):
            hit = [lb for lb in labels if lb[0] + len(lb[1]) >= start and start + len(text) >= lb[0]]
            if all(lb[3] == "hour" for lb in hit):
                labels = [lb for lb in labels if lb not in hit] + [(start, text, C.WHITE, "zone")]
                break
    for c, text, color, kind in labels:
        cv.put(x0 + c, y, text, color, None, kind != "hour")


def cursor_row(cv, y, axis, now, live, h12):
    c = axis.col(floor_slot(now))
    if c is None:
        return
    label = "↑ " + hm(now, h12)
    x = axis.x0 + c - 1                     # the arrow sits right under the cursor column
    if x + len(label) + 2 > cv.w - 1:
        label = hm(now, h12) + " ↑"
        x = axis.x0 + c - len(label)
    chip(cv, x, y, label, C.INK, C.ACCENT)


def info_line(cv, y, s, now, h12, W):
    if s is None:
        return
    x = chip(cv, 0, y, "%s %s–%s %s" % (local(s.start).strftime("%a"), hm(s.start, h12), hm(s.end, h12),
                                       tz_name(s.start)), C.WHITE, C.BAR_BG)
    parts = []
    if s.carbon is not None:
        parts += [("   ", None), ("~%d gCO₂e/kWh" % s.carbon, carbon_color(s.carbon)),
                  (" " + (s.index or ""), C.DIM)]
    if s.price is not None:
        parts += [("   ", None), ("%.1fc/kWh spot" % s.price, price_color(s.price))]
        if s.price < 0:
            parts.append((" negative", C.PLUNGE))
    if s.mix:
        top = sorted(((v, k) for k, v in s.mix.items() if v >= 1), reverse=True)[:4]
        parts.append(("   ", None))
        for i, (v, k) in enumerate(top):
            if i:
                parts.append((" · ", C.BORDER))
            parts.append(("%s %d%%" % (FUEL_NAMES.get(k, k).lower(), round(v)), fuel_color(k)))
    for text, color in parts:
        if x + len(text) > W - 1:
            break
        cv.put(x, y, text, color or C.DIM)
        x += len(text)


def section(cv, y, title, W):
    cv.put(0, y, "▍", C.ACCENT)
    cv.put(2, y, title.upper(), C.DIM, None, True)


def top_fuels(slots, n):
    totals = {}
    for s in slots:
        for k, v in (s.mix or {}).items():
            totals[k] = totals.get(k, 0) + v
    ranked = sorted(totals, key=lambda k: -totals[k])
    return [k for k in ranked if totals[k] > 0][:n]


def fuel_row(cv, y, axis, slots, fuel, cur, now, live):
    col = fuel_color(fuel)
    vals = [(s.mix or {}).get(fuel) for s in slots]
    top = max([v for v in vals if v is not None] + [30])
    cells = []
    for s, v in zip(slots, vals, strict=True):
        if v is None:
            cells.append((C.BAR_BG, None))
        else:
            c = mix(C.BAR_BG, col, 0.12 + 0.88 * v / top)
            cells.append((mix(c, C.BG, 0.55) if s.end <= floor_slot(live) else c, None))
    v = (cur.mix or {}).get(fuel) if cur else None
    return strip_row(cv, y, axis, FUEL_NAMES.get(fuel, fuel.title()), "–" if v is None else "%d%%" % round(v),
                     cells, now, live, col)


def carbon_strip(slots, live):
    out = []
    for s in slots:
        c = carbon_color(s.carbon) if s.carbon is not None else C.BAR_BG
        out.append((mix(c, C.BG, 0.6) if s.end <= floor_slot(live) else c, None))
    return out


def price_strip(slots, live):
    out = []
    for s in slots:
        c = price_color(s.price) if s.price is not None else C.BAR_BG
        out.append((mix(c, C.BG, 0.6) if s.end <= floor_slot(live) else c, None))
    return out


def strip_row(cv, y, axis, label, value, cells, now, live, label_color, sel=False):
    """A gutter label and value, then one coloured cell per column of the time axis."""
    if sel:
        cv.put(0, y, "▌", C.ACCENT)
    cv.put(3, y, label[:GUTTER - 11], C.WHITE if sel else label_color, None, sel)
    cv.put(GUTTER - 2 - len(value), y, value, C.ACCENT if sel else C.TEXT, None, sel)
    cur_col = axis.col(floor_slot(now))
    # bars three quarters high, so the rows stand apart instead of merging into one block
    for c in range(axis.w):
        color, ch = cells[axis.slot(c)]
        x = axis.x0 + c
        if c == cur_col:
            cv.put(x, y, "┃", C.ACCENT, C.BG, True)
        elif ch:
            cv.put(x, y, ch[0], ch[1], C.BG)
        elif color != C.BG:
            cv.put(x, y, "▆", color, C.BG)
    return y + 1


def draw_jobs(cv, y, axis, st, slots, sc, wins, mode, now, live, room):
    title = "best time to run · " + plan.MODE_LABEL[mode]
    section(cv, y, title, cv.w)
    if st.mode != mode:
        cv.put(len(title) + 4, y, " (no prices, so carbon only) ", C.AMBER)
    y += 1
    now_slot = floor_slot(live)
    for i, (job, win) in enumerate(zip(st.jobs, wins, strict=True)):
        if room <= 1:
            break
        cells = []
        for k, s in enumerate(slots):
            v = sc[k]
            if s.start < now_slot or v is None:
                cells.append((C.BG if s.start < now_slot else C.BAR_BG, None))
            elif win and win.covers(k):
                cells.append((score_color(v), None))
            else:
                cells.append((mix(C.BAR_BG, score_color(v), 0.28), None))
        sel = i == st.sel
        strip_row(cv, y, axis, job.name, plan.fmt_duration(job.minutes), cells, now, live,
                  C.WHITE if sel else C.TEXT, sel)
        due = plan.deadline_at(job, live)
        due_col = axis.col(due - SLOT) if due else None
        if due_col is not None and due_col != axis.col(floor_slot(now)):
            cv.put(axis.x0 + due_col, y, "┤", C.AMBER, C.BG, True)     # last half hour before the deadline
        if win:
            label_window(cv, y, axis, win, st.h12, live)
        elif job.deadline:
            cv.put(axis.x0 + 1, y, "not enough forecast before %s for %s" % (job_hm(due, st.h12) if due else
                                                                           job.deadline, job.describe()), C.DIM)
        else:
            cv.put(axis.x0 + 1, y, "no %s window in the forecast yet" % plan.fmt_duration(job.minutes), C.DIM)
        y += 1
        room -= 1
    return y


def label_window(cv, y, axis, win, h12, live):
    cols = axis.span(win.first, win.last)
    pieces = len(win.parts())
    if pieces > 1:
        text = " %d pieces, done %s" % (pieces, job_hm(win.end, h12))
    else:
        text = " %s–%s" % (job_hm(win.start, h12), job_hm(win.end, h12))
    if win.carbon is not None:
        text += " · %dg" % round(win.carbon)
    if win.price is not None:
        text += " · %s" % fmt_price(win.price)
    text += " "
    a, b = axis.x0 + cols[0], axis.x0 + cols[-1]
    if b + 1 + len(text) <= cv.w - 1:
        x = b + 1
    elif a - len(text) >= axis.x0:
        x = a - len(text)
    else:
        return
    cv.put(x, y, text, C.WHITE)          # keeps the strip colours underneath


def draw_footer(cv, st, fc, now, live, win, slots, W, H):
    y = H - 2
    if st.prompt in ("job", "postcode", "deadline"):
        q = {"job": "Add a job (Dryer 1h30, or EV 4h by 07:00 split): ",
             "postcode": "Postcode (2000) or region (nsw, vic, qld, sa, tas): ",
             "deadline": "Finish %s by (07:00, 7am), empty for no deadline: " % (st.job.name if st.job else ""),
             }[st.prompt]
        cv.fill(0, y, W, C.PANEL_BG)
        cv.put(1, y, q, C.AMBER, C.PANEL_BG, True)
        cv.put(1 + len(q), y, st.buf + "▏", C.WHITE, C.PANEL_BG)
        if st.err:
            cv.put(W - len(st.err) - 2, y, st.err, C.RED, C.PANEL_BG)
    else:
        text, color, until = st.msg
        if text and time.time() < until:
            cv.put(1, y, text[:W - 2], color)
        elif win and slots:
            g_now, p_now = plan.window_now(slots, win.job, live)
            x = chip(cv, 0, y, "%s %s" % (win.job.name, plan.fmt_duration(win.job.minutes)), C.INK, C.ACCENT)
            start_in = win.start - live
            parts = [(" start %s" % ("now" if start_in.total_seconds() <= 0
                                     else day_hm(win.start, live, st.h12) + ", in %s" % plan.fmt_in(start_in)),
                      C.TEXT)]
            if len(win.parts()) > 1:
                parts.append((", %d pieces, done %s" % (len(win.parts()), job_hm(win.end, st.h12)), C.TEXT))
            if win.carbon is not None and g_now:
                d = (win.carbon - g_now) / g_now * 100
                parts.append((" · %dg" % round(win.carbon), carbon_color(win.carbon)))
                if abs(d) >= 1:
                    parts.append((" (%+d%%)" % round(d), C.DIM))
            if win.price is not None and p_now is not None:
                parts.append((" · %s" % fmt_price(win.price), price_color(win.price)))
                dp = win.price - p_now
                if abs(dp) >= 0.1:
                    parts.append((" (%+.1fc)" % dp, C.DIM))
            parts.append(("  vs starting now", C.BORDER))
            for text, color in parts:
                if x + len(text) > W - 22:
                    break
                cv.put(x, y, text, color)
                x += len(text)
        age = data_age(fc, live)
        if fc.errors:
            age = (fc.errors[0][:40] + " · " + age) if age else fc.errors[0][:40]
        cv.put(W - len(age) - 1, y, age, C.AMBER if fc.errors else C.DIM)
    keys = [("←→", "time"), ("↑↓", "job"), ("w", "green/cheap"), ("+-", "length"), ("a", "add"), ("d", "delete"),
            ("p", "postcode"), ("?", "help"), ("q", "quit"), ("r", "now"), ("R", "refresh"),
            ("m", "mix"), ("T", "theme")]
    x = 0
    for k, label in keys:
        if x + len(k) + len(label) + 4 > W:
            break
        x = chip(cv, x, H - 1, k, C.KEY, C.BAR_BG)
        cv.put(x + 1, H - 1, label, C.DIM)
        x += len(label) + 2
    if st.prompt == "help":
        draw_help(cv, W, H)


def data_age(fc, live):
    if not fc.fetched:
        return ""
    mins = int((live.timestamp() - fc.fetched) // 60)
    return "data just now" if mins < 1 else "data %s old" % plan.fmt_in(timedelta(minutes=mins))


HELP = [
    ("← →", "move the cursor 30 minutes (Shift or H L: 2 hours)"),
    ("Home End", "start and end of the forecast"),
    ("r", "back to now"),
    ("↑ ↓ j k", "select a job"),
    ("+ -", "make the selected job 30 minutes longer or shorter"),
    ("a d", "add a job (EV 4h by 07:00 split), delete one"),
    ("b s", "selected job: finish-by time, may run in pieces"),
    ("Enter", "jump the cursor to the selected job's best window"),
    ("w", "rank windows by carbon, by price, or both"),
    ("p", "set your postcode or NEM region (nsw, vic, qld, sa, tas)"),
    ("m", "show or hide the generation mix"),
    ("R", "fetch fresh data now"),
    ("T t c", "theme, 12/24 hour clock, compact layout"),
    ("q", "quit"),
]


def draw_help(cv, W, H):
    w = min(W - 4, 72)
    h = len(HELP) + 4
    x0, y0 = (W - w) // 2, max(1, (H - h) // 2)
    for y in range(h):
        cv.fill(x0, y0 + y, w, C.PANEL_BG)
    cv.put(x0, y0, "╭" + "─" * (w - 2) + "╮", C.FRAME, C.PANEL_BG)
    cv.put(x0, y0 + h - 1, "╰" + "─" * (w - 2) + "╯", C.FRAME, C.PANEL_BG)
    for y in range(1, h - 1):
        cv.put(x0, y0 + y, "│", C.FRAME, C.PANEL_BG)
        cv.put(x0 + w - 1, y0 + y, "│", C.FRAME, C.PANEL_BG)
    cv.put(x0 + 2, y0, " keys ", C.WHITE, C.PANEL_BG, True)
    for i, (k, d) in enumerate(HELP):
        cv.put(x0 + 3, y0 + 2 + i, k, C.KEY, C.PANEL_BG, True)
        cv.put(x0 + 14, y0 + 2 + i, d[:w - 17], C.TEXT, C.PANEL_BG)
