import io
import json
import zipfile
from datetime import UTC, datetime, timedelta

import pytest
from conftest import FX, NOW, fake_fetch, fixture_bytes, listing

from gridhour_au import grid

PREDISPATCH = "PUBLIC_PREDISPATCHIS_202610091600_20261009153245.zip"
STPASA = "PUBLIC_STPASA_202610091500_0000000541989278.zip"
SCADA = "PUBLIC_DISPATCHSCADA_202610091550_0000000541993115.zip"
ROOFTOP = "PUBLIC_ROOFTOP_PV_FORECAST_20261009153000_0000000541991132.zip"
GENERATORS = "CO2EII_AVAILABLE_GENERATORS.CSV"


def report(kind, name):
    r = grid.REPORTS[kind]
    return r.parse(r.extract(grid.csv_lines(fixture_bytes(name)), None))


@pytest.mark.parametrize("text,out,region", [
    ("2000", "2000", "NSW1"), (" 2600 ", "2600", "NSW1"), ("0200", "0200", "NSW1"), ("200", "0200", "NSW1"),
    ("3000", "3000", "VIC1"), ("4000", "4000", "QLD1"), ("5000", "5000", "SA1"), ("7000", "7000", "TAS1"),
    ("2913", "2913", "NSW1"),         # Canberra: the ACT is in the NSW region
])
def test_postcodes(text, out, region):
    assert grid.normalize_postcode(text) == out
    assert grid.region_for_postcode(out) == region


@pytest.mark.parametrize("text", ["", "hello", "20000", "2000a", "20 00", "٢٠٠٠", "0100"])
def test_normalize_postcode_rejects(text):
    with pytest.raises(ValueError):
        grid.normalize_postcode(text)


@pytest.mark.parametrize("text,where", [("6000", "Western Australia"), ("0800", "Northern Territory")])
def test_places_outside_the_nem_are_explained(text, where):
    with pytest.raises(ValueError, match=where):
        grid.normalize_postcode(text)


@pytest.mark.parametrize("text,rid", [("nsw", "NSW1"), ("NSW1", "NSW1"), ("act", "NSW1"), ("Victoria", "VIC1"),
                                      ("vic", "VIC1"), ("queens", "QLD1"), ("south", "SA1"), ("tas", "TAS1")])
def test_region_from_arg(text, rid):
    assert grid.region_from_arg(text) == rid


@pytest.mark.parametrize("text", ["", "wa", "nt", "london"])
def test_region_from_arg_rejects(text):
    with pytest.raises(ValueError):
        grid.region_from_arg(text)


def test_interval_times_are_nem_time_and_mark_the_end():
    assert grid.interval_start("2026/10/09 16:00:00") == datetime(2026, 10, 9, 5, 30, tzinfo=UTC)
    with pytest.raises(ValueError):
        grid.interval_start("2026-10-09 16:00:00")


@pytest.mark.parametrize("text,value", [("4.32027", 4.32027), ("-675.08", -675.08), ("6E-05", 6e-05), ("0", 0.0)])
def test_decimal(text, value):
    assert grid.decimal(text, -1000, 1000) == value


@pytest.mark.parametrize("text", ["", " 1", "nan", "inf", "1e400", "0x10", "1,5", "5000"])
def test_decimal_rejects(text):
    with pytest.raises(ValueError):
        grid.decimal(text, -1000, 1000)


def test_newest_in_a_real_listing():
    with open(FX + "/listing_dispatch_scada.html", "rb") as f:
        assert grid.newest(f.read(), grid.REPORTS["scada"]) == SCADA


def test_newest_ignores_other_folders_and_names():
    page = listing("Other", [SCADA]) + listing("Dispatch_SCADA", ["DUPLICATE", "../" + SCADA, "x.zip"])
    with pytest.raises(ValueError, match="no Dispatch_SCADA report"):
        grid.newest(page, grid.REPORTS["scada"])


def test_predispatch():
    pd = report("predispatch", PREDISPATCH)
    assert set(pd) == set(grid.REGIONS)
    first = min(pd["NSW1"])
    assert first == datetime(2026, 10, 9, 5, 30, tzinfo=UTC) and len(pd["NSW1"]) == 73
    assert pd["NSW1"][first] == [53.15, 6063.42, 5799.25, 2715.82227, 496.018, 0.0, 540.85247]
    assert pd["NSW1"][datetime(2026, 10, 9, 8, 30, tzinfo=UTC)][5] == 1230      # BDU_CLEAREDMW_GEN at 19:00


