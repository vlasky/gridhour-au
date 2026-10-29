"""Untrusted input: NEMWeb listings and reports, redirects, the cache, the config file, terminal output."""
import io
import json
import os
import socket
import stat
import subprocess
import sys
import time
import zipfile
from threading import BoundedSemaphore, Event

import pytest
from conftest import FX, NOW, fake_fetch, fixture_bytes, listing

from gridhour_au import cli, grid, output, state
from gridhour_au.canvas import Canvas
from gridhour_au.compose import compose
from gridhour_au.grid import http_get as REAL_HTTP_GET  # bound before conftest blocks the network
from gridhour_au.safe import clean, label
from gridhour_au.themes import C, apply_theme

PREDISPATCH = "PUBLIC_PREDISPATCHIS_202610091600_20261009153245.zip"
LISTING_URL = grid.CURRENT + "Dispatch_SCADA/"


def zipped(text, name="PUBLIC_X.CSV"):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, text)
    return buf.getvalue()


def predispatch_text():
    z = zipfile.ZipFile(io.BytesIO(fixture_bytes(PREDISPATCH)))
    return z.read(z.namelist()[0]).decode()


def edit_row(text, table, region, period, **fields):
    """The predispatch fixture with one row of ``table`` changed, by column name."""
    lines = text.splitlines(True)
    cols = next(ln.rstrip("\r\n").split(",")[4:] for ln in lines if ln.startswith("I,PREDISPATCH,%s," % table))
    out = []
    for ln in lines:
        f = ln.rstrip("\r\n").split(",")
        if ln.startswith("D,PREDISPATCH,%s," % table) and f[6] == region and f[7] == period:
            for k, v in fields.items():
                f[4 + cols.index(k)] = v
            ln = ",".join(f) + "\n"
        out.append(ln)
    return "".join(out)


def serving(name, body):
    """fake_fetch, but the file ``name`` is ``body``."""
    def fetch(url):
        return body if url.endswith("/" + name) else fake_fetch(url)
    return fetch


# ---------------------------------------------------------------- what reaches the terminal

def test_clean_strips_control_characters():
    assert clean("ok\x1b]52;c;bad\x07\x9b31m\x7f\ud800") == "ok?]52;c;bad??31m??"


def test_label_drops_bidi_zero_width_and_wide():
    assert label("Syd‮ney​́ 東京") == "Syd?ney?? ??"
    assert label(42) is None and label("x" * 100, 5) == "xxxxx"


def test_region_name_with_escapes_never_reaches_the_terminal(st):
    st.fc.region = "Sydney\x1b]0;pwned\x07"
    st.fc.errors = ["network: \x1b[2J"]
    joined = "".join(compose(120, 40, st, NOW, NOW).lines())
    assert "\x1b]0;" not in joined and "\x07" not in joined and "\x1b[2J" not in joined


def test_canvas_put_cleans():
    cv = Canvas(10, 1)
    cv.put(0, 0, "a\x1bb")
    assert cv.text() == ["a?b       "]


def test_title_bar_has_no_double_width_glyph(st):
    import unicodedata
    for line in compose(120, 40, st, NOW, NOW).text():
        assert not any(unicodedata.east_asian_width(c) in "WF" for c in line), line


def test_tmux_output_cannot_inject_formats_or_commands(st):
    st.fc.slots = []
    st.fc.errors = ["oops #(touch /tmp/x) #[fg=red]"]
    out = output.line_output(st, NOW, "tmux")
    assert "#" not in out.replace("##", "")        # every '#' is an escaped literal
    assert "##(touch" in out and "##[fg=red]" in out


def test_line_output_cleans_errors(st):
    st.fc.slots = []
    st.fc.errors = ["\x1b[31mevil‮"]
    out = output.line_output(st, NOW)
    assert "\x1b" not in out and "‮" not in out


def test_json_is_ascii(st):
    st.fc.region = "x\x9b‮"
    out = output.json_output(st, NOW)
    assert out.isascii()


# ---------------------------------------------------------------- the network

class FakeResponse:
    def __init__(self, body, url=LISTING_URL, delay=0.0):
        self.body, self.url, self.delay = body, url, delay

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def geturl(self):
        return self.url

    def read(self, n):
        time.sleep(self.delay)
        chunk, self.body = self.body[:n], self.body[n:]
        return chunk


