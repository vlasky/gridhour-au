"""Forecast data for the National Electricity Market, from AEMO's public NEMWeb reports.

Nothing here needs a key. Prices are AEMO's own forecasts of the regional spot price. AEMO does not
forecast carbon intensity, so it is estimated per half hour: forecast generation less forecast grid
wind and solar is taken to come from the rest of the region's fleet, at the emissions intensity that
fleet is running at right now (the latest 5 minute SCADA readings times AEMO's emissions factor for
each unit), and forecast rooftop solar is added to the supply.

Each report is reduced to a few numbers per half hour before it is cached under ~/.cache/gridhour-au,
so the app starts instantly and keeps working without a network, with the age of the data shown.
"""
import csv
import http.client
import io
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import zipfile
import zlib
from concurrent.futures import Future
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from threading import BoundedSemaphore, Thread
from zoneinfo import ZoneInfo

from . import __version__
from .safe import atomic_write, label

NEMWEB = "https://nemweb.com.au"
CURRENT = NEMWEB + "/Reports/CURRENT/"
ALLOWED = (NEMWEB + "/Reports/",)
SLOT = timedelta(minutes=30)
WINDOW_SLOTS = 96               # 48 hours
PAST = timedelta(hours=2)       # how much of the timeline lies before now
HISTORY = timedelta(hours=24)   # how long passed half hours are kept, for what batteries charged with
MAX_AGE = 30 * 60               # seconds before a cached report is checked for a newer one
MAX_BYTES = 8 * 1024 * 1024     # the biggest report (PD7Day) is about 5 MB zipped
MAX_TEXT = 96 * 1024 * 1024     # and about 50 MB unzipped; more than this is not a report
MAX_LINE = 64 * 1024
DEADLINE = 30                   # seconds for a whole request, however slowly the bytes trickle in
MAX_ROWS = 10000                # rows of the wanted tables looked at in one report
CACHE_KEY = re.compile(r"[a-z]+(?:-[a-z0-9]+)+", re.ASCII)
MARKET = timezone(timedelta(hours=10), "AEST")     # NEM time: Brisbane time all year, no daylight saving
FUELS = {"coal", "gas", "hydro", "wind", "solar", "rooftop", "battery", "other"}
INDEXES = {"very low", "low", "moderate", "high", "very high"}
BANDS = ((100, "very low"), (250, "low"), (450, "moderate"), (650, "high"))     # gCO2/kWh upper bounds
GREEN_INDEX = ("very low", "low")

# NEM region -> (name, short name, time zone people there live in)
REGIONS = {
    "NSW1": ("New South Wales", "NSW", "Australia/Sydney"),
    "QLD1": ("Queensland", "QLD", "Australia/Brisbane"),
    "VIC1": ("Victoria", "VIC", "Australia/Melbourne"),
    "SA1": ("South Australia", "SA", "Australia/Adelaide"),
    "TAS1": ("Tasmania", "TAS", "Australia/Hobart"),
}
DEFAULT_REGION = "NSW1"


@dataclass
class Slot:
    start: datetime                 # UTC, on a half hour
    carbon: int = None              # gCO2e/kWh, estimated
    index: str = None               # "very low" ... "very high", from BANDS
    mix: dict = None                # fuel -> percent of supply, rooftop solar included
    price: float = None             # wholesale spot price, c/kWh excluding GST

    @property
    def end(self):
        return self.start + SLOT

    @property
    def green(self):
        return self.index in GREEN_INDEX


@dataclass
class Forecast:
    region: str = REGIONS[DEFAULT_REGION][0]
    region_id: str = DEFAULT_REGION
    postcode: str = None
    slots: list = field(default_factory=list)
    fetched: float = 0.0            # unix time of the oldest data shown
    errors: list = field(default_factory=list)

    def at(self, t):
        """The slot that contains ``t``, or None."""
        for s in self.slots:
            if s.start <= t < s.end:
                return s
        return None

    @property
    def has_prices(self):
        return any(s.price is not None for s in self.slots)

    @property
    def has_data(self):
        return any(s.carbon is not None or s.price is not None for s in self.slots)


# ---------------------------------------------------------------- where

_ZONE = [ZoneInfo(REGIONS[DEFAULT_REGION][2])]


def set_zone(region):
    """Show times in the region's local time, like apply_theme for colours. Set by whoever puts
    a forecast on screen (not load(), which runs on a worker thread while the old one is drawn)."""
    _ZONE[0] = ZoneInfo(REGIONS.get(region, REGIONS[DEFAULT_REGION])[2])


def zone():
    return _ZONE[0]


def local(t):
    return t.astimezone(_ZONE[0])


