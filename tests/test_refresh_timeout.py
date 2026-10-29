"""One stalled network request must not multiply a refresh's deadline."""

import time
import urllib.error
from threading import BoundedSemaphore, Event

from conftest import NOW, fake_fetch

from gridhour_au import grid
from gridhour_au.grid import http_get as REAL_HTTP_GET


def test_hanging_opener_uses_one_deadline_and_cached_forecast(monkeypatch):
    grid.load(NOW, "2000", fetch=fake_fetch)
    release = Event()
    calls = []

    def hanging(req, timeout):
        calls.append(req.full_url)
        release.wait(2)
        raise TimeoutError("simulated offline resolver")

    monkeypatch.setattr(grid._OPENER, "open", hanging)
    monkeypatch.setattr(grid, "_REQUEST_SLOTS", BoundedSemaphore(2))
    monkeypatch.setattr(grid, "DEADLINE", 0.1)
    start = time.monotonic()
    try:
        forecast = grid.load(NOW, "2000", force=True, fetch=REAL_HTTP_GET)
        elapsed = time.monotonic() - start
        assert len(calls) == 1
        assert elapsed < 0.5
        assert len(forecast.errors) == 1
        assert forecast.region == "New South Wales"
        assert forecast.has_prices
        assert all(slot.carbon is not None for slot in forecast.slots[5:])
    finally:
        release.set()


def test_listing_timeout_makes_the_rest_cache_only_and_next_refresh_recovers():
    grid.load(NOW, "2000", fetch=fake_fetch)
    calls = []

    def stalled(url):
        calls.append(url)
        raise urllib.error.URLError(TimeoutError("deadline"))

    forecast = grid.load(NOW, "2000", force=True, fetch=stalled)
    assert len(calls) == 1
    assert forecast.has_prices and forecast.slots[10].carbon is not None
    assert len(forecast.errors) == 1 and "deadline" in forecast.errors[0]
    calls.clear()

    def healthy(url):
        calls.append(url)
        return fake_fetch(url)

    recovered = grid.load(NOW, "2000", force=True, fetch=healthy)
    assert len(calls) == 5          # a listing for each report but the daily emissions factors; nothing new to fetch
    assert recovered.errors == []


def test_non_timeout_errors_do_not_disable_other_requests():
    calls = []

    def bad_predispatch(url):
        calls.append(url)
        if "PredispatchIS" in url:
            raise ValueError("invalid shape")
        return fake_fetch(url)

    forecast = grid.load(NOW, "2000", force=True, fetch=bad_predispatch)
    assert len(calls) == 10         # predispatch fails at its listing; the others list and download
    assert forecast.has_prices
    assert len(forecast.errors) == 1 and forecast.errors[0].startswith("forecast:")


def test_timeout_without_cache_keeps_one_error_and_no_fabricated_data():
    calls = []

    def timeout(url):
        calls.append(url)
        raise TimeoutError("deadline")

    forecast = grid.load(NOW, "2000", fetch=timeout)
    assert len(calls) == 1
    assert len(forecast.errors) == 1
    assert not forecast.has_prices
    assert all(slot.carbon is None for slot in forecast.slots)