def test_oversized_response_is_refused(monkeypatch):
    monkeypatch.setattr(grid._OPENER, "open", lambda req, timeout: FakeResponse(b"x" * (grid.MAX_BYTES + 10)))
    with pytest.raises(ValueError, match="larger than"):
        REAL_HTTP_GET(LISTING_URL)


def test_response_from_another_host_is_refused(monkeypatch):
    monkeypatch.setattr(grid._OPENER, "open", lambda req, timeout: FakeResponse(b"{}", "http://evil.example/"))
    with pytest.raises(ValueError, match="somewhere else"):
        REAL_HTTP_GET(LISTING_URL)


def test_slow_drip_hits_the_deadline(monkeypatch):
    monkeypatch.setattr(grid, "DEADLINE", 0.05)
    monkeypatch.setattr(grid._OPENER, "open", lambda req, timeout: FakeResponse(b"x" * 10_000_000, delay=0.03))
    with pytest.raises(TimeoutError):
        REAL_HTTP_GET(LISTING_URL)


def test_redirects_are_refused():
    import urllib.request
    handler = grid._RefuseRedirects()
    req = urllib.request.Request(LISTING_URL)
    with pytest.raises(grid.urllib.error.HTTPError, match="redirect refused"):
        handler.redirect_request(req, None, 302, "Found", {}, "http://evil.example/")
    assert any(isinstance(h, grid._RefuseRedirects) for h in grid._OPENER.handlers)
    assert not any(type(h) is urllib.request.HTTPRedirectHandler for h in grid._OPENER.handlers)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://nemweb.com.au/Reports/CURRENT/", "https://evil.example/",
                                 "https://nemweb.com.au.evil.example/Reports/", "https://nemweb.com.au/other/",
                                 None, 42])
def test_only_nemweb_reports_over_https(url):
    with pytest.raises(ValueError, match="refusing"):
        REAL_HTTP_GET(url)


# ---------------------------------------------------------------- hostile listings

@pytest.mark.parametrize("href", [
    "https://evil.example/PUBLIC_DISPATCHSCADA_202610091550_0000000541993115.zip",
    "/Reports/CURRENT/Dispatch_SCADA/../../evil/PUBLIC_DISPATCHSCADA_202610091550_0000000541993115.zip",
    "/Reports/CURRENT/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_202610091550_0000000541993115.zip?x=1",
    "/Reports/CURRENT/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_٢٠٢٦١٠٠٩١٥٥٠_0000000541993115.zip",
    "/Reports/CURRENT/Dispatch_SCADA/PUBLIC_DISPATCHSCADA_202610091550_0000000541993115.zip\n",
])
def test_listing_links_outside_the_expected_names_are_ignored(href):
    page = ('<A HREF="%s">x</A>' % href).encode()
    with pytest.raises(ValueError, match="no Dispatch_SCADA report"):
        grid.newest(page, grid.REPORTS["scada"])


@pytest.mark.parametrize("page", [b"", b"\xff\xfe\x00", b"<html>" * 100000, "東京".encode(), None])
def test_garbage_listing_is_an_error_not_a_crash(page):
    fc = grid.load(NOW, "2000", fetch=lambda url: page if url.endswith("/") else fake_fetch(url))
    assert fc.errors and len(fc.slots) == 96 and all(s.carbon is None for s in fc.slots)


# ---------------------------------------------------------------- hostile reports

@pytest.mark.parametrize("body", [b"", b"not a zip", b"PK\x03\x04junk", zipped("x", "a.txt"), b"<html>" * 1000,
                                  zipped("I,PREDISPATCH,REGION_SOLUTION,9,REGIONID\n"),
                                  zipped("D,PREDISPATCH,REGION_SOLUTION,9,NSW1,01\n" * 50)])
def test_garbage_report_is_an_error_not_a_crash(body):
    fc = grid.load(NOW, "2000", fetch=serving(PREDISPATCH, body))
    assert fc.errors and fc.errors[0].startswith("forecast:") and len(fc.slots) == 96