def normalize_postcode(text):
    """'2000', ' 3000 ' -> '2000'. Raises ValueError."""
    s = str(text or "").strip()
    if not re.fullmatch(r"[0-9]{3,4}", s, re.ASCII):
        raise ValueError("not an Australian postcode: %r" % label(str(text), 20))
    s = s.zfill(4)
    region_for_postcode(s)
    return s


def region_for_postcode(postcode):
    """The NEM region a postcode is in, by Australia Post's state ranges. Raises ValueError."""
    n = int(postcode)
    if 800 <= n <= 999:
        raise ValueError("the Northern Territory is not part of the NEM, so there is no forecast for %s" % postcode)
    if 6000 <= n <= 6999:
        raise ValueError("Western Australia is not part of the NEM, so there is no forecast for %s" % postcode)
    if 200 <= n <= 299 or 1000 <= n <= 2999:        # NSW, and the ACT, which is in the NSW region
        return "NSW1"
    if 3000 <= n <= 3999 or 8000 <= n <= 8999:
        return "VIC1"
    if 4000 <= n <= 4999 or 9000 <= n <= 9999:
        return "QLD1"
    if 5000 <= n <= 5999:
        return "SA1"
    if 7000 <= n <= 7999:
        return "TAS1"
    raise ValueError("not an Australian postcode: %s" % postcode)


def region_from_arg(text):
    """Accept a region id (NSW1), a state (nsw, act) or a name (victoria). Returns the region id."""
    t = str(text or "").strip().lower()
    if t == "act":
        return "NSW1"
    for rid, (name, short, _) in REGIONS.items():
        if t in (rid.lower(), short.lower(), name.lower()):
            return rid
    for rid, (name, _, _) in REGIONS.items():
        if t and name.lower().startswith(t):
            return rid
    raise ValueError("unknown region %r (try nsw, vic, qld, sa or tas)" % label(str(text), 30))


# ---------------------------------------------------------------- time

def floor_slot(t):
    return t.replace(minute=t.minute - t.minute % 30, second=0, microsecond=0)


def window_start(now):
    return now.replace(minute=0, second=0, microsecond=0) - PAST


def iso(t):
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%MZ")


TIME = re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d(?::\d\d(?:\.\d+)?)?(?:Z|[+-]\d\d:?\d\d)?", re.ASCII)
NEM_TIME = re.compile(r"\d{4}/\d\d/\d\d \d\d:\d\d:\d\d", re.ASCII)


def parse_time(s):
    """ISO 8601 -> aware UTC datetime. Without an offset the time is taken as UTC."""
    if not isinstance(s, str) or not TIME.fullmatch(s.strip()):
        raise ValueError("not a time: %r" % (s if isinstance(s, str) and len(s) < 40 else type(s).__name__))
    s = s.strip().replace("Z", "+00:00")
    if re.search(r"T\d\d:\d\d[+-]", s):
        s = s[:16] + ":00" + s[16:]
    try:
        t = datetime.fromisoformat(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=UTC)
        return t.astimezone(UTC)
    except (ValueError, OverflowError) as e:
        raise ValueError("not a time: %r" % s) from e


def interval_start(s):
    """An AEMO interval time ('2026/10/09 16:00:00', NEM time, the end of the interval) ->
    the UTC start of the half hour it closes."""
    if not isinstance(s, str) or not NEM_TIME.fullmatch(s):
        raise ValueError("not a NEM time")
    try:
        t = datetime.strptime(s, "%Y/%m/%d %H:%M:%S").replace(tzinfo=MARKET)
    except ValueError as e:
        raise ValueError("not a NEM time") from e
    return t.astimezone(UTC) - SLOT


# ---------------------------------------------------------------- parsing (everything here is untrusted)