def test_stpasa_starts_where_predispatch_ends():
    pd, st = report("predispatch", PREDISPATCH), report("stpasa", STPASA)
    assert min(st["NSW1"]) == max(pd["NSW1"]) + grid.SLOT and len(st["NSW1"]) == 288


def test_stpasa_wind_and_solar_are_the_forecast_not_the_reliability_run():
    st = report("stpasa", STPASA)
    t = datetime(2026, 10, 11, 0, 0, tzinfo=UTC)            # 11:00 in Sydney
    assert st["NSW1"][t] == [4014.0, 4134.25, 238.5]        # DEMAND50, SS_*_UIGF; SS_*_CLEARED are 1626, 163


def test_generators():
    units = report("generators", GENERATORS)
    assert units["BW01"] == ("NSW1", 0.88920196, "coal")       # Bayswater
    assert units["STAN-1"][2] == "coal" and units["TARONG#1"][0] == "QLD1"
    assert units["HPR1"][2] == "battery" and units["APPIN"][2] == "gas"
    assert units["W/HOE#1"] == ("QLD1", 0.0, "hydro")          # Wivenhoe pumped hydro


@pytest.mark.parametrize("source,fuel", [
    ("Black coal", "coal"), ("Brown coal", "coal"), ("Natural Gas (Pipeline)", "gas"), ("Coal seam methane", "gas"),
    ("Hydro", "hydro"), ("Battery Storage", "battery"), ("Diesel oil", "other"), ("Bagasse", "other"), (None, "other"),
])
def test_fuel_of(source, fuel):
    assert grid.fuel_of(source) == fuel


def test_fleet_leaves_out_wind_solar_batteries_and_strangers():
    units = {"C": ("SA1", 1.0, "coal"), "G": ("SA1", 0.5, "gas"), "W": ("SA1", 0.0, "wind"),
             "B": ("SA1", 0.0, "battery"), "V": ("VIC1", 1.2, "coal")}
    scada = {"C": 100.0, "G": 300.0, "W": 900.0, "B": 400.0, "V": 1000.0, "X": 50.0, "L": -20.0}
    intensity, shares = grid.fleet(units, scada, "SA1")
    assert intensity == pytest.approx(0.625) and shares == {"coal": 0.25, "gas": 0.75}
    assert grid.fleet(units, {"W": 900.0}, "SA1") is None


def test_fleet_from_the_fixtures():
    units, scada = report("generators", GENERATORS), report("scada", SCADA)
    by_region = {r: grid.fleet(units, scada, r)[0] for r in grid.REGIONS}
    assert by_region["VIC1"] > by_region["NSW1"] > by_region["SA1"] > by_region["TAS1"]     # brown coal ... hydro


def test_estimate():
    g, mix = grid.estimate(500, 250, 150, 100, (0.8, {"coal": 0.5, "gas": 0.5}))
    assert g == 400 and mix == {"coal": 25.0, "gas": 25.0, "solar": 25.0, "wind": 15.0, "rooftop": 10.0}
    assert grid.estimate(500, 0, 0, 0, None) == (None, None)


def test_grade():
    assert [grid.grade(g) for g in (0, 99, 100, 300, 500, 900)] == [
        "very low", "very low", "low", "moderate", "high", "very high"]


def test_beyond_predispatch_the_fleet_never_drops_below_its_predispatch_minimum():
    t0, t1 = NOW, NOW + grid.SLOT
    pd = {t0: [50.0, 1000.0, 1200.0, 600.0, 200.0, 0.0, 0.0]}    # 400 MW from the fleet, 200 MW exported
    st = {t1: [800.0, 900.0, 300.0]}                         # wind and solar alone would cover demand
    slots = grid.build_slots(t0, pd, st, {}, {}, (1.0, {"coal": 1.0}), n=2)
    assert slots[0].carbon == 333                           # 400 of 1200 MW
    # generation 800 + 200 = 1000 MW: the fleet's 400 at least, so wind and solar get 600 of their 1200
    assert slots[1].carbon == 400 and slots[1].mix == {"coal": 40.0, "solar": 45.0, "wind": 15.0}


def test_batteries_carry_the_carbon_of_the_hours_they_charge_in():
    pd = {NOW: [50.0, 1000.0, 1000.0, 800.0, 0.0, 0.0, 300.0],                  # midday: 200 g, charging 300 MW
          NOW + grid.SLOT: [50.0, 1000.0, 1000.0, 0.0, 400.0, 0.0, 100.0],      # 600 g, charging 100 MW
          NOW + grid.SLOT * 2: [50.0, 1000.0, 1000.0, 300.0, 100.0, 200.0, 0.0]}  # evening: 200 MW discharge
    slots = grid.build_slots(NOW, pd, {}, {}, {t: [0.0] for t in pd}, (1.0, {"coal": 1.0}), n=3)
    assert [s.carbon for s in slots[:2]] == [200, 600]
    # charged at (300 * 200 + 100 * 600) / 400 = 300 g; the fleet's 400 MW at 1000 g
    assert slots[2].carbon == 460 and slots[2].mix == {"solar": 30.0, "wind": 10.0, "battery": 20.0, "coal": 40.0}