@pytest.mark.parametrize("names", [("A.CSV", "B.CSV"), ("A.CSV", "readme.txt"), ("dir/", "A.CSV"), ("a.txt",)])
def test_a_zip_that_is_not_one_csv_is_refused(names):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name in names:
            z.writestr(name, "x")
    with pytest.raises(ValueError, match="not one report"):
        list(grid.csv_lines(buf.getvalue()))


def test_a_zip_bomb_stops_at_the_size_cap(monkeypatch):
    monkeypatch.setattr(grid, "MAX_TEXT", 1024 * 1024)
    bomb = zipped(("D,PREDISPATCH,CONSTRAINT_SOLUTION,5," + "0" * 200 + "\n") * 20000)
    assert len(bomb) < 100_000
    with pytest.raises(ValueError, match="larger than a report"):
        list(grid.csv_lines(bomb))


def test_one_endless_line_is_refused():
    with pytest.raises(ValueError, match="larger than a report"):
        list(grid.csv_lines(zipped("D," + "x" * (grid.MAX_LINE + 10))))


def test_rows_beyond_the_cap_are_not_read(monkeypatch):
    monkeypatch.setattr(grid, "MAX_ROWS", 10)
    text = "I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE\n" + "".join(
        'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",U%d,1\n' % i for i in range(1000))
    assert len(grid.extract_scada(grid.csv_lines(text.encode()))["mw"]) == 10


@pytest.mark.parametrize("value", ["1e400", "1e99", "nan", "inf", "-inf", "99999999", "", "0x10", "1_000", " 5",
                                   "5\x1b[2J", "٥"])
def test_a_bad_price_costs_one_half_hours_price(value):
    text = edit_row(predispatch_text(), "REGION_PRICES", "NSW1", "01", RRP=value)
    fc = grid.load(NOW, "2000", fetch=serving(PREDISPATCH, zipped(text)))
    assert not fc.errors
    assert fc.at(NOW).carbon == 247                     # the carbon estimate doesn't need the price
    assert fc.at(NOW).price == -0.324                   # PD7Day's, not predispatch's 5.315
    assert fc.at(NOW + grid.SLOT).carbon is not None and fc.at(NOW + grid.SLOT).price == 6.402


def test_a_bad_price_keeps_the_carbon_without_prices():
    text = edit_row(predispatch_text(), "REGION_PRICES", "NSW1", "01", RRP="nan")
    fc = grid.load(NOW, "2000", prices=False, fetch=serving(PREDISPATCH, zipped(text)))
    assert fc.at(NOW).carbon == 247 and not fc.has_prices


@pytest.mark.parametrize("fields", [{"REGIONID": "NSW1\x1b[2J"}, {"REGIONID": "WA1"},
                                    {"DATETIME": "2026/10/09 16:00:00\n"}, {"DATETIME": "tomorrow"},
                                    {"DATETIME": "2026/13/45 16:00:00"}, {"INTERVENTION": "1"},
                                    {"DISPATCHABLEGENERATION": "-1e9"}, {"BDU_CLEAREDMW_GEN": "nan"},
                                    {"BDU_CLEAREDMW_GEN": "-400"}, {"BDU_CLEAREDMW_LOAD": "nan"},
                                    {"BDU_CLEAREDMW_LOAD": "-400"}])
def test_bad_row_fields_cost_that_row(fields):
    text = edit_row(predispatch_text(), "REGION_SOLUTION", "NSW1", "01", **fields)
    fc = grid.load(NOW, "2000", fetch=serving(PREDISPATCH, zipped(text)))
    assert not fc.errors and fc.at(NOW).carbon is None and fc.at(NOW + grid.SLOT).carbon is not None


