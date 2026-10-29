import json
from datetime import timedelta, timezone
from pathlib import Path

from conftest import NOW, fake_fetch

from gridhour_au import grid, output
from gridhour_au.plan import Job


def test_line(st):
    assert output.line_output(st, NOW) == "⚡ 247g · 5c · green now"


def test_line_green_later(st):
    assert output.line_output(st, NOW + timedelta(hours=1)).endswith("green in 14h30")


def test_line_without_prices(st):
    for s in st.fc.slots:
        s.price = None
    assert output.line_output(st, NOW) == "⚡ 247g · green now"


def test_line_best(st):
    assert output.line_output(st, NOW, best=Job("job", 120)) == "⚡ 247g · 5c · 2h best 12:00 (in 1d19h)"


def test_line_colours(st):
    tmux = output.line_output(st, NOW, "tmux")
    assert tmux.count("#[fg=#") == 3 and "#[default]" in tmux
    assert "\x1b[38;2;" in output.line_output(st, NOW, "ansi")


def test_line_without_data(st):
    st.fc.slots = []
    st.fc.errors = ["network: down"]
    assert output.line_output(st, NOW) == "⚡ network: down"


def test_json(st):
    doc = json.loads(output.json_output(st, NOW))
    assert doc["region"] == {"id": "NSW1", "name": "New South Wales", "postcode": "2000"}
    assert doc["units"]["price"].startswith("c/kWh")
    assert doc["now"]["carbon"] == 247 and doc["now"]["price"] == 5.315 and doc["now"]["mix"]["rooftop"] == 33.7
    assert doc["green_now"] is True and doc["next_green"]["from"] == "2026-10-09T05:30Z"
    washing = doc["jobs"][0]
    assert washing["name"] == "Washing" and washing["best"]["from"] == "2026-10-11T01:00Z"
    assert washing["best"]["starts_in_minutes"] == 2590
    assert len(doc["slots"]) == 96


def test_ical_time():
    assert output._ical_time(NOW) == "20261009T055000Z"
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    assert output._ical_time(local) == "20261009T055000Z"


def test_ical_text():
    text = "Wash\\dry; cheap, green\r\nnext\rlast\nend"
    assert output._ical_text(text) == (
        "Wash\\\\dry\\; cheap\\, green\\nnext\\nlast\\nend"
    )


def test_ical_uid():
    start = NOW.replace(hour=13, minute=30)
    uid = output._ical_uid("Washing", start, 1)

    # Both timestamps fall on October 10 in Sydney (00:30 and 02:30 AEDT), though not in UTC.
    assert uid == output._ical_uid("Washing", start + timedelta(hours=2), 1)
    assert uid != output._ical_uid("Washing", start + timedelta(days=1), 1)
    assert uid != output._ical_uid("Washing", start, 2)
    assert uid != output._ical_uid("Dryer", start, 1)
    assert len(uid.split("@")[0]) == 40
    assert uid.endswith("@gridhour-au")


def test_ical_fold():
    assert output._ical_fold("a" * 75) == "a" * 75
    assert output._ical_fold("a" * 76) == "a" * 75 + "\r\n a"

    text = "SUMMARY:" + "é" * 100
    folded = output._ical_fold(text)
    lines = folded.split("\r\n")

    assert all(len(line.encode("utf-8")) <= 75 for line in lines)
    assert all(line.startswith(" ") for line in lines[1:])
    assert folded.replace("\r\n ", "") == text


def test_ical_document():
    lines = ["BEGIN:VCALENDAR", "END:VCALENDAR"]
    assert output._ical_document(lines) == (
        "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
    )


def test_ical_empty(st):
    st.jobs = []
    assert output.ical_output(st, NOW) == (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "PRODID:-//gridhour-au//Job windows//EN\r\n"
        "END:VCALENDAR\r\n"
    )


def test_ical_job(st):
    st.jobs = [Job("Washing", 120)]
    doc = output.ical_output(st, NOW)

    assert doc.count("BEGIN:VEVENT\r\n") == 1
    assert doc.count("END:VEVENT\r\n") == 1
    assert "SUMMARY:Washing (gridhour-au)\r\n" in doc
    assert "DTSTAMP:20261009T055000Z\r\n" in doc
    assert "DTSTART:20261011T010000Z\r\n" in doc
    assert "DTEND:20261011T030000Z\r\n" in doc
    assert "UID:" in doc
    assert doc.endswith("END:VCALENDAR\r\n")