def test_batteries_that_dont_charge_in_the_run_are_at_the_fleets_intensity():
    pd = {NOW: [50.0, 1000.0, 1000.0, 300.0, 100.0, 200.0, 0.0],
          NOW + grid.SLOT: [50.0, 1000.0, 1000.0, 800.0, 0.0, 0.0, -50.0]}      # a negative figure is not charging
    s = grid.build_slots(NOW, pd, {}, {}, {}, (1.0, {"coal": 1.0}), n=1)[0]
    assert s.carbon == 600


def test_charging_before_the_window_counts_too():
    evening = NOW + grid.SLOT * 2
    pd = {NOW: [50.0, 1000.0, 1000.0, 800.0, 0.0, 0.0, 100.0],                   # 200 g, charging 100 MW
          evening: [50.0, 1000.0, 1000.0, 300.0, 100.0, 200.0, 0.0]}
    overnight = {NOW - timedelta(hours=10): [50.0, 1000.0, 1000.0, 0.0, 0.0, 0.0, 300.0]}    # 1000 g, 300 MW
    roof = {t: [0.0] for t in [*pd, *overnight]}
    alone = grid.build_slots(NOW, pd, {}, {}, roof, (1.0, {"coal": 1.0}), n=3)
    both = grid.build_slots(NOW, pd, {}, {}, roof, (1.0, {"coal": 1.0}), n=3, history=overnight)
    # charged at 200 g, then at (100 * 200 + 300 * 1000) / 400 = 800 g
    assert alone[2].carbon == 440 and both[2].carbon == 560
    assert [s.start for s in both] == [s.start for s in alone]             # history is not shown


def test_charging_without_a_rooftop_forecast_is_left_out():
    """Taken as no rooftop solar, a midday half hour would read as if the fleet made it all."""
    pd = {NOW: [50.0, 1000.0, 1000.0, 300.0, 0.0, 0.0, 100.0],                   # 300 MW solar, rooftop unknown
          NOW + grid.SLOT: [50.0, 1000.0, 1000.0, 800.0, 0.0, 0.0, 100.0]}      # 200 g
    roof = {NOW + grid.SLOT: [0.0]}
    assert grid.charged_intensity(pd, roof, (1.0, {"coal": 1.0})) == pytest.approx(0.2)
    assert grid.charged_intensity(pd, {}, (1.0, {"coal": 1.0})) is None


def test_a_day_of_passed_half_hours_is_kept():
    rows = {"NSW1": {grid.iso(NOW): [1.0]}}
    old = {"NSW1": {NOW - timedelta(hours=h): [float(h)] for h in (1, 23.5, 24, 24.5)}}
    grid._keep_just_gone(rows, old)
    assert sorted(rows["NSW1"].values()) == [[1.0], [1.0], [23.5], [24.0]]


def remember(key, t, vals):
    """Add a passed half hour to a cached report, as earlier refreshes would have kept it."""
    path = grid._cache_path(key)
    box = json.load(open(path))
    box["doc"]["rows"]["NSW1"][grid.iso(t)] = vals
    json.dump(box, open(path, "w"))


def test_load_takes_charging_from_before_the_window():
    fc = grid.load(NOW, "2000", fetch=fake_fetch)
    overnight = NOW.replace(minute=30) - timedelta(hours=10)                # long before the window starts
    remember("nem-predispatch", overnight, [50.0, 8000.0, 8000.0, 0.0, 0.0, 0.0, 3000.0])
    evening = datetime(2026, 10, 9, 8, 30, tzinfo=UTC)                     # 19:30 in Sydney, batteries discharging
    assert grid.load(NOW, "2000", fetch=fake_fetch).at(evening).carbon == fc.at(evening).carbon   # no rooftop row
    remember("nem-rooftop", overnight, [0.0])
    again = grid.load(NOW, "2000", fetch=fake_fetch)                         # the cache is fresh: no download
    assert again.at(evening).carbon > fc.at(evening).carbon
    assert again.slots[0].start == fc.slots[0].start and again.at(NOW).carbon == fc.at(NOW).carbon
    assert again.slots[-1].carbon == fc.slots[-1].carbon       # nor is it in export_fit, past predispatch


