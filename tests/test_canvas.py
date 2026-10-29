import re

from gridhour_au.app import KeyDecoder
from gridhour_au.canvas import Canvas


def visible(line):
    return re.sub(r"\x1b\[[0-9;]*m", "", line)


def test_lines_keep_the_text():
    cv = Canvas(8, 2)
    cv.put(1, 0, "grid", (255, 0, 0), bold=True)
    cv.put(0, 1, "hour", (0, 255, 0), (0, 0, 40))
    assert [visible(ln) for ln in cv.lines()] == cv.text() == [" grid   ", "hour    "]


def test_a_run_of_one_colour_costs_one_escape():
    cv = Canvas(20, 1)
    cv.put(0, 0, "x" * 20, (1, 2, 3))
    line = cv.lines()[0]
    assert line.count("38;2;1;2;3") == 1
    assert line.startswith("\x1b[0m") and line.endswith("\x1b[0m")


def test_bold_off_resets_and_restores_colour():
    cv = Canvas(2, 1)
    cv.put(0, 0, "A", (9, 9, 9), (1, 1, 1), bold=True)
    cv.put(1, 0, "b", (9, 9, 9), (1, 1, 1))
    line = cv.lines()[0]
    after = line.split("A", 1)[1]
    assert after.startswith("\x1b[0;38;2;9;9;9;48;2;1;1;1m")


def test_put_keeps_background_when_none():
    cv = Canvas(3, 1)
    cv.fill(0, 0, 3, (5, 5, 5))
    cv.put(0, 0, "ab", (200, 200, 200))
    assert cv.bg_at(0, 0) == (5, 5, 5) and cv.cell(1, 0).char == "b"
    assert cv.cell(9, 0) is None


def test_decoder_plain_and_sequences():
    d = KeyDecoder()
    assert d.feed("ab\x1b[C\x1b[1;2D\x1bOHq") == ["a", "b", "\x1b[C", "\x1b[1;2D", "\x1bOH", "q"]


def test_decoder_holds_a_split_sequence():
    d = KeyDecoder()
    assert d.feed("x\x1b[1;") == ["x"]
    assert d.feed("2C") == ["\x1b[1;2C"]


def test_decoder_lone_escape():
    d = KeyDecoder()
    assert d.feed("\x1b") == []
    assert d.flush() == ["\x1b"]
    assert d.feed("\x1bq") == ["\x1b", "q"]