def test_ical_description(st):
    st.jobs = [Job("Washing", 120)]
    best = json.loads(output.json_output(st, NOW))["jobs"][0]["best"]
    description = (
        "Best window average: carbon %.0f gCO2e/kWh (estimated); price %.2f c/kWh (wholesale spot)"
        % (best["carbon"], best["price"])
    )

    doc = output.ical_output(st, NOW).replace("\r\n ", "")
    assert "DESCRIPTION:" + output._ical_text(description) + "\r\n" in doc


def test_ical_split_job(st):
    st.mode = "cheap"
    st.jobs = [Job("EV charge", 90, split=True)]
    st.fc.slots = [s for s in st.fc.slots if s.start >= NOW][:6]

    for slot, price in zip(st.fc.slots, [1, 100, 2, 100, 3, 100], strict=True):
        slot.price = price

    doc = output.ical_output(st, NOW).replace("\r\n ", "")
    assert doc.count("BEGIN:VEVENT\r\n") == 3

    for number, index in enumerate([0, 2, 4], start=1):
        slot = st.fc.slots[index]
        summary = "EV charge (gridhour-au) (part %d of 3)" % number
        assert "SUMMARY:" + summary + "\r\n" in doc
        assert "DTSTART:" + output._ical_time(slot.start) + "\r\n" in doc
        assert "DTEND:" + output._ical_time(slot.end) + "\r\n" in doc


def test_ical_no_window(st):
    st.jobs = [Job("Washing", 120)]
    st.fc.slots = []

    doc = output.ical_output(st, NOW)

    assert "BEGIN:VEVENT" not in doc
    assert doc.startswith("BEGIN:VCALENDAR\r\n")
    assert doc.endswith("END:VCALENDAR\r\n")


def test_ical_without_prices(st):
    st.jobs = [Job("Washing", 120)]
    for slot in st.fc.slots:
        slot.price = None

    doc = output.ical_output(st, NOW).replace("\r\n ", "")

    assert doc.count("BEGIN:VEVENT\r\n") == 1
    assert "price unknown\r\n" in doc
    assert "SUMMARY:Washing (gridhour-au)\r\n" in doc


def test_ical_safe_job_name(st):
    st.jobs = [Job("Wash\\dry; cheap, green\r\nBEGIN:VEVENT", 120)]

    doc = output.ical_output(st, NOW).replace("\r\n ", "")

    assert doc.count("BEGIN:VEVENT\r\n") == 1
    assert (
        "SUMMARY:Wash\\\\dry\\; cheap\\, green??BEGIN:VEVENT (gridhour-au)\r\n"
    ) in doc


def test_ical_repeated_export(st):
    st.jobs = [Job("Washing", 120)]

    first = output.ical_output(st, NOW)
    second = output.ical_output(st, NOW + timedelta(minutes=1))

    first_uids = [line for line in first.split("\r\n") if line.startswith("UID:")]
    second_uids = [line for line in second.split("\r\n") if line.startswith("UID:")]

    assert len(first_uids) == 1
    assert first_uids == second_uids
    assert "DTSTAMP:20261009T055000Z\r\n" in first
    assert "DTSTAMP:20261009T055100Z\r\n" in second


def test_watch_shows_times_in_the_regions_zone(st, monkeypatch, capsys):
    monkeypatch.setattr(output, "load", lambda now, pc, rid, prices: grid.load(now, pc, rid, prices, fetch=fake_fetch))

    def stop(secs):
        raise KeyboardInterrupt

    monkeypatch.setattr(output.time, "sleep", stop)
    st.postcode, st.region_id = None, "QLD1"
    output.watch(st, lambda: NOW, "plain")
    assert capsys.readouterr().out.startswith("\r⚡ ") and grid.local(NOW).tzname() == "AEST"


def test_json_schema_version_on_forecast_and_empty_output(st):
    assert json.loads(output.json_output(st, NOW))["schema"] == 1
    st.fc.slots = []
    st.fc.errors = ["offline and nothing cached yet"]
    empty = json.loads(output.json_output(st, NOW, Job("job", 120)))
    assert empty["schema"] == 1
    assert empty["slots"] == [] and empty["best"] is None


def test_json_documented_shape(st):
    docs = Path(__file__).resolve().parents[1] / "docs" / "json.md"
    example = json.loads(docs.read_text(encoding="utf-8").split("```json\n", 1)[1].split("```", 1)[0])
    actual = json.loads(output.json_output(st, NOW, Job("job", 120)))

    def check_keys(documented, emitted):
        if isinstance(documented, dict):
            assert documented.keys() <= emitted.keys()
            for key, value in documented.items():
                if key != "mix":            # fuel names are data, not schema keys
                    check_keys(value, emitted[key])
        elif isinstance(documented, list) and documented:
            assert emitted
            for value in emitted:
                check_keys(documented[0], value)

    check_keys(example, actual)
    assert example["schema"] == actual["schema"] == 1