def test_battery_discharge_beyond_the_rest_of_generation_is_capped():
    pd = {NOW: [50.0, 1000.0, 1000.0, 600.0, 300.0, 400.0, 0.0]}     # only 100 MW left for batteries
    s = grid.build_slots(NOW, pd, {}, {}, {}, (1.0, {"coal": 1.0}), n=1)[0]
    assert s.mix == {"solar": 60.0, "wind": 30.0, "battery": 10.0} and s.carbon == 100


def test_export_fit_recovers_a_line():
    pd = {NOW + grid.SLOT * i: [0.0, d, d + 100 + 0.5 * (vre - d), vre, 0.0, 0.0, 0.0]
          for i, (d, vre) in enumerate([(5000, 1000), (4000, 3000), (3000, 4500), (6000, 500)])}
    a, b = grid.export_fit(pd)
    assert a == pytest.approx(100) and b == pytest.approx(0.5)
    assert grid.export_fit({}) == (0.0, 0.0)
    assert grid.export_fit({NOW: [0.0, 1000.0, 1300.0, 0.0, 0.0, 0.0, 0.0]}) == (300.0, 0.0)  # one point: the gap


@pytest.mark.parametrize("region", ["NSW1", "QLD1", "VIC1"])
def test_no_step_where_predispatch_hands_over(region):
    """The first STPASA half hour reads close to the last predispatch one. With demand taken as
    generation, Victoria stepped 5% here."""
    fc = grid.load(NOW, region=region, fetch=fake_fetch)
    last = max(report("predispatch", PREDISPATCH)[region])
    before, after = fc.at(last).carbon, fc.at(last + grid.SLOT).carbon
    assert abs(after - before) / before < 0.03


def test_load_builds_48_hours(fc):
    assert fc.region == "New South Wales" and fc.region_id == "NSW1" and not fc.errors
    assert len(fc.slots) == 96
    assert fc.slots[0].start == datetime(2026, 10, 9, 3, 0, tzinfo=UTC)
    # nothing before the recorded run, then carbon for every half hour to the end of the window
    assert [s.carbon is None for s in fc.slots[:6]] == [True] * 5 + [False]
    assert all(s.carbon is not None for s in fc.slots[5:])
    assert fc.has_prices and fc.slots[-1].price == -0.882          # from PD7Day, past the predispatch horizon
    assert fc.at(NOW).start == datetime(2026, 10, 9, 5, 30, tzinfo=UTC)


def test_load_leaves_the_time_zone_to_whoever_shows_the_forecast():
    fc = grid.load(NOW, region="QLD1", fetch=fake_fetch)       # on a worker thread, the old forecast is still drawn
    assert fc.region == "Queensland" and grid.local(NOW).tzname() == "AEDT"


def test_load_without_prices():
    calls = []
    fc = grid.load(NOW, "2000", prices=False, fetch=lambda url: calls.append(url) or fake_fetch(url))
    assert not fc.has_prices and fc.slots[10].carbon is not None
    assert not any("PD7Day" in url for url in calls)


def test_cache_is_used_when_offline(monkeypatch):
    grid.load(NOW, "2000", fetch=fake_fetch)
    monkeypatch.setenv("GRIDHOUR_AU_OFFLINE", "1")
    fc = grid.load(NOW, "2000", force=True, fetch=lambda url: pytest.fail("fetched while offline"))
    assert fc.has_prices and fc.slots[10].carbon is not None and not fc.errors


def test_an_unchanged_listing_does_not_download_again():
    grid.load(NOW, "2000", fetch=fake_fetch)
    calls = []
    fc = grid.load(NOW, "2000", force=True, fetch=lambda url: calls.append(url) or fake_fetch(url))
    assert calls and all(url.endswith("/") for url in calls) and not fc.errors


def test_an_older_file_in_the_listing_does_not_replace_the_cache():
    grid.load(NOW, "2000", fetch=fake_fetch)
    older = "PUBLIC_STPASA_202610091400_0000000541900000.zip"
    calls = []

    def fetch(url):
        calls.append(url)
        if url.endswith("Short_Term_PASA_Reports/"):
            return listing("Short_Term_PASA_Reports", [older])       # a stale copy of the listing
        return fake_fetch(url)

    fc = grid.load(NOW, "2000", force=True, fetch=fetch)
    assert not fc.errors and not any(url.endswith(older) for url in calls)
    assert grid._read_cache("nem-stpasa")[1] == STPASA


