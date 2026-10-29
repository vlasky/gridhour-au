import pytest
from conftest import NOW

from gridhour_au.compose import compose
from gridhour_au.themes import THEME_ORDER, apply_theme


def text(st, W=120, H=40, now=NOW):
    return "\n".join(compose(W, H, st, now, NOW).text())


def test_full_layout(st):
    t = text(st)
    lines = t.splitlines()
    assert "ϟ gridhour-au" in lines[0] and "New South Wales  2000" in lines[0]
    assert "16:50 AEDT" in lines[0] and "→" not in lines[0]      # live: no scrub offset, Sydney time
    assert "gCO₂e/kWh est." in t and "spot c/kWh" in t
    assert "Sat" in t and "Sun" in t                # day names on the time axis
    assert "GENERATION MIX" in t and "Coal" in t and "Rooftop PV" in t and "Grid solar" in t
    assert "BEST TIME TO RUN · GREEN + CHEAP" in t
    assert "12:00–14:00 · 152g · -0.7c" in t         # the washing window label in its strip
    assert "start Sun 12:00, in 1d19h" in t and "Washing 2h" in t.splitlines()[-2]
    assert "spot negative" not in t                 # the price now is positive


def test_every_line_is_exactly_the_width(st):
    for W, H in ((120, 40), (100, 30), (72, 16), (44, 10)):
        lines = compose(W, H, st, NOW, NOW).text()
        assert len(lines) == H and all(len(ln) == W for ln in lines)


def test_compact_layout(st):
    t = text(st, 72, 16)
    assert "Carbon" in t and "Price" in t and "gCO₂e/kWh" not in t
    assert "Washing" in t


def test_title_drops_pieces_instead_of_cutting_words(st):
    first = text(st, 60, 16).splitlines()[0]
    assert "New South Wales" in first and "green +" not in first


def test_scrubbed_badge_and_info(st):
    st.frozen = NOW.replace(day=10, hour=0, minute=0)
    t = text(st, now=st.frozen)
    assert "+18h30 → 11:00 AEDT" in t.splitlines()[0]
    assert "Sat 11:00–11:30 AEDT" in t and "spot negative" in t


def test_mix_toggle(st):
    st.mix = False
    assert "GENERATION MIX" not in text(st)


def test_no_prices_says_so(st):
    st.mode = "cheap"
    for s in st.fc.slots:
        s.price = None
    t = text(st)
    assert "no prices, so carbon only" in t and "c/kWh" not in t


def test_loading_screen(st):
    st.fc.slots = []
    st.loading = True
    assert "fetching the forecast" in text(st)


def test_error_screen(st):
    st.fc.slots = []
    st.fc.errors = ["network: down"]
    assert "network: down" in text(st)


@pytest.mark.parametrize("theme", THEME_ORDER)
def test_every_theme_renders(st, theme):
    apply_theme(theme)
    assert compose(120, 40, st, NOW, NOW).lines()


def test_help_overlay(st):
    st.prompt = "help"
    assert "move the cursor 30 minutes" in text(st)


def test_twelve_hour_clock(st):
    st.h12 = True
    t = text(st)
    assert "4:50pm AEDT" in t and "12:00pm–2:00pm" in t


def test_narrow_axis_with_a_short_window(st):
    """A one hour window can fall between two columns of a narrow axis; that must not crash."""
    for W in range(40, 80):
        assert len(compose(W, 16, st, NOW, NOW).text()) == 16


@pytest.mark.parametrize("region,zone", [("NSW1", "Australia/Sydney"), ("SA1", "Australia/Adelaide")])
def test_hour_labels_sit_on_local_hours(st, region, zone):
    """Adelaide is UTC+10:30, so its hours fall on UTC half hours."""
    from zoneinfo import ZoneInfo

    from gridhour_au import compose as cm
    from gridhour_au import grid
    grid.set_zone(region)
    W = 120
    axis = cm.Axis(cm.GUTTER, W - cm.GUTTER - 1, st.fc.slots[0].start)
    row = next(ln for ln in compose(W, 40, st, NOW, NOW).text() if "Sat" in ln and "─" in ln)
    col = row.index("Sat") - cm.GUTTER
    t0, t1 = axis.start + cm.WINDOW * (col / axis.w), axis.start + cm.WINDOW * ((col + 1) / axis.w)
    midnight = t1.astimezone(ZoneInfo(zone)).replace(hour=0, minute=0, second=0, microsecond=0)
    assert t0 <= midnight < t1


@pytest.mark.parametrize("region", ["NSW1", "QLD1", "SA1"])
@pytest.mark.parametrize("small", [True, False])                       # compact layout, or the chart
def test_narrow_axes_name_every_midnight(region, small):
    """A terminal of 40 to 70 columns has an axis of 17 to 47. Below 48 one column spans more than
    an hour; midnight shared it with 23:00 and was dropped, which could leave the axis with no
    labels at all. On the chart, under 32 columns, 6-hourly labels crowded the day names out."""
    from datetime import UTC, datetime, timedelta

    from gridhour_au import compose as cm
    from gridhour_au import grid
    from gridhour_au.canvas import Canvas

    grid.set_zone(region)
    for width in range(17, 72):
        for k in range(48):
            start = datetime(2026, 10, 9, tzinfo=UTC) + timedelta(minutes=30 * k)
            axis = cm.Axis(0, width, start)
            cv = Canvas(width, 1)
            cm.hour_ticks(cv, 0, axis, False, small=small)
            row = cv.text()[0]
            midnights = {grid.local(start + timedelta(hours=h)).replace(hour=0, minute=0) for h in range(49)}
            due = [m for m in midnights if (c := axis.col(m.astimezone(UTC))) is not None and c + 3 <= width]
            assert sum(row.count(m.strftime("%a")) for m in due) == len(due), (width, k, row)
