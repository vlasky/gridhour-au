"""Daylight saving: the hour that repeats when clocks go back, and the marks on the time axis."""
from datetime import UTC, datetime, timedelta

import pytest

from gridhour_au import grid
from gridhour_au.canvas import Canvas
from gridhour_au.compose import Axis, compose, day_hm, hour_ticks, job_hm
from gridhour_au.grid import SLOT, Slot
from gridhour_au.output import ical_output, json_output, line_output
from gridhour_au.plan import Job

BACK = datetime(2027, 4, 3, 16, tzinfo=UTC)         # Sydney: 03:00 AEDT becomes 02:00 AEST, Sun 4 April
FORWARD = datetime(2026, 10, 3, 16, tzinfo=UTC)     # Sydney: 02:00 AEST becomes 03:00 AEDT, Sun 4 October


@pytest.mark.parametrize("hour,zone", [(15, "AEDT"), (16, "AEST")])
@pytest.mark.parametrize("h12,clock", [(False, "02:30"), (True, "2:30am")])
def test_both_repeated_times_are_disambiguated(hour, zone, h12, clock):
    t = datetime(2027, 4, 3, hour, 30, tzinfo=UTC)
    assert job_hm(t, h12) == clock + " " + zone
    assert day_hm(t, t, h12) == clock + " " + zone


def test_ordinary_and_spring_times_remain_short():
    for t, expected in [(datetime(2027, 4, 3, 17, 30, tzinfo=UTC), "03:30"), (FORWARD + SLOT, "03:30")]:
        assert job_hm(t) == expected


def test_adelaide_repeats_its_own_hour():
    grid.set_zone("SA1")
    assert job_hm(datetime(2027, 4, 3, 16, tzinfo=UTC)) == "02:30 ACDT"
    assert job_hm(datetime(2027, 4, 3, 17, tzinfo=UTC)) == "02:30 ACST"


def test_brisbane_has_no_repeated_hour():
    grid.set_zone("QLD1")
    assert job_hm(datetime(2027, 4, 3, 16, 30, tzinfo=UTC)) == "02:30"


@pytest.mark.parametrize("region,transition,zone", [
    ("NSW1", BACK, "AEST"), ("NSW1", FORWARD, "AEDT"), ("VIC1", BACK, "AEST"), ("TAS1", FORWARD, "AEDT"),
    ("SA1", BACK + SLOT, "ACST"), ("SA1", FORWARD + SLOT, "ACDT"),
])
@pytest.mark.parametrize("h12", [False, True])
def test_the_axis_marks_a_clock_change_near_it_and_apart(region, transition, zone, h12):
    """The change falls on an hour label (02:00 or 03:00), so the mark takes that label's place."""
    grid.set_zone(region)
    for width in range(40, 201):
        cv = Canvas(width, 1)
        axis = Axis(0, width, transition - timedelta(hours=12))
        hour_ticks(cv, 0, axis, h12, small=width < 60)
        row = cv.text()[0]
        assert zone in row, width
        start = row.index(zone)
        assert 0 <= start - axis.col(transition) <= max(2, width // 48)
        assert row[start - 1] == "─" and row[start + len(zone)] == "─"


@pytest.mark.parametrize("width", [44, 63, 72, 120])
def test_a_clock_change_keeps_the_day_name(width):
    cv = Canvas(width, 1)
    hour_ticks(cv, 0, Axis(0, width, BACK - timedelta(hours=12)), False, small=width < 60)
    row = cv.text()[0]
    assert "Sun" in row and "AEST" in row and row.index("Sun") + 4 <= row.index("AEST")


def test_brisbane_has_no_mark():
    grid.set_zone("QLD1")
    cv = Canvas(120, 1)
    hour_ticks(cv, 0, Axis(0, 120, BACK - timedelta(hours=12)), False)
    assert "AE" not in cv.text()[0]


@pytest.mark.parametrize("hour,zone", [(15, "AEDT"), (16, "AEST")])
def test_repeated_hour_job_in_frame_and_line(st, hour, zone):
    now = datetime(2027, 4, 3, 11, tzinfo=UTC)          # 22:00 AEDT on Saturday
    start = datetime(2027, 4, 3, hour, 30, tzinfo=UTC)
    st.fc.slots = [Slot(now + SLOT * k, carbon=10 if now + SLOT * k == start else 300, index="moderate", price=10)
                   for k in range(96)]
    st.mode = "green"
    st.jobs = [Job("Dryer", 30)]
    frame = "\n".join(compose(180, 40, st, now, now).text())
    assert "02:30 " + zone in frame
    assert "start Sun 02:30 " + zone in frame.splitlines()[-2]
    assert "best 02:30 " + zone in line_output(st, now, best=st.jobs[0])
    assert start.strftime("%Y-%m-%dT%H:%MZ") in json_output(st, now)
    assert start.strftime("%Y%m%dT%H%M%SZ") in ical_output(st, now)
