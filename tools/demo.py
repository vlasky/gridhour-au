"""Record docs/demo.gif: a short session on the recorded Sydney data, driven through the real
key handler. Needs Pillow and the DejaVu fonts, like screenshots.py.

    python3 tools/demo.py

Every frame is composed by gridhour-au itself; a caption bar under the terminal shows the key pressed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from PIL import Image, ImageDraw  # noqa: E402
from screenshots import FIXTURE_NOW, ROOT, draw, fixture_state, font  # noqa: E402

from gridhour_au.compose import compose  # noqa: E402
from gridhour_au.keys import DOWN, RIGHT, S_RIGHT, handle_key  # noqa: E402
from gridhour_au.themes import C, apply_theme  # noqa: E402

W, H = 110, 34
NOW = FIXTURE_NOW

# (key, label on the badge, how long to hold the frame afterwards in ms)
SCRIPT = (
    [(None, None, 1600)]
    + [(RIGHT, "→", 90)] * 10
    + [(S_RIGHT, "⇧→", 160)] * 6
    + [(None, None, 700)]
    + [("r", "r  back to now", 900)]
    + [("\r", "⏎  jump to best window", 1600)]
    + [(DOWN, "↓  next job", 900), ("\r", "⏎", 1100)] * 2
    + [("+", "+  30 min longer", 700), ("+", "+", 1100)]
    + [("w", "w  rank by carbon", 1500), ("\r", "⏎", 1300)]
    + [("w", "w  rank by price", 1500), ("\r", "⏎", 1300)]
    + [("w", "w  carbon + price", 900), ("r", "r", 600)]
    + [("T", "T  theme", 1000)] * 3
    + [("T", "T", 300)] * 3
    + [("?", "?  help", 1800), ("x", None, 1400)]
)
CAPTION = 46


def with_caption(frame, text):
    """The frame with a bar underneath; the key and what it does sit in the bar, not on the UI."""
    img = Image.new("RGB", (frame.width, frame.height + CAPTION), C.TOP_BG)
    img.paste(frame, (0, 0))
    if text:
        d = ImageDraw.Draw(img)
        f = font(True)
        key, _, what = text.partition("  ")
        kw = int(d.textlength(key, font=f)) + 20
        x0, y0 = 14, frame.height + 8
        d.rounded_rectangle([x0, y0, x0 + kw, y0 + 30], radius=7, fill=C.ACCENT)
        d.text((x0 + 10, y0 + 6), key, font=f, fill=C.INK)
        if what:
            d.text((x0 + kw + 12, y0 + 6), what, font=f, fill=C.TEXT)
    return img


def main():
    st = fixture_state()
    apply_theme("carbon")
    frames, durations = [], []
    for key, label, hold in SCRIPT:
        if key:
            handle_key(st, key, NOW)
        now = st.frozen or NOW
        img = with_caption(draw(compose(W, H, st, now, NOW)), label)
        frames.append(img.convert("P", palette=Image.Palette.ADAPTIVE, colors=128))
        durations.append(hold)
    out = os.path.join(ROOT, "docs", "demo.gif")
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True,
                   disposal=1)
    print("wrote %s, %d frames, %.1f s, %d kB" % (out, len(frames), sum(durations) / 1000,
                                                  os.path.getsize(out) // 1024))


if __name__ == "__main__":
    main()
