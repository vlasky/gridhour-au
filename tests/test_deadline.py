"""Jobs with a deadline ("by 07:00") and jobs that may run in pieces ("split")."""
import json
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import NOW

from gridhour_au import grid, output, plan, state
from gridhour_au.compose import compose
from gridhour_au.grid import SLOT, Slot
from gridhour_au.keys import handle_key

T0 = datetime(2026, 1, 10, tzinfo=ZoneInfo("Australia/Sydney")).astimezone(UTC)    # midnight in Sydney, AEDT


def mk(carbons):
    return [Slot(T0 + SLOT * i, c, "moderate", None, None) for i, c in enumerate(carbons)]


@pytest.mark.parametrize("text,out", [("07:00", "07:00"), ("7:00", "07:00"), ("7am", "07:00"), ("7.30pm", "19:30"),
                                      ("12am", "00:00"), ("12pm", "12:00"), ("23:30", "23:30")])
def test_parse_clock(text, out):
    assert plan.parse_clock(text) == out


@pytest.mark.parametrize("text", ["7", "25:00", "07:15", "13pm", "noon", "", "٧am"])
def test_parse_clock_rejects(text):
    with pytest.raises(ValueError):
        plan.parse_clock(text)


@pytest.mark.parametrize("text,job", [
    ("Dryer 1h30", plan.Job("Dryer", 90)),
    ("EV charge 4h by 07:00 split", plan.Job("EV charge", 240, "07:00", True)),
    ("EV 4h by 7am", plan.Job("EV", 240, "07:00", False)),
    ("Battery 3h split", plan.Job("Battery", 180, None, True)),
    ("Hot water by night 2h by 6am", plan.Job("Hot water by night", 120, "06:00", False)),
])
def test_parse_job_grammar(text, job):
    assert plan.parse_job(text) == job


@pytest.mark.parametrize("text", ["EV by 07:00", "4h by 07:00", "EV 4h by", "EV 4h by soon split"])
def test_parse_job_grammar_rejects(text):
    with pytest.raises(ValueError):
        plan.parse_job(text)


def test_deadline_is_the_next_one():
    job = plan.Job("EV", 240, "07:00")
    assert plan.deadline_at(job, T0 + timedelta(hours=2)) == T0 + timedelta(hours=7)
    assert plan.deadline_at(job, T0 + timedelta(hours=8)) == T0 + timedelta(days=1, hours=7)
    assert plan.deadline_at(plan.Job("x", 60), T0) is None


def test_deadline_follows_daylight_saving():
    job = plan.Job("EV", 60, "07:00")
    winter, summer = datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 1, 10, tzinfo=UTC)
    assert plan.deadline_at(job, winter) == winter + timedelta(hours=21)       # 07:00 AEST is 21:00 UTC
    assert plan.deadline_at(job, summer) == summer + timedelta(hours=20)       # 07:00 AEDT is 20:00 UTC


def test_queensland_has_no_daylight_saving():
    grid.set_zone("QLD1")
    job = plan.Job("EV", 60, "07:00")
    for t in (datetime(2026, 7, 1, tzinfo=UTC), datetime(2026, 1, 10, tzinfo=UTC)):
        assert plan.deadline_at(job, t) == t + timedelta(hours=21)


def test_window_must_finish_by_the_deadline():
    # cheapest run is at 08:00-09:00, after the 07:00 deadline
    slots = mk([300] * 4 + [100, 100] + [300] * 10 + [10, 10] + [300] * 6)
    free = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 60), T0)
    due = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 60, "07:00"), T0)
    assert free.first == 16
    assert due.first == 4 and due.end <= T0 + timedelta(hours=7)


def test_split_picks_the_best_half_hours():
    slots = mk([10, 300, 10, 300, 10, 300, 10, 300])
    sc = plan.scores(slots, "green")
    one_go = plan.best_window(slots, sc, plan.Job("x", 120), T0)
    pieces = plan.best_window(slots, sc, plan.Job("x", 120, None, True), T0)
    assert pieces.indices == (0, 2, 4, 6) and pieces.carbon == 10
    assert pieces.parts() == [(0, 0), (2, 2), (4, 4), (6, 6)]
    assert one_go.carbon > pieces.carbon


