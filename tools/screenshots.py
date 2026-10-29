"""Render frames to PNG for the README. Needs Pillow and the DejaVu Sans Mono fonts.

    python3 tools/screenshots.py            # regenerate docs/*.png from the test fixtures
    python3 tools/screenshots.py --live     # the same from today's real data

Frames are drawn straight from the Canvas (characters plus colours), so there is no ANSI parsing.
Block characters are painted as rectangles so the bars line up without gaps.
"""
import argparse
import functools
import os
import sys
import tempfile
from datetime import UTC, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw, ImageFont  # noqa: E402

from gridhour_au.compose import compose  # noqa: E402
from gridhour_au.grid import CURRENT, REPORTS, load, set_zone  # noqa: E402
from gridhour_au.state import State  # noqa: E402
from gridhour_au.themes import C, apply_theme  # noqa: E402

FONT_DIRS = ["/usr/share/fonts/truetype", "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/TTF",
             "/Library/Fonts", os.path.expanduser("~/Library/Fonts")]
CW, CH, SIZE = 9, 19, 15
LOWER = " ▁▂▃▄▅▆▇█"
UPPER = {"▔": 1, "▀": 4}         # eighths filled from the top
ROOT = os.path.join(os.path.dirname(__file__), "..")
FIXTURE_NOW = datetime(2026, 10, 9, 5, 50, tzinfo=UTC)     # 16:50 in Sydney, as in tests/conftest.py


@functools.cache
def font(bold=False):
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, SIZE)
    sys.exit("DejaVu Sans Mono not found")


def draw(cv, pad=10):
    """The canvas as a picture: one CW x CH cell per character."""
    img = Image.new("RGB", (cv.w * CW + pad * 2, cv.h * CH + pad * 2), C.BG)
    d = ImageDraw.Draw(img)
    regular, bold = font(), font(True)
    for y in range(cv.h):
        for x in range(cv.w):
            c = cv.cell(x, y)
            ch, fg, bg, b = c.char, c.fg, c.bg, c.bold
            fg = fg or C.TEXT
            px, py = pad + x * CW, pad + y * CH
            d.rectangle([px, py, px + CW - 1, py + CH - 1], fill=bg or C.BG)
            if ch in LOWER and ch != " ":
                h = round(CH * LOWER.index(ch) / 8)
                d.rectangle([px, py + CH - h, px + CW - 1, py + CH - 1], fill=fg)
            elif ch in UPPER:
                d.rectangle([px, py, px + CW - 1, py + round(CH * UPPER[ch] / 8) - 1], fill=fg)
            elif ch != " ":
                d.text((px, py + 1), ch, font=bold if b else regular, fill=fg)
    return img


def render(cv, path, pad=10):
    draw(cv, pad).save(path)
    print("wrote", path)


def fixture_state(now=FIXTURE_NOW, live=False):
    """A State on the recorded Sydney data (or today's, with live=True), with a throwaway cache."""
    os.environ["XDG_CACHE_HOME"] = tempfile.mkdtemp()
    st = State()
    st.persist = False
    st.postcode = "2000"
    st.fc = load(now, "2000", force=True, fetch=None if live else fixture_fetch)
    set_zone(st.fc.region_id)
    return st


def big_font(size, bold=True):
    name = "DejaVuSansMono-Bold.ttf" if bold else "DejaVuSansMono.ttf"
    for d in FONT_DIRS:
        p = os.path.join(d, name)
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    sys.exit("DejaVu Sans Mono not found")


def social_preview(st, now, path):
    """1280x640 card for link previews (GitHub: Settings -> General -> Social preview)."""
    apply_theme("carbon")
    st.frozen = None
    shot = draw(compose(92, 24, st, now, now), pad=0)
    card = Image.new("RGB", (1280, 640), C.BG)
    d = ImageDraw.Draw(card)
    # the UI on the right, cut off by the edge on purpose
    card.paste(shot, (600, 150))
    d.rectangle([0, 0, 580, 640], fill=C.BG)
    logo = big_font(46)
    lw = int(d.textlength("ϟ gridhour-au", font=logo))
    d.rounded_rectangle([60, 70, 60 + lw + 52, 150], radius=14, fill=C.ACCENT)
    d.text((86, 82), "ϟ gridhour-au", font=logo, fill=C.INK)
    y = 200
    for line in ("When Australian", "electricity is", "green and cheap"):
        d.text((60, y), line, font=big_font(40), fill=C.WHITE)
        y += 54
    d.text((60, 390), "48 h of estimated carbon and", font=big_font(22, False), fill=C.DIM)
    d.text((60, 420), "NEM spot prices, and the", font=big_font(22, False), fill=C.DIM)
    d.text((60, 450), "best time to run each job.", font=big_font(22, False), fill=C.DIM)
    cmd = big_font(22)
    cw = int(d.textlength("$ pipx install gridhour-au", font=cmd))
    d.rounded_rectangle([60, 520, 60 + cw + 40, 572], radius=10, fill=C.BAR_BG)
    d.text((80, 531), "$ pipx install gridhour-au", font=cmd, fill=C.ACCENT)
    card.save(path)
    print("wrote", path)


def fixture_fetch(url):
    """tests/fixtures served the way NEMWeb serves them (tests/conftest.py does the same for the tests)."""
    import re
    fx = os.path.join(ROOT, "tests", "fixtures")
    folder, _, name = url[len(CURRENT):].rpartition("/")
    if name:
        with open(os.path.join(fx, name), "rb") as f:
            return f.read()
    report = next(r for r in REPORTS.values() if r.folder == folder)
    return "".join('<A HREF="/Reports/CURRENT/%s/%s">x</A>' % (folder, n) for n in sorted(os.listdir(fx))
                   if re.fullmatch(report.name, n)).encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true")
    ap.add_argument("--only", help="render one shot by name")
    args = ap.parse_args()
    now = datetime.now(UTC).replace(microsecond=0) if args.live else FIXTURE_NOW
    st = fixture_state(now, args.live)
    out = os.path.join(ROOT, "docs")
    os.makedirs(out, exist_ok=True)
    shots = {
        "main": dict(W=120, H=40),
        "compact": dict(W=72, H=16),
        "light": dict(W=120, H=40, theme="daylight"),
        "scrub": dict(W=120, H=40, frozen=2 * 3600 * 6),
    }
    if not args.only or args.only == "social":
        social_preview(st, now, os.path.join(out, "social-preview.png"))
    for name, o in shots.items():
        if args.only and name != args.only:
            continue
        apply_theme(o.get("theme", "carbon"))
        st.frozen = now + timedelta(seconds=o["frozen"]) if o.get("frozen") else None
        st.frozen = st.frozen.replace(minute=0) if st.frozen else None
        render(compose(o["W"], o["H"], st, st.frozen or now, now), os.path.join(out, name + ".png"))


if __name__ == "__main__":
    main()
