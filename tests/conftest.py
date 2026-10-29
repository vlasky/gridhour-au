import os
import re
import socket
from datetime import UTC, datetime

import pytest

from gridhour_au import grid
from gridhour_au.grid import CURRENT, REPORTS, load
from gridhour_au.state import State
from gridhour_au.themes import apply_theme

FX = os.path.join(os.path.dirname(__file__), "fixtures")
# 15:50 in Brisbane, 16:50 in Sydney: inside the first half hour of the recorded predispatch run
NOW = datetime(2026, 10, 9, 5, 50, tzinfo=UTC)


def fixture_bytes(name):
    with open(os.path.join(FX, name), "rb") as f:
        return f.read()


def listing(folder, names):
    """A NEMWeb directory listing, in the shape of fixtures/listing_dispatch_scada.html."""
    rows = "".join('      Friday, October 9, 2026 03:47 PM         4275 <A HREF="/Reports/CURRENT/%s/%s">%s</A><br>'
                   % (folder, n, n) for n in names)
    return ('<html><body><pre><A HREF="/Reports/CURRENT">[To Parent Directory]</A><br><br>%s</pre></body></html>'
            % rows).encode()


def fake_fetch(url):
    """Replays tests/fixtures: a folder URL gets a listing of the fixtures that belong in it."""
    assert url.startswith(CURRENT), url
    folder, _, name = url[len(CURRENT):].rpartition("/")
    if name:
        return fixture_bytes(name)
    report = next(r for r in REPORTS.values() if r.folder == folder)
    return listing(folder, [n for n in sorted(os.listdir(FX)) if re.fullmatch(report.name, n)])


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """No test touches the real config, cache or network."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("GRIDHOUR_AU_OFFLINE", raising=False)

    def no_network(url, timeout=12):
        raise AssertionError("tests must not hit the network: " + url)

    def no_socket(*args, **kwargs):
        raise AssertionError("tests must not open network connections")

    monkeypatch.setattr("gridhour_au.grid.http_get", no_network)
    # and below that, in case some code path ever reaches the network without http_get
    monkeypatch.setattr(socket.socket, "connect", no_socket)
    monkeypatch.setattr(socket, "create_connection", no_socket)
    monkeypatch.setattr(socket, "getaddrinfo", no_socket)
    apply_theme("carbon")
    grid.set_zone(grid.DEFAULT_REGION)


@pytest.fixture
def fc():
    return load(NOW, "2000", fetch=fake_fetch)


@pytest.fixture
def st(fc):
    s = State()
    s.persist = False
    s.postcode = "2000"
    s.fc = fc
    return s
