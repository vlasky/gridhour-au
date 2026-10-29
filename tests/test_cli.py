import dataclasses
import json
import os
import subprocess
import sys

import pytest
from conftest import fake_fetch

from gridhour_au import cli, grid


@pytest.fixture(autouse=True)
def fixture_network(monkeypatch):
    monkeypatch.setattr(grid, "http_get", lambda url, timeout=12: fake_fetch(url))


AT = ["--at", "2026-10-09T05:50Z"]


def test_line(capsys):
    cli.main(["2000", "--line", *AT])
    assert capsys.readouterr().out.strip() == "⚡ 247g · 5c · green now"


def test_postcode_is_remembered(capsys):
    cli.main(["2000", "--line", *AT])
    capsys.readouterr()
    cli.main(["--json", *AT])
    assert json.loads(capsys.readouterr().out)["region"]["postcode"] == "2000"


def test_region_is_remembered(capsys):
    cli.main(["--region", "vic", "--line", *AT])
    capsys.readouterr()
    cli.main(["--json", *AT])
    assert json.loads(capsys.readouterr().out)["region"] == {"id": "VIC1", "name": "Victoria", "postcode": None}


def test_times_are_in_the_regions_zone(capsys):
    cli.main(["--region", "qld", "--line", *AT])
    assert grid.local(grid.parse_time(AT[1])).tzname() == "AEST"


def test_tmux(capsys):
    cli.main(["2000", "--tmux", *AT])
    assert "#[fg=#" in capsys.readouterr().out


def test_once(capsys):
    cli.main(["2000", "--once", "--size", "100x30", *AT])
    out = capsys.readouterr().out
    assert out.count("\n") == 30 and "\x1b[" in out


@pytest.mark.parametrize("arg,message", [("hello", "not an Australian postcode"), ("6000", "Western Australia"),
                                         ("0800", "Northern Territory")])
def test_bad_postcode(arg, message):
    with pytest.raises(SystemExit, match=message):
        cli.main([arg, "--line"])


def test_bad_region():
    with pytest.raises(SystemExit, match="unknown region"):
        cli.main(["--region", "wa", "--line"])


def test_reset(capsys):
    cli.main(["2000", "--line", *AT])
    cli.main(["--reset"])
    assert "removed" in capsys.readouterr().out
    cli.main(["--reset"])
    assert "nothing to reset" in capsys.readouterr().out


def test_needs_a_terminal():
    with pytest.raises(SystemExit, match="interactive terminal"):
        cli.main([])


def test_ical(capsys):
    cli.main(["2000", "--ical", *AT])
    out = capsys.readouterr().out

    assert out.startswith("BEGIN:VCALENDAR\r\n")
    assert out.endswith("END:VCALENDAR\r\n")
    assert "BEGIN:VEVENT\r\n" in out
    assert "SUMMARY:Washing (gridhour-au)\r\n" in out
    assert "DTSTAMP:20261009T055000Z\r\n" in out
    assert "\n" not in out.replace("\r\n", "")


def test_json_best_uses_ad_hoc_duration_without_saving_it(capsys):
    cli.main(["2000", "--json", *AT])
    before = json.loads(capsys.readouterr().out)
    cli.main(["--json", "--best", "2h", *AT])
    requested = json.loads(capsys.readouterr().out)
    assert requested["best"]["from"] == "2026-10-11T01:00Z" and requested["best"]["to"] == "2026-10-11T03:00Z"
    assert requested["best"]["starts_in_minutes"] == 2590
    assert requested["jobs"] == before["jobs"]
    cli.main(["--json", *AT])
    after = json.loads(capsys.readouterr().out)
    assert "best" not in after and after["jobs"] == before["jobs"]


def test_json_best_without_forecast_is_null(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["2000", "--json", "--best", "2h", "--at", "2030-01-01T00:00Z"])
    assert exc.value.code == 2
    doc = json.loads(capsys.readouterr().out)
    assert doc["best"] is None and doc["slots"] == []


def broken(url, timeout=12):
    raise OSError("down")


@pytest.mark.parametrize("args,check", [
    (["--json"], lambda out: json.loads(out)["slots"] == [] and bool(json.loads(out)["errors"])),
    (["--ical"], lambda out: out.startswith("BEGIN:VCALENDAR\r\n") and "BEGIN:VEVENT" not in out),
    (["--once", "--size", "80x24"], lambda out: out.count("\n") == 24 and "network: down" in out),
])
def test_script_modes_exit_2_without_forecast(monkeypatch, capsys, args, check):
    monkeypatch.setattr(grid, "http_get", broken)
    with pytest.raises(SystemExit) as exc:
        cli.main(["2000", *AT, *args])
    assert exc.value.code == 2
    assert check(capsys.readouterr().out)


@pytest.mark.parametrize("mode", ["--line", "--tmux"])
def test_status_bar_modes_keep_error_text_at_exit_0(monkeypatch, capsys, mode):
    monkeypatch.setattr(grid, "http_get", broken)
    cli.main(["2000", *AT, mode])                   # no SystemExit: the bar keeps showing the message
    assert "network: down" in capsys.readouterr().out


@pytest.mark.parametrize("down,carbon,price", [("Dispatch_SCADA", False, True), ("PD7Day", True, True),
                                               ("PredispatchIS", True, True)])
def test_partial_forecasts_exit_0(monkeypatch, capsys, down, carbon, price):
    def partial(url, timeout=12):
        if down in url:
            raise OSError("down")
        return fake_fetch(url)

    monkeypatch.setattr(grid, "http_get", partial)
    cli.main(["2000", "--json", *AT])
    doc = json.loads(capsys.readouterr().out)
    assert doc["errors"] and doc["slots"]
    assert any(s["carbon"] is not None for s in doc["slots"]) == carbon
    assert any(s["price"] is not None for s in doc["slots"]) == price


def test_a_cached_forecast_exits_0_when_the_refresh_fails(monkeypatch, capsys):
    cli.main(["2000", "--line", *AT])
    capsys.readouterr()
    for kind, r in grid.REPORTS.items():                # every cached report is due for a refresh
        monkeypatch.setitem(grid.REPORTS, kind, dataclasses.replace(r, max_age=-1))
    monkeypatch.setattr(grid, "http_get", broken)
    cli.main(["2000", "--json", *AT])
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["slots"]) == 96 and doc["errors"]


def test_subprocess_offline_empty_cache_exits_2(tmp_path):
    env = dict(os.environ, XDG_CONFIG_HOME=str(tmp_path / "cfg"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               GRIDHOUR_AU_OFFLINE="1")
    proc = subprocess.run([sys.executable, "-m", "gridhour_au", "2000", "--json", *AT], env=env,  # noqa: S603
                          capture_output=True, text=True, check=False)
    assert proc.returncode == 2 and proc.stderr == ""
    doc = json.loads(proc.stdout)
    assert doc["slots"] == [] and doc["errors"][0] == "forecast: offline and nothing cached yet"