@pytest.mark.parametrize("duid", ["A" * 21, "U\x1b", "", "東京", "U 1", "U\\1", 'U"1'])
def test_hostile_unit_ids_are_ignored(duid):
    text = ('I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE\n'
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",%s,100\n'
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",BW01,500\n' % duid)
    assert grid.extract_scada(grid.csv_lines(text.encode()))["mw"] == {"BW01": 500.0}


def test_an_absurd_emissions_factor_is_dropped():
    text = ("I,CO2EII,PUBLISHING,1,STATIONNAME,DUID,GENSETID,REGIONID,CO2E_EMISSIONS_FACTOR,CO2E_ENERGY_SOURCE\n"
            'D,CO2EII,PUBLISHING,1,"X",BAD,BAD,NSW1,99,"Black coal"\n'
            'D,CO2EII,PUBLISHING,1,"Y",OK,OK,NSW1,0.9,"Black coal"\n')
    assert list(grid.extract_generators(grid.csv_lines(text.encode()))["units"]) == ["OK"]


def test_error_messages_never_repeat_report_content():
    body = zipped("<script>\x1b[31mSECRET-IN-BODY</script>")
    fc = grid.load(NOW, "2000", fetch=serving(PREDISPATCH, body))
    assert fc.errors and not any("SECRET" in e or "\x1b" in e for e in fc.errors)


# ---------------------------------------------------------------- the cache

def cache_file(key="nem-predispatch"):
    return os.path.join(grid.cache_dir(), key + ".json")


def test_a_broken_report_is_never_cached():
    grid.load(NOW, "2000", fetch=serving(PREDISPATCH, b"poison"))
    assert not os.path.exists(cache_file())
    assert os.path.exists(cache_file("nem-stpasa"))


def test_the_cache_holds_numbers_not_the_report():
    grid.load(NOW, "2000", fetch=fake_fetch)
    assert os.path.getsize(cache_file()) < 100_000          # the report is 12 MB unzipped
    box = json.load(open(cache_file()))
    assert box["file"] == PREDISPATCH and set(box["doc"]["rows"]) == set(grid.REGIONS)


@pytest.mark.parametrize("doc", [{"rows": {"NSW1": {"2026-10-09T05:30Z": [1e999, 1, 1, 1, 1]}}},
                                 {"rows": {"NSW1": {"x": [1, 1, 1, 1, 1]}}}, {"rows": []}, [], "x",
                                 {"rows": {"EVIL": {"2026-10-09T05:30Z": [1, 1, 1, 1, 1]}}},
                                 {"rows": {"NSW1": {"2026-10-09T05:30Z": [1, None, 1, 1, 1, 0, 0]}}},  # prices only
                                 {"rows": {"NSW1": {"2026-10-09T05:30Z": [1, 1, 1, 1, 1, -400, 0]}}}])
def test_a_poisoned_cache_entry_counts_as_missing(doc):
    os.makedirs(grid.cache_dir(), exist_ok=True)
    with open(cache_file(), "w") as f:
        json.dump({"at": time.time(), "file": PREDISPATCH, "doc": doc}, f)
    fc = grid.load(NOW, "2000", fetch=fake_fetch)
    assert not fc.errors and fc.at(NOW).carbon == 247


def test_a_cache_stamped_in_the_future_expires():
    grid.load(NOW, "2000", fetch=fake_fetch)
    box = json.load(open(cache_file()))
    box["at"] = 9e18
    json.dump(box, open(cache_file(), "w"))
    fetched = []
    grid.load(NOW, "2000", fetch=lambda url: fetched.append(url) or fake_fetch(url))
    assert any("PredispatchIS" in u for u in fetched)


def test_a_planted_symlink_is_not_followed(tmp_path):
    os.makedirs(grid.cache_dir(), exist_ok=True)
    victim = tmp_path / "victim"
    victim.write_text("keep me")
    for name in ("nem-predispatch.tmp", ".tmp-nem-predispatch.json"):
        os.symlink(victim, os.path.join(grid.cache_dir(), name))
    grid.load(NOW, "2000", fetch=fake_fetch)
    assert victim.read_text() == "keep me"
    assert not os.path.islink(cache_file())


def test_existing_loose_permissions_are_tightened(st):
    os.makedirs(grid.cache_dir(), mode=0o755, exist_ok=True)
    os.chmod(grid.cache_dir(), 0o755)
    with open(cache_file(), "w") as f:
        f.write("{}")
    os.chmod(cache_file(), 0o644)
    grid.load(NOW, "2000", force=True, fetch=fake_fetch)
    assert stat.S_IMODE(os.stat(grid.cache_dir()).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(cache_file()).st_mode) == 0o600


def test_config_and_cache_are_private(st):
    st.persist = True
    st.save()
    assert stat.S_IMODE(os.stat(state.config_path()).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(state.config_path())).st_mode) == 0o700
    grid.load(NOW, "2000", fetch=fake_fetch)
    for name in os.listdir(grid.cache_dir()):
        assert stat.S_IMODE(os.stat(os.path.join(grid.cache_dir(), name)).st_mode) == 0o600


