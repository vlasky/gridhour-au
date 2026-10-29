from datetime import UTC, datetime, timedelta

import pytest
from conftest import NOW

from gridhour_au import plan
from gridhour_au.grid import SLOT, Slot

T0 = datetime(2026, 1, 1, tzinfo=UTC)


def mk(carbons, prices=None, index=None):
    prices = prices or [None] * len(carbons)
    return [Slot(T0 + SLOT * i, c, (index or {}).get(i, "moderate"), None, p)
            for i, (c, p) in enumerate(zip(carbons, prices, strict=True))]


@pytest.mark.parametrize("text,mins", [("2h", 120), ("1h30", 90), ("90m", 90), ("1.5h", 90), ("45m", 60),
                                       ("3", 180), ("45", 60), ("20min", 30)])
def test_parse_duration(text, mins):
    assert plan.parse_duration(text) == mins


@pytest.mark.parametrize("text", ["", "h", "abc", "0m", "25h"])
def test_parse_duration_rejects(text):
    with pytest.raises(ValueError):
        plan.parse_duration(text)


def test_parse_job():
    assert plan.parse_job("Tumble dryer 1h30") == plan.Job("Tumble dryer", 90)
    with pytest.raises(ValueError):
        plan.parse_job("Dryer")


def test_best_window_green():
    slots = mk([300, 200, 50, 60, 250, 40, 300])
    w = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 60), T0)
    assert (w.first, w.last) == (2, 3) and w.carbon == 55


def test_best_window_starts_no_earlier_than_now():
    slots = mk([10, 10, 300, 200, 100, 300])
    w = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 30), T0 + SLOT * 2 + timedelta(minutes=7))
    assert w.first == 4


def test_ties_go_to_the_earliest():
    slots = mk([100, 50, 50, 50, 50])
    w = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 60), T0)
    assert w.first == 1


def test_cheap_and_both_differ():
    slots = mk([50, 50, 300, 300], [30, 30, 5, 5])
    g = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 60), T0)
    c = plan.best_window(slots, plan.scores(slots, "cheap"), plan.Job("x", 60), T0)
    assert g.first == 0 and c.first == 2


def test_price_modes_fall_back_to_green_without_prices():
    slots = mk([300, 50, 300])
    assert plan.effective_mode(slots, "cheap") == "green"
    w = plan.best_window(slots, plan.scores(slots, "both"), plan.Job("x", 30), T0)
    assert w.first == 1


def test_window_needs_every_slot_known():
    slots = mk([100, 100, 10, 10], [5, 5, None, None])
    w = plan.best_window(slots, plan.scores(slots, "both"), plan.Job("x", 60), T0)
    assert w.first == 0


def test_no_window_when_job_is_longer_than_the_data():
    slots = mk([100, 100])
    assert plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 180), T0) is None


def test_next_green_and_green_until():
    slots = mk([300, 300, 60, 50, 300], index={2: "low", 3: "very low"})
    hit, is_now = plan.next_green(slots, T0)
    assert hit.start == T0 + SLOT * 2 and not is_now
    assert plan.green_until(slots, T0) is None
    assert plan.green_until(slots, T0 + SLOT * 2) == T0 + SLOT * 4
    assert plan.next_green(slots, T0 + SLOT * 4) is None


def test_fixture_windows(fc):
    sc = plan.scores(fc.slots, "green")
    w = plan.best_window(fc.slots, sc, plan.Job("Washing", 120), NOW)
    assert w.start == datetime(2026, 10, 11, 1, 0, tzinfo=UTC)          # 12:00 Sunday in Sydney, rooftop solar
    sc = plan.scores(fc.slots, "cheap")
    w = plan.best_window(fc.slots, sc, plan.Job("Washing", 120), NOW)
    assert w.start == datetime(2026, 10, 10, 23, 0, tzinfo=UTC) and w.price < 0


@pytest.mark.parametrize("mins,out", [(0, "now"), (25, "25m"), (60, "1h"), (150, "2h30"), (135, "2h"),
                                      (60 * 26, "1d2h")])
def test_fmt_in(mins, out):
    assert plan.fmt_in(timedelta(minutes=mins)) == out


def test_fmt_duration():
    assert [plan.fmt_duration(m) for m in (30, 60, 90, 240)] == ["30m", "1h", "1h30", "4h"]
