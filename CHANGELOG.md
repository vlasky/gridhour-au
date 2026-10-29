# Changelog

All notable changes are listed here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[semantic versioning](https://semver.org/).

## [Unreleased]

## [0.1.0] - 2026-10-29

### Added

- First version: a port of [gridhour](https://github.com/777dimas/gridhour) 0.5.0 to the National
  Electricity Market (NEM). 48 hours of Australian Energy Market Operator (AEMO) spot price
  forecasts and an estimated carbon intensity per NEM region, from public NEMWeb reports, with the
  same jobs, deadlines, split jobs, `--line`, `--tmux`, `--json`, `--ical` and `--watch`.
- Australian postcodes and `--region nsw|vic|qld|sa|tas`; times in the region's local time,
  Adelaide's half-hour offset and Brisbane's lack of daylight saving included.
- Carbon is estimated from AEMO's predispatch forecast and the region's units as they run now.
  Past the end of predispatch, the demand and wind and solar forecasts of AEMO's Short Term
  Projected Assessment of System Adequacy (STPASA) fill the 48 hours, with generation estimated
  from the export pattern predispatch shows (see the README).
- Rooftop solar, grid solar and batteries as separate rows in the generation mix. Battery output
  carries the carbon of the half hours the batteries charged in, over the forecast and the last 24
  hours.
- From gridhour 0.5.0: `--json` has `"schema": 1` and a reference in [docs/json.md](docs/json.md);
  `--json --best 2h` adds a `best` window; `--json`, `--ical` and `--once` exit with status 2 when
  there is no forecast at all.
- From gridhour 0.5.0, adapted: times that happen twice when the clocks go back (the first Sunday
  in April, outside Queensland) say which one they are, "02:30 AEDT" or "02:30 AEST" (daylight or
  standard time), and the time axis marks both clock changes.
- On narrow terminals the time axis keeps its day names, which gridhour 0.5.0 can drop (sent
  upstream as [gridhour#48](https://github.com/777dimas/gridhour/pull/48)).

[0.1.0]: https://github.com/vlasky/gridhour-au/releases/tag/v0.1.0
[unreleased]: https://github.com/vlasky/gridhour-au/compare/v0.1.0...HEAD