def number(v, lo, hi):
    """A finite real number within [lo, hi]. Booleans and strings are refused."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError("not a number")
    f = float(v)
    if not math.isfinite(f) or not lo <= f <= hi:
        raise ValueError("number out of range")
    return f


DECIMAL = re.compile(r"-?[0-9]{1,9}(?:\.[0-9]{1,15})?(?:[eE][-+]?[0-9]{1,2})?", re.ASCII)


def decimal(text, lo, hi):
    """A decimal from a CSV field ('4.32027', '-675.08', '6E-05') within [lo, hi]. Raises ValueError."""
    if not isinstance(text, str) or not DECIMAL.fullmatch(text):
        raise ValueError("not a number")
    return number(float(text), lo, hi)


def csv_lines(body):
    """The lines of a report: a zip holding one CSV, or a CSV. Unzipped size is capped as it is read,
    so a small zip cannot expand without limit."""
    try:
        if body[:2] == b"PK":
            z = zipfile.ZipFile(io.BytesIO(body))
            members = z.infolist()
            if len(members) != 1 or not members[0].filename.lower().endswith(".csv"):
                raise ValueError("response is not one report")
            raw = z.open(members[0])
        else:
            raw = io.BytesIO(body)
        total = 0
        with raw:
            while True:
                line = raw.readline(MAX_LINE + 1)
                if not line:
                    return
                total += len(line)
                if len(line) > MAX_LINE or total > MAX_TEXT:
                    raise ValueError("response larger than a report")
                yield line.decode("utf-8", "replace")
    except (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError, OSError) as e:
        raise ValueError("response is not a readable report") from e


def aemo_rows(lines, tables):
    """AEMO's CSV format: 'I' rows name the columns of a table, 'D' rows carry its data. Yields
    (table, {column: text}) for the wanted tables; other rows are skipped before they are split."""
    cols = {}
    n = 0
    for line in lines:
        head = line.split(",", 4)
        if len(head) < 5 or head[0] not in ("I", "D") or head[2] not in tables:
            continue
        try:
            row = next(csv.reader([line]))
        except csv.Error:
            continue                    # a stray CR in a field, say: that row, not the report
        if row[0] == "I":
            cols[row[2]] = row[4:]
        elif row[2] in cols and len(row) - 4 == len(cols[row[2]]):     # a cut-off row would read as zeros
            n += 1
            if n > MAX_ROWS:
                return
            yield row[2], dict(zip(cols[row[2]], row[4:], strict=True))


def _value(v, rng):
    """A checked number, or None where the range allows a gap: (lo, hi, None)."""
    if v is None and len(rng) == 3:
        return None
    return number(v, *rng[:2])


def _region_table(doc, ranges):
    """{region: {iso start: [numbers]}} as cached -> {region: {start: [numbers]}}, checked."""
    rows = doc.get("rows") if isinstance(doc, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("unexpected report")
    out = {}
    for region, table in rows.items():
        if region not in REGIONS or not isinstance(table, dict):
            continue
        clean = {}
        for t, vals in list(table.items())[:MAX_ROWS]:
            try:
                if not isinstance(vals, list) or len(vals) != len(ranges):
                    continue
                clean[parse_time(t)] = [_value(v, rng) for v, rng in zip(vals, ranges, strict=True)]
            except ValueError:
                continue                # one bad row costs that half hour, not the forecast
        if clean:
            out[region] = clean
    if not out:
        raise ValueError("no forecast in the report")
    return out


PRICE = (-1100.0, 30000.0)      # $/MWh: the market floor is -1000, the cap about 20 000 and rising
MW = (-5000.0, 30000.0)
ROOFTOP = (0.0, 30000.0)
BATTERY = (0.0, 30000.0)        # discharge and charging are each reported as a positive MW
PRICE_OR_GAP = (*PRICE, None)   # predispatch: a bad price costs that half hour's price, not its carbon


def _field(row, name, rng, blank=None):
    v = row.get(name, "")
    if v == "" and blank is not None:
        return blank
    return decimal(v, *rng)


def _keep_just_gone(rows, old, back=HISTORY):
    """Add the previous report's rows for the half hours (up to ``back``) before each region's
    first new one.

    Predispatch and the rooftop forecast both start at the current half hour, so without this
    the half hours just gone would be empty. They were forecast 30 minutes earlier, which is
    close to what happened. A day of them is kept, not just the two hours on screen: batteries
    discharge what they charged then (``charged_intensity``)."""
    for region, table in (old or {}).items():
        if region not in rows:
            continue
        first = min(parse_time(t) for t in rows[region])
        for t, vals in table.items():
            if first - back <= t < first:
                rows[region][iso(t)] = vals


def extract_predispatch(lines, old=None):
    """PREDISPATCHIS report -> {"rows": {region: {start: [rrp, demand, generation, solar, wind,
    battery, charging]}}}. rrp is None where the price row is missing or bad. battery is what
    batteries discharge, part of generation; charging is what they take in."""
    sol, prices = {}, {}
    for table, row in aemo_rows(lines, ("REGION_SOLUTION", "REGION_PRICES")):
        if row.get("INTERVENTION") != "0" or row.get("REGIONID") not in REGIONS:
            continue
        try:
            key = (row["REGIONID"], iso(interval_start(row.get("DATETIME"))))
            if table == "REGION_PRICES":
                prices[key] = _field(row, "RRP", PRICE)
            else:
                sol[key] = [_field(row, "TOTALDEMAND", MW), _field(row, "DISPATCHABLEGENERATION", MW),
                            _field(row, "SS_SOLAR_CLEAREDMW", MW, 0.0), _field(row, "SS_WIND_CLEAREDMW", MW, 0.0),
                            _field(row, "BDU_CLEAREDMW_GEN", BATTERY, 0.0),
                            _field(row, "BDU_CLEAREDMW_LOAD", BATTERY, 0.0)]
        except (ValueError, KeyError):
            continue
    rows = {}
    for (region, t), vals in sol.items():
        rows.setdefault(region, {})[t] = [prices.get((region, t))] + vals
    _keep_just_gone(rows, old)
    return {"rows": rows}


def extract_stpasa(lines, old=None):
    """STPASA report -> {"rows": {region: {start: [demand, solar, wind]}}}: the 50% POE demand, and
    the wind and solar AEMO forecasts to be available (UIGF). The CLEARED figures beside them come
    from STPASA's reliability run, which dispatches only what it needs, so they say little about
    how much will actually generate: at a NSW noon, 1.6 GW cleared against 4.5 GW forecast."""
    rows, other = {}, {}
    for _, row in aemo_rows(lines, ("REGIONSOLUTION",)):
        if row.get("REGIONID") not in REGIONS:
            continue
        try:
            t = iso(interval_start(row.get("INTERVAL_DATETIME")))
            vals = [_field(row, "DEMAND50", MW), _field(row, "SS_SOLAR_UIGF", MW, 0.0),
                    _field(row, "SS_WIND_UIGF", MW, 0.0)]
        except (ValueError, KeyError):
            continue
        # the LOR run is the one with every unit's availability; an outage run, if there is one, is not
        (rows if row.get("RUNTYPE") == "LOR" else other).setdefault(row["REGIONID"], {})[t] = vals
    rows = rows or other
    # STPASA starts where the latest predispatch ends. Until a fetch gets that predispatch too
    # (it failed, or was published later), the day between would be empty without the previous run.
    _keep_just_gone(rows, old, back=timedelta(days=2))
    return {"rows": rows}


def extract_pd7day(lines, old=None):
    """PD7DAY report -> {"rows": {region: {start: [rrp]}}}."""
    rows = {}
    for _, row in aemo_rows(lines, ("PRICESOLUTION",)):
        if row.get("INTERVENTION") != "0" or row.get("REGIONID") not in REGIONS:
            continue
        try:
            rows.setdefault(row["REGIONID"], {})[iso(interval_start(row.get("INTERVAL_DATETIME")))] = \
                [_field(row, "RRP", PRICE)]
        except (ValueError, KeyError):
            continue
    return {"rows": rows}


def extract_rooftop(lines, old=None):
    """ROOFTOP_PV_FORECAST report -> {"rows": {region: {start: [mw]}}}, the mean forecast."""
    rows = {}
    for _, row in aemo_rows(lines, ("FORECAST",)):
        if row.get("REGIONID") not in REGIONS:
            continue
        try:
            mw = _field(row, "POWERMEAN", ROOFTOP)
            rows.setdefault(row["REGIONID"], {})[iso(interval_start(row.get("INTERVAL_DATETIME")))] = [mw]
        except (ValueError, KeyError):
            continue
    _keep_just_gone(rows, old)
    return {"rows": rows}


DUID = re.compile(r"[A-Za-z0-9_#/-]{1,20}", re.ASCII)      # 'BW01', 'STAN-1', 'TARONG#1', 'W/HOE#1'


def extract_scada(lines, old=None):
    """DISPATCHSCADA report -> {"mw": {duid: MW}}, every unit's output in the last 5 minutes."""
    mw = {}
    for _, row in aemo_rows(lines, ("UNIT_SCADA",)):
        try:
            if DUID.fullmatch(row.get("DUID", "")):
                mw[row["DUID"]] = _field(row, "SCADAVALUE", MW)
        except ValueError:
            continue
    return {"mw": mw}