def test_a_cached_name_stamped_in_the_future_is_not_trusted():
    grid.load(NOW, "2000", fetch=fake_fetch)
    path = grid._cache_path("nem-predispatch")
    box = json.load(open(path))
    box["file"], box["at"] = "PUBLIC_PREDISPATCHIS_999912312359_99991231235959.zip", box["at"] - 3 * 86400
    json.dump(box, open(path, "w"))
    calls = []
    fc = grid.load(NOW, "2000", fetch=lambda url: calls.append(url) or fake_fetch(url))
    assert any(url.endswith(PREDISPATCH) for url in calls) and not fc.errors
    assert grid._read_cache("nem-predispatch")[1] == PREDISPATCH


def an_hour_later(name, later, gone):
    """The fixture ``name`` as the next run an hour on would publish it: renamed ``later``, without
    the rows ``gone`` picks out (the two half hours that have passed)."""
    src = zipfile.ZipFile(io.BytesIO(fixture_bytes(name)))
    text = src.read(src.namelist()[0]).decode()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(later.replace(".zip", ".CSV"), "".join(ln for ln in text.splitlines(True) if not gone(ln)))
    return buf.getvalue()


def test_half_hours_just_gone_are_kept_from_the_previous_run():
    grid.load(NOW, "2000", fetch=fake_fetch)
    pd_later = "PUBLIC_PREDISPATCHIS_202610091700_20261009163245.zip"
    roof_later = "PUBLIC_ROOFTOP_PV_FORECAST_20261009163000_0000000541999999.zip"
    files = {
        pd_later: an_hour_later(PREDISPATCH, pd_later, lambda ln: ln.startswith("D,PREDISPATCH,REGION_")
                                and ln.split(",")[7] in ("01", "02")),
        roof_later: an_hour_later(ROOFTOP, roof_later, lambda ln: ln.startswith("D,ROOFTOP,")
                                  and ln.split(",")[6].strip('"') in ("2026/10/09 16:00:00", "2026/10/09 16:30:00")),
    }
    folders = {"PredispatchIS_Reports/": pd_later, "ROOFTOP_PV/FORECAST/": roof_later}

    def fetch(url):
        for folder, name in folders.items():
            if url.endswith(folder):
                return listing(folder.rstrip("/"), [name])
        name = url.rpartition("/")[2]
        return files[name] if name in files else fake_fetch(url)

    an_hour_on = NOW.replace(hour=6, minute=50)
    fc = grid.load(an_hour_on, "2000", force=True, fetch=fetch)
    assert not fc.errors
    before = fc.at(NOW)                                 # remembered from the earlier runs, rooftop included
    assert before.price == 5.315 and before.carbon == 247 and before.mix["rooftop"] == 33.7


def test_a_newer_stpasa_does_not_leave_a_hole_after_predispatch():
    """STPASA starts where the predispatch run it goes with ends. While the predispatch fetched is
    older than that, the previous STPASA run fills the hours between them."""
    grid.load(NOW, "2000", fetch=fake_fetch)
    later = "PUBLIC_STPASA_202610091600_0000000541999999.zip"
    body = an_hour_later(STPASA, later, lambda ln: ln.startswith("D,STPASA,REGIONSOLUTION,")
                         and ln.split(",")[5].strip('"') <= "2026/10/11 08:00:00")

    def fetch(url):
        if url.endswith("Short_Term_PASA_Reports/"):
            return listing("Short_Term_PASA_Reports", [later])
        return body if url.endswith(later) else fake_fetch(url)

    fc = grid.load(NOW, "2000", force=True, fetch=fetch)
    assert not fc.errors
    end = max(report("predispatch", PREDISPATCH)["NSW1"]) + grid.SLOT     # 04:00 NEM time on the 11th
    hole = [s for s in fc.slots if end <= s.start < end + grid.SLOT * 8]   # the new STPASA starts after this
    assert len(hole) == 8 and all(s.carbon is not None for s in hole)
    assert all(s.carbon is not None for s in fc.slots[5:])


def test_network_failure_falls_back_to_stale_cache():
    grid.load(NOW, "2000", fetch=fake_fetch)

    def broken(url):
        raise OSError("no route to host")

    fc = grid.load(NOW, "2000", force=True, fetch=broken)
    assert fc.slots[10].carbon is not None and fc.has_prices
    assert fc.errors and "no route" in fc.errors[0]


def test_network_failure_with_empty_cache():
    def broken(url):
        raise OSError("down")

    fc = grid.load(NOW, "2000", fetch=broken)
    assert fc.errors and all(s.carbon is None for s in fc.slots) and not fc.has_prices