# ---------------------------------------------------------------- the config file and arguments

@pytest.mark.parametrize("cfg", ['[1, 2]', '"x"', '{"postcode": "../../etc", "region": "../NSW1"}',
                                 '{"postcode": "6000", "region": 99}', '{"postcode": 2000, "region": ["NSW1"]}',
                                 '{"jobs": [{"name": "x", "minutes": 999999}]}', 'not json',
                                 '{"theme": [], "region": [], "mode": [], "compact": {}}',
                                 '{"jobs": [{"name": "x", "minutes": 1e400}], "region": true}',
                                 '{"jobs": [{"name": "\\u202e\\u001b[2J", "minutes": 60}]}',
                                 '{"postcode": "2\\u0660\\u0660\\u0660", "region": "NSW1\\u001b"}',
                                 "[" * 50000 + "]" * 50000])
def test_hostile_config_is_survivable(cfg):
    path = state.config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(cfg)
    st = state.state_from_config()
    assert st.postcode is None and st.region_id is None
    assert all(30 <= j.minutes <= 24 * 60 and j.name.isprintable() and "‮" not in j.name for j in st.jobs)
    assert C.name == "carbon"


def test_apply_theme_with_junk():
    apply_theme([])
    assert C.name == "carbon"


@pytest.mark.parametrize("text", ["٢٠٠٠", "２０００", "2٠٠٠", "2000\n0", "+2000", "-200"])
def test_postcodes_are_ascii_digits_only(text):
    with pytest.raises(ValueError):
        grid.normalize_postcode(text)


@pytest.mark.parametrize("args,msg", [(["--size", "abc", "--once"], "--size"),
                                      (["--size", "99999x99999", "--once"], "--size"),
                                      (["--at", "0001-01-01T00:00Z", "--line"], "2000"),
                                      (["--at", "yesterday", "--line"], "not a time"),
                                      (["--region", "\x1b[2J", "--line"], "unknown region")])
def test_bad_arguments_exit_cleanly(args, msg):
    with pytest.raises(SystemExit, match=msg) as e:
        cli.main(args)
    assert "\x1b" not in str(e.value)


def test_at_without_offset_is_utc():
    assert grid.parse_time("2026-07-01T12:00") == grid.parse_time("2026-07-01T12:00Z")


def test_reset_removes_the_cache(capsys):
    grid.load(NOW, "2000", fetch=fake_fetch)
    assert any(n.startswith("nem-") for n in os.listdir(grid.cache_dir()))
    cli.main(["--reset"])
    assert not any(n.startswith("nem-") for n in os.listdir(grid.cache_dir()))


@pytest.mark.parametrize("phase", ["dns", "body"])
def test_blocked_request_returns_at_deadline(monkeypatch, phase):
    release = Event()
    entered = Event()
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(grid, "_REQUEST_SLOTS", slots)
    monkeypatch.setattr(grid, "DEADLINE", 0.05)

    def blocked_dns(*args):
        entered.set()
        assert release.wait(2)
        return []

    class Response(FakeResponse):
        def read(self, n):
            if phase == "body":
                entered.set()
                assert release.wait(2)
            return super().read(n)

    def open_response(req, timeout):
        if phase == "dns":
            socket.getaddrinfo("nemweb.com.au", 443)
        return Response(b"{}")

    monkeypatch.setattr(socket, "getaddrinfo", blocked_dns)
    monkeypatch.setattr(grid._OPENER, "open", open_response)
    start = time.monotonic()
    try:
        with pytest.raises(TimeoutError, match="took longer"):
            REAL_HTTP_GET(LISTING_URL)
        assert entered.is_set()
        assert time.monotonic() - start < 0.5
    finally:
        release.set()
        assert slots.acquire(timeout=1)
        slots.release()