SOURCES = (("coal", ("black coal", "brown coal")),
           ("gas", ("natural gas", "coal seam methane", "coal mine waste gas", "ethane")),
           ("hydro", ("hydro",)), ("wind", ("wind",)), ("solar", ("solar",)), ("battery", ("battery",)))


def fuel_of(source):
    """AEMO's CO2E_ENERGY_SOURCE ('Black coal', 'Natural Gas (Pipeline)') -> one of FUELS."""
    s = source.strip().lower() if isinstance(source, str) else ""
    for fuel, prefixes in SOURCES:
        if s.startswith(prefixes):
            return fuel
    return "other"                  # diesel, kerosene, bagasse, landfill gas, biofuels


def extract_generators(lines, old=None):
    """CO2EII_AVAILABLE_GENERATORS -> {"units": {duid: [region, tCO2e/MWh, fuel]}}. A unit listed
    with several generator sets gets their mean factor; SCADA reports the unit, not the sets."""
    units, factors = {}, {}
    for _, row in aemo_rows(lines, ("PUBLISHING",)):
        duid, region = row.get("DUID", ""), row.get("REGIONID")
        if not DUID.fullmatch(duid) or region not in REGIONS:
            continue
        try:
            factors.setdefault(duid, []).append(_field(row, "CO2E_EMISSIONS_FACTOR", (0.0, 3.0)))
        except ValueError:
            continue
        units.setdefault(duid, [region, None, fuel_of(row.get("CO2E_ENERGY_SOURCE"))])
    for duid, u in units.items():
        u[1] = sum(factors[duid]) / len(factors[duid])
    return {"units": units}


