"""Short job windows on narrow screens, where there are fewer columns than half hours."""
from datetime import UTC, datetime

import pytest

from gridhour_au import plan
from gridhour_au.compose import Axis, compose
from gridhour_au.grid import SLOT, WINDOW_SLOTS, Forecast, Slot, window_start
from gridhour_au.state import State

NOW = datetime(2026, 11, 3, 10, 10, tzinfo=UTC)


def state_with_best_at(slot, minutes):
    start = window_start(NOW)
    n = minutes // 30
    st = State()
    st.persist = False
    st.fc = Forecast(slots=[Slot(start + SLOT * i, 20 if slot <= i < slot + n else 300, "moderate")
                            for i in range(WINDOW_SLOTS)])
    st.jobs = [plan.Job("Dryer", minutes)]
    st.mode = "green"
    return st


@pytest.mark.parametrize("minutes", [30, 60, 120])
@pytest.mark.parametrize("compact", ["on", "off"])
def test_short_windows_never_crash_on_narrow_screens(minutes, compact):
    for slot in range(4, 90, 3):
        for width in range(40, 130, 7):
            st = state_with_best_at(slot, minutes)
            st.compact = compact
            text = "\n".join(compose(width, 40, st, NOW, NOW).text())
            assert "Dryer" in text


def test_span_always_has_a_column():
    for width in (10, 37, 49, 95, 96, 150):
        axis = Axis(0, width, NOW)
        for a in range(WINDOW_SLOTS):
            cols = axis.span(a, a)
            assert cols and all(0 <= c < width for c in cols)


def test_wide_screens_keep_the_exact_span():
    axis = Axis(0, 192, NOW)                      # two columns per half hour
    assert axis.span(10, 11) == [20, 21, 22, 23]