def test_stalled_requests_are_capped_and_capacity_recovers(monkeypatch):
    release = Event()
    started = []
    slots = BoundedSemaphore(2)
    monkeypatch.setattr(grid, "_REQUEST_SLOTS", slots)
    monkeypatch.setattr(grid, "DEADLINE", 0.05)

    def open_response(req, timeout):
        started.append(req.full_url)
        assert release.wait(2)
        return FakeResponse(b"{}")

    monkeypatch.setattr(grid._OPENER, "open", open_response)
    try:
        for _ in range(4):
            with pytest.raises(TimeoutError):
                REAL_HTTP_GET(LISTING_URL)
        assert len(started) == 2
    finally:
        release.set()
        assert slots.acquire(timeout=1)
        assert slots.acquire(timeout=1)
        slots.release()
        slots.release()
    assert REAL_HTTP_GET(LISTING_URL) == b"{}"


def test_dns_timeout_returns_stale_cache_without_replacing_it(monkeypatch):
    release = Event()
    slots = BoundedSemaphore(1)
    monkeypatch.setattr(grid, "_REQUEST_SLOTS", slots)
    monkeypatch.setattr(grid, "DEADLINE", 0.05)
    report = grid.REPORTS["scada"]
    doc = {"mw": {"BW01": 500.0}}
    grid._write_cache(report.key, "PUBLIC_DISPATCHSCADA_202610091550_0000000541993115.zip", doc)

    def open_response(req, timeout):
        assert release.wait(2)
        return FakeResponse(b"{}")

    monkeypatch.setattr(grid._OPENER, "open", open_response)
    try:
        at, parsed, error = grid.cached_report(report, force=True, fetch=REAL_HTTP_GET)
        assert at is not None
        assert parsed == {"BW01": 500.0}
        assert "network: took longer" in error
    finally:
        release.set()
        assert slots.acquire(timeout=1)
        slots.release()
    assert grid._read_cache(report.key)[2] == doc


def test_stalled_dns_does_not_hold_process_open():
    script = """
import socket
import time
from gridhour_au import grid

def slow_dns(*args):
    time.sleep(60)

def open_response(req, timeout):
    socket.getaddrinfo("nemweb.com.au", 443)

socket.getaddrinfo = slow_dns
grid._OPENER.open = open_response
grid.DEADLINE = 0.05
try:
    grid.http_get(grid.CURRENT + "Dispatch_SCADA/")
except TimeoutError:
    print("deadline reached")
"""
    # The interpreter and inline program are fixed test inputs, never user-provided commands.
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=2,  # noqa: S603
                            check=True)
    assert result.stdout.strip() == "deadline reached"


def test_conftest_listings_read_like_the_real_one():
    """fake_fetch builds listings in the shape of fixtures/listing_dispatch_scada.html; keep them in step."""
    report = grid.REPORTS["scada"]
    with open(os.path.join(FX, "listing_dispatch_scada.html"), "rb") as f:
        real = grid.newest(f.read(), report)
    assert grid.newest(listing("Dispatch_SCADA", [real]), report) == real


def test_a_row_cut_short_is_dropped_not_read_as_zeros():
    text = ("I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE\n"
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",BW01\n'
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",ER01,300,extra\n'
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",MP1,200\n')
    assert grid.extract_scada(grid.csv_lines(text.encode()))["mw"] == {"MP1": 200.0}


def test_a_line_the_csv_reader_rejects_costs_that_line():
    text = ("I,DISPATCH,UNIT_SCADA,1,SETTLEMENTDATE,DUID,SCADAVALUE\n"
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",ER01,3\r00\n'
            'D,DISPATCH,UNIT_SCADA,1,"2026/10/09 15:50:00",MP1,200\n')
    assert grid.extract_scada(grid.csv_lines(zipped(text)))["mw"] == {"MP1": 200.0}


def test_a_report_the_csv_reader_rejects_is_not_a_crash():
    body = zipped(predispatch_text().replace(",NSW1,01,0,53.15,", ",NSW1,01,0,53\r15,", 1))
    fc = grid.load(NOW, "2000", fetch=serving(PREDISPATCH, body))
    assert not fc.errors and fc.at(NOW).carbon == 247 and fc.at(NOW).price == -0.324     # only the price row went