def parse_mw(doc):
    mw = doc.get("mw") if isinstance(doc, dict) else None
    if not isinstance(mw, dict):
        raise ValueError("unexpected report")
    out = {}
    for duid, v in list(mw.items())[:MAX_ROWS]:
        try:
            if isinstance(duid, str) and DUID.fullmatch(duid):
                out[duid] = number(v, *MW)
        except ValueError:
            continue
    if not out:
        raise ValueError("no unit readings in the report")
    return out


def parse_units(doc):
    units = doc.get("units") if isinstance(doc, dict) else None
    if not isinstance(units, dict):
        raise ValueError("unexpected report")
    out = {}
    for duid, u in list(units.items())[:MAX_ROWS]:
        try:
            if (isinstance(duid, str) and DUID.fullmatch(duid) and isinstance(u, list) and len(u) == 3
                    and u[0] in REGIONS and isinstance(u[2], str) and u[2] in FUELS):
                out[duid] = (u[0], number(u[1], 0.0, 3.0), u[2])
        except ValueError:
            continue
    if not out:
        raise ValueError("no generators in the report")
    return out


def fleet(units, scada, region):
    """(tCO2e/MWh, {fuel: share}) of the region's fleet other than grid wind and solar, as it runs
    right now. None when nothing in it is running (or there is no data).

    Batteries are left out: their discharge is taken at the intensity of the hours they charged
    in (``charged_intensity``), or at this one past the end of predispatch."""
    by_fuel, tonnes = {}, 0.0
    for duid, mw in scada.items():
        u = units.get(duid)
        if u is None or u[0] != region or mw <= 0 or u[2] in ("wind", "solar", "battery"):
            continue
        by_fuel[u[2]] = by_fuel.get(u[2], 0.0) + mw
        tonnes += mw * u[1]
    total = sum(by_fuel.values())
    if total < 1:
        return None
    return tonnes / total, {f: v / total for f, v in by_fuel.items()}


def grade(g):
    for top, name in BANDS:
        if g < top:
            return name
    return "very high"


def estimate(residual, solar, wind, rooftop, fleet_now, battery=0.0, charged=None):
    """(gCO2e/kWh, mix) for a half hour: ``residual`` MW from the fleet at its current intensity,
    plus grid solar, wind and rooftop solar at zero, and ``battery`` MW of discharge at the
    intensity it was ``charged`` at (tCO2e/MWh; the fleet's if None). Batteries return energy the
    grid made earlier, so taking it as zero-carbon would flatter whatever hour they run in."""
    supply = residual + battery + solar + wind + rooftop
    if fleet_now is None or supply < 1:
        return None, None
    intensity, shares = fleet_now
    charged = intensity if charged is None else charged
    mix = {"solar": solar, "wind": wind, "rooftop": rooftop, "battery": battery}
    for fuel, share in shares.items():
        mix[fuel] = mix.get(fuel, 0.0) + residual * share
    mix = {f: round(v / supply * 100, 1) for f, v in mix.items() if v / supply >= 0.0005}
    return int(round((residual * intensity + battery * charged) / supply * 1000)), mix


def _predispatch_supply(row):
    """(fleet, solar, wind, battery) MW of a predispatch half hour. Discharge beyond what the
    fleet would otherwise make is capped: it is part of generation."""
    _, _, gen, solar, wind, battery, _ = row
    battery = min(battery, max(0.0, gen - solar - wind))
    return max(0.0, gen - solar - wind - battery), max(0.0, solar), max(0.0, wind), battery


def charged_intensity(pd, roof, fleet_now):
    """tCO2e/MWh of what batteries discharge: the estimated intensity of the predispatch half
    hours they charge in, weighted by how much they take in. None if they don't charge in the run.

    Batteries mostly charge at midday, from solar, so this is well below the fleet's intensity
    (2% to 44% of it in six runs on 26 and 27 October 2026). Batteries often discharge more than the run
    shows them charging, from what they held when it started; that energy is taken to have been
    charged like the rest. A half hour without a rooftop forecast is left out rather than taken
    as no rooftop solar, which at midday would make the charging read far dirtier."""
    if fleet_now is None:
        return None
    tonnes = taken = 0.0
    for t, row in pd.items():
        charging = row[6]
        fleet_mw, solar, wind, battery = _predispatch_supply(row)
        if charging <= 0 or t not in roof:
            continue
        supply = fleet_mw + battery + solar + wind + roof[t][0]
        if supply >= 1:
            tonnes += charging * (fleet_mw + battery) * fleet_now[0] / supply
            taken += charging
    return tonnes / taken if taken else None


