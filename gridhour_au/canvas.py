"""An off-screen frame: a grid of cells that turns into ANSI text.

Each cell holds a character, a foreground, a background and a bold flag. When the frame is
turned into text, an emitter tracks the terminal's current attributes and writes only the ones
that change from one cell to the next, so a run of same-coloured cells costs one escape code.
"""
import re
from dataclasses import dataclass

from .themes import C

# C0 and C1 control characters, DEL included. Text from the network or the config file goes through
# put(), so an ESC in a region name or an error message can never reach the terminal as a sequence.
UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def clean(text):
    return UNSAFE.sub("?", str(text))


@dataclass
class Cell:
    char: str = " "
    fg: tuple = None
    bg: tuple = None
    bold: bool = False


class Canvas:
    def __init__(self, w, h):
        self.w, self.h = w, h
        self.rows = [[Cell(bg=C.BG) for _ in range(w)] for _ in range(h)]

    def cell(self, x, y):
        """The cell at (x, y), or None off the edge."""
        if 0 <= y < self.h and 0 <= x < self.w:
            return self.rows[y][x]
        return None

    def bg_at(self, x, y):
        c = self.cell(x, y)
        return c.bg if c else C.BG

    def put(self, x, y, text, fg=None, bg=None, bold=False):
        """Write ``text`` from (x, y) rightwards. ``bg=None`` keeps each cell's own background."""
        for i, ch in enumerate(clean(text)):
            c = self.cell(x + i, y)
            if c is None:
                continue
            c.char, c.fg, c.bold = ch, fg, bold
            if bg is not None:
                c.bg = bg

    def fill(self, x, y, w, bg):
        """Blank ``w`` cells and give them background ``bg``."""
        for i in range(w):
            c = self.cell(x + i, y)
            if c is not None:
                c.char, c.fg, c.bg, c.bold = " ", None, bg, False

    def tint(self, x, y, bg):
        """Change only the background of one cell."""
        c = self.cell(x, y)
        if c is not None:
            c.bg = bg

    def text(self):
        """The characters alone, one string per row (for tests)."""
        return ["".join(c.char for c in row) for row in self.rows]

    def lines(self):
        """One ANSI string per row. Every row starts and ends with attributes reset."""
        return [_Emitter().row(row) for row in self.rows]


class _Emitter:
    """Writes cells while remembering what the terminal is already set to."""

    def __init__(self):
        self.fg = self.bg = None        # None is the terminal default, which is what reset leaves
        self.bold = False
        self.out = ["\x1b[0m"]

    def row(self, cells):
        for c in cells:
            self.style(c)
            self.out.append(c.char)
        self.out.append("\x1b[0m")
        return "".join(self.out)

    def style(self, c):
        codes = []
        if c.bold != self.bold:
            # there is no portable "bold off" that leaves colours alone, so a change resets
            if self.bold:
                codes.append("0")
                self.fg = self.bg = None
            else:
                codes.append("1")
            self.bold = c.bold
        if c.fg != self.fg:
            codes.append("39" if c.fg is None else "38;2;%d;%d;%d" % c.fg)
            self.fg = c.fg
        if c.bg != self.bg:
            codes.append("49" if c.bg is None else "48;2;%d;%d;%d" % c.bg)
            self.bg = c.bg
        if codes:
            self.out.append("\x1b[" + ";".join(codes) + "m")