def test_split_keeps_one_block_when_pieces_are_no_better():
    slots = mk([300, 50, 50, 50, 50, 300, 50])
    w = plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 120, None, True), T0)
    assert w.parts() == [(1, 4)]


def test_not_enough_time_before_the_deadline():
    slots = mk([100] * 20)
    job = plan.Job("x", 240, "02:00")               # needs 4 hours, only 2 before 02:00
    assert plan.best_window(slots, plan.scores(slots, "green"), job, T0) is None
    assert plan.best_window(slots, plan.scores(slots, "green"), plan.Job("x", 240, "02:00", True), T0) is None


def test_keys_toggle_split_and_set_deadline(st):
    st.sel = 0
    handle_key(st, "s", NOW)
    assert st.job.split
    for k in ["b", *"7am", "\r"]:
        handle_key(st, k, NOW)
    assert st.job.deadline == "07:00" and st.prompt is None
    for k in ["b", *"\x7f" * 10, "\r"]:
        handle_key(st, k, NOW)
    assert st.job.deadline is None
    for k in ["b", *"soon", "\r"]:
        handle_key(st, k, NOW)
    assert st.prompt == "deadline" and st.err


def test_add_job_with_deadline_and_split(st):
    for k in ["a", *"Battery 3h by 16:00 split", "\r"]:
        handle_key(st, k, NOW)
    assert st.jobs[-1] == plan.Job("Battery", 180, "16:00", True)


def test_config_round_trip(st):
    st.persist = True
    st.jobs = [plan.Job("EV", 240, "07:00", True), plan.Job("Wash", 120)]
    st.save()
    again = state.state_from_config()
    assert again.jobs == st.jobs


@pytest.mark.parametrize("raw", [{"deadline": "nope"}, {"deadline": 7}, {"deadline": "\x1b[2J"}, {"split": "yes"}])
def test_hostile_job_fields(raw):
    path = state.config_path()
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"jobs": [dict({"name": "EV", "minutes": 240}, **raw)]}, f)
    jobs = state.state_from_config().jobs
    assert all(j.deadline in (None, "07:00") and j.split in (True, False) for j in jobs)
    assert not any(j.split for j in jobs if j.name == "EV")


def test_json_has_deadline_split_and_parts(st):
    st.jobs = [plan.Job("EV", 240, "07:00", True)]
    job = json.loads(output.json_output(st, NOW))["jobs"][0]
    assert job["deadline"] == "07:00" and job["split"] is True
    assert job["deadline_at"] == "2026-10-09T20:00Z"            # 07:00 AEDT
    assert job["best"]["to"] <= job["deadline_at"]
    assert job["best"]["parts"] and all(p["from"] < p["to"] for p in job["best"]["parts"])


def test_line_best_with_deadline(st):
    out = output.line_output(st, NOW, best=plan.parse_job("job 4h by 07:00 split"))
    assert "4h by 07:00, split best" in out


def test_screen_shows_deadline_and_explains_missing_window(st):
    st.jobs = [plan.Job("EV", 240, "09:00", True), plan.Job("Late", 600, "00:30")]
    text = "\n".join(compose(120, 40, st, NOW, NOW).text())
    assert "┤" in text
    assert "not enough forecast before 00:30 for 10h by 00:30" in text


def test_deadline_prompt_accepts_by_and_explains_a_whole_job(st):
    st.sel = 0
    for k in ["b", *"\x7f" * 10, *"by 6:30am", "\r"]:
        handle_key(st, k, NOW)
    assert st.job.deadline == "06:30" and st.prompt is None
    for k in ["b", *"\x7f" * 10, *"EV charge 4h by 07:00 split", "\r"]:
        handle_key(st, k, NOW)
    assert st.prompt == "deadline" and "new job" in st.err and "press a" not in st.err
    assert st.job.deadline == "06:30"                   # unchanged


@pytest.mark.parametrize("text,hint", [("EV by 07:00 split", "'EV 4h by 07:00 split'"),
                                       ("Car charge by 7am", "'Car charge 4h by 07:00'"),
                                       ("Battery split", "'Battery 4h split'")])
def test_missing_duration_says_so(text, hint):
    with pytest.raises(ValueError, match="how long does it run") as e:
        plan.parse_job(text)
    assert hint in str(e.value)