def export_fit(pd):
    """(a, b) for generation - demand = a + b * (wind + solar - demand), least squares over the
    predispatch run. That gap is what a region exports (negative: imports) plus what its batteries
    and pumps take, and it grows with how far wind and solar outrun demand. Fitted on the first
    half of a run, this predicted the fleet's output in the second half with 7% to 70% less error
    than taking the gap as zero in NSW, QLD and VIC on two days; in SA it was better on one, worse
    on the other (README, "Past the end of predispatch")."""
    xs = [solar + wind - demand for _, demand, _, solar, wind, _, _ in pd.values()]
    ys = [gen - demand for _, demand, gen, _, _, _, _ in pd.values()]
    if not xs:
        return 0.0, 0.0
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx if sxx > 1 else 0.0
    return my - b * mx, b


def build_slots(start, pd, st, p7, roof, fleet_now, n=WINDOW_SLOTS, history=None):
    """Half hours from ``start``: predispatch where it reaches, then STPASA for supply and PD7Day
    for prices.

    Beyond predispatch only demand is forecast, not generation: nothing says how much a region
    will import or export. So generation there is demand plus the gap ``export_fit`` predicts from
    predispatch. The fleet covers what wind and solar don't, but never less than the least
    predispatch has it running: coal units can't stop for a sunny afternoon, they export. Wind and
    solar beyond that are taken as curtailed. STPASA has no battery figure, so there battery
    discharge stays inside the fleet's share. ``history`` is predispatch's half hours before
    ``start``, for what batteries charged with."""
    floor = min((max(0.0, gen - solar - wind) for _, _, gen, solar, wind, _, _ in pd.values()), default=0.0)
    a, b = export_fit(pd)
    charged = charged_intensity({**(history or {}), **pd}, roof, fleet_now)
    slots = []
    for i in range(n):
        t = start + SLOT * i
        price, carbon, mix, battery = None, None, None, 0.0
        if t in pd:
            price = pd[t][0]
            residual, solar, wind, battery = _predispatch_supply(pd[t])
        elif t in st:
            demand, solar, wind = st[t]
            gen = max(0.0, demand + a + b * (solar + wind - demand))
            residual = max(floor, gen - solar - wind)
            room = max(0.0, gen - residual)             # what's left for wind and solar
            if solar + wind > room:
                scale = room / (solar + wind)
                solar, wind = solar * scale, wind * scale
        else:
            residual = None
        if price is None and t in p7:
            price = p7[t][0]
        if residual is not None:
            carbon, mix = estimate(residual, max(0.0, solar), max(0.0, wind), roof.get(t, [0.0])[0], fleet_now,
                                   battery, charged)
        slots.append(Slot(t, carbon, grade(carbon) if carbon is not None else None, mix,
                          None if price is None else round(price / 10, 3)))
    return slots


# ---------------------------------------------------------------- network and cache

def cache_dir():
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "gridhour-au")


def offline():
    return os.environ.get("GRIDHOUR_AU_OFFLINE", "") not in ("", "0")


def _cache_path(key):
    if not CACHE_KEY.fullmatch(key):
        raise ValueError("bad cache key")
    return os.path.join(cache_dir(), key + ".json")


def _read_cache(key):
    """(fetched at, report file name, doc), or Nones."""
    try:
        with open(_cache_path(key), encoding="utf-8") as f:
            box = json.loads(f.read(MAX_BYTES + 1))
        at = number(box["at"], 0, time.time() + 300)    # a timestamp from the future would never expire
        name = box["file"]
        return at, name if isinstance(name, str) else None, box["doc"]
    except (OSError, ValueError, KeyError, TypeError, RecursionError):
        return None, None, None


def _write_cache(key, name, doc):
    try:
        atomic_write(_cache_path(key), json.dumps({"at": time.time(), "file": name, "doc": doc}))
    except (OSError, ValueError):
        pass


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """NEMWeb answers directly. A redirect could lead anywhere, plain http included."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, "redirect refused", headers, fp)


_OPENER = urllib.request.build_opener(_RefuseRedirects)
# A resolver cannot be cancelled; cap abandoned requests and never let them delay process exit.
_REQUEST_SLOTS = BoundedSemaphore(2)


def http_get(url, timeout=12):
    if not isinstance(url, str) or not url.startswith(ALLOWED):
        raise ValueError("refusing to fetch %r" % url)
    deadline = time.monotonic() + DEADLINE
    slots = _REQUEST_SLOTS
    if not slots.acquire(timeout=DEADLINE):
        raise TimeoutError("took longer than %s seconds" % DEADLINE)
    result = Future()

    def fetch():
        try:
            result.set_result(_http_get(url, timeout, deadline))
        except Exception as e:  # noqa: BLE001 -- forward worker failures to the calling thread
            result.set_exception(e)
        finally:
            slots.release()

    worker = Thread(target=fetch, daemon=True)
    try:
        worker.start()
    except RuntimeError:
        slots.release()
        raise
    try:
        return result.result(timeout=max(0, deadline - time.monotonic()))
    except TimeoutError:
        raise TimeoutError("took longer than %s seconds" % DEADLINE) from None


def _http_get(url, timeout, deadline):
    if time.monotonic() >= deadline:
        raise TimeoutError("request deadline expired")
    # scheme and host are checked above and redirects are refused, so S310 cannot apply
    req = urllib.request.Request(url, headers={"User-Agent": "gridhour-au/" + __version__})  # noqa: S310
    chunks, size = [], 0
    with _OPENER.open(req, timeout=timeout) as r:
        if not r.geturl().startswith(ALLOWED):
            raise ValueError("response came from somewhere else")
        while True:
            if time.monotonic() > deadline:
                raise TimeoutError("took longer than %d seconds" % DEADLINE)
            chunk = r.read(64 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("response larger than %d bytes" % MAX_BYTES)
            chunks.append(chunk)
    return b"".join(chunks)


def _why(e):
    """A short reason for the status line that never echoes response content."""
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code
    if isinstance(e, urllib.error.URLError):
        return label(str(e.reason), 50) or "unreachable"
    if isinstance(e, (UnicodeDecodeError, RecursionError)):
        return "unreadable response"
    if isinstance(e, (TimeoutError, OSError)):
        return label(str(e), 50) or type(e).__name__
    if isinstance(e, ValueError) and str(e).startswith(("response", "refusing", "took", "no ")):
        return label(str(e), 60)
    return type(e).__name__


FETCH_ERRORS = (urllib.error.URLError, OSError, ValueError, http.client.HTTPException, RecursionError,
                OverflowError)


@dataclass(frozen=True)
class Report:
    key: str                # cache key
    folder: str             # under /Reports/CURRENT/
    name: str               # regex for the file names, always used with fullmatch
    extract: object         # (lines, the cached data parsed, or None) -> doc
    parse: object           # doc -> checked data; raises ValueError
    max_age: int = MAX_AGE
    fixed: bool = False     # ``name`` is one file that is replaced in place, not a series


REPORTS = {
    "predispatch": Report("nem-predispatch", "PredispatchIS_Reports", r"PUBLIC_PREDISPATCHIS_\d{12}_\d{14}\.zip",
                          extract_predispatch,
                          lambda d: _region_table(d, (PRICE_OR_GAP, MW, MW, MW, MW, BATTERY, BATTERY))),
    "stpasa": Report("nem-stpasa", "Short_Term_PASA_Reports", r"PUBLIC_STPASA_\d{12}_\d{16}\.zip",
                     extract_stpasa, lambda d: _region_table(d, (MW, MW, MW))),
    "rooftop": Report("nem-rooftop", "ROOFTOP_PV/FORECAST", r"PUBLIC_ROOFTOP_PV_FORECAST_\d{14}_\d{16}\.zip",
                      extract_rooftop, lambda d: _region_table(d, (ROOFTOP,))),
    "scada": Report("nem-scada", "Dispatch_SCADA", r"PUBLIC_DISPATCHSCADA_\d{12}_\d{16}\.zip",
                    extract_scada, parse_mw),
    "generators": Report("nem-generators", "CDEII", "CO2EII_AVAILABLE_GENERATORS.CSV", extract_generators,
                         parse_units, max_age=86400, fixed=True),
    # published about three times a day and 5 MB, so looked for less often
    "pd7day": Report("nem-pd7day", "PD7Day", r"PUBLIC_PD7DAY_\d{14}_\d{16}\.zip", extract_pd7day,
                     lambda d: _region_table(d, (PRICE,)), max_age=2 * 3600),
}


def newest(listing, report):
    """The newest file of ``report`` in a NEMWeb directory listing. Names carry their timestamps,
    so the newest sorts last."""
    text = listing.decode("utf-8", "replace") if isinstance(listing, bytes) else ""
    prefix = "/Reports/CURRENT/%s/" % report.folder
    names = [m for m in re.findall(r'HREF="([^"]{1,200})"', text, re.IGNORECASE)
             if m.startswith(prefix) and re.fullmatch(report.name, m[len(prefix):], re.ASCII)]
    if not names:
        raise ValueError("no %s report on NEMWeb" % report.folder.split("/")[0])
    return max(names)[len(prefix):]


def _stamped_later(name):
    """Whether a report name's timestamp (its first 12 digits: run or publish time, NEM time) is
    more than an hour from now, i.e. not a real report."""
    m = re.search(r"_(\d{12})", name)
    try:
        t = datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(tzinfo=MARKET)
    except (AttributeError, ValueError):
        return True
    return t > datetime.now(UTC) + timedelta(hours=1)


def cached_report(report, force=False, fetch=None, cache_only=False):
    """(fetched_at, parsed, error). A report is reduced and checked before it is cached, so a broken
    one is never stored; a cache entry that no longer passes counts as missing. When the listing
    still shows the cached file (or an older one), only the cache's clock moves. Falls back to a
    stale cache when the network fails."""
    get = fetch or http_get
    at, name, doc = _read_cache(report.key)
    parsed = None
    if doc is not None:
        try:
            parsed = report.parse(doc)
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, OverflowError):
            at, name, doc, parsed = None, None, None, None
    if parsed is not None and not force and time.time() - at < report.max_age:
        return at, parsed, None
    if offline() or cache_only:
        return at, parsed, None if parsed is not None or cache_only else "offline and nothing cached yet"
    try:
        latest = report.name if report.fixed else newest(get(CURRENT + report.folder + "/"), report)
        # names sort by time; a stale copy of the listing along the way can show an older file.
        # A cached name stamped in the future would stop it ever refreshing, so it doesn't count.
        if (parsed is not None and not report.fixed and name and re.fullmatch(report.name, name, re.ASCII)
                and (latest == name or latest < name and not _stamped_later(name))):
            _write_cache(report.key, name, doc)
            return time.time(), parsed, None
        fresh = report.extract(csv_lines(get(CURRENT + report.folder + "/" + latest)), parsed)
        result = report.parse(fresh)
    except FETCH_ERRORS as e:
        return at, parsed, "network: %s" % _why(e)
    except (TypeError, KeyError, AttributeError):
        return at, parsed, "network: unreadable response"
    _write_cache(report.key, latest, fresh)
    return time.time(), result, None


ERROR_NAMES = {"predispatch": "forecast", "stpasa": "outlook", "rooftop": "rooftop solar", "scada": "live mix",
               "generators": "emissions factors", "pd7day": "7 day prices"}


def empty_forecast(postcode=None, region=None):
    """A forecast with nothing in it yet, for a postcode or else a region (NSW if neither)."""
    if postcode is not None:
        postcode = normalize_postcode(postcode)
        region = region_for_postcode(postcode)
    if region not in REGIONS:
        region = DEFAULT_REGION
    return Forecast(region=REGIONS[region][0], region_id=region, postcode=postcode)


def load(now, postcode=None, region=None, prices=True, force=False, fetch=None):
    """Fetch (or read from cache) everything for the 48 hour window around ``now``."""
    timed_out = False

    def refresh_fetch(url):
        nonlocal timed_out
        try:
            return (fetch or http_get)(url)
        except TimeoutError:
            timed_out = True
            raise
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                timed_out = True
            raise

    start = window_start(now)
    fc = empty_forecast(postcode, region)
    region = fc.region_id
    got, ages = {}, []
    for kind, report in REPORTS.items():
        if kind == "pd7day" and not prices:
            continue
        at, parsed, err = cached_report(report, force=force and kind != "generators", fetch=refresh_fetch,
                                        cache_only=timed_out)
        got[kind] = parsed
        if parsed is not None and kind not in ("generators", "pd7day"):
            ages.append(at)
        if err:
            fc.errors.append("%s: %s" % (ERROR_NAMES[kind], err))
    fc.fetched = min(ages) if ages else 0.0
    units, scada = got["generators"], got["scada"]
    fleet_now = fleet(units, scada, region) if units and scada else None
    pd = (got["predispatch"] or {}).get(region, {})
    # half hours before the window only tell charged_intensity what batteries charged with
    history = {t: v for t, v in pd.items() if t < start}
    pd = {t: v for t, v in pd.items() if t >= start}
    st = (got["stpasa"] or {}).get(region, {})
    roof = (got["rooftop"] or {}).get(region, {})
    p7 = (got.get("pd7day") or {}).get(region, {})
    if not prices:
        pd = {t: [None] + v[1:] for t, v in pd.items()}
    fc.slots = build_slots(start, pd, st, p7, roof, fleet_now, history=history)
    return fc
