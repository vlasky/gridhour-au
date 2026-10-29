# Security policy

## Reporting a problem

Please report security problems privately, not in a public issue. Use GitHub's private
vulnerability reporting:
[report a vulnerability](https://github.com/vlasky/gridhour-au/security/advisories/new).

Only the latest version gets fixes.

## What gridhour-au does that matters for security

**Network.** HTTPS GET requests to `https://nemweb.com.au/Reports/` and nothing else: directory
listings and the report files they link to. Nothing about you (postcode, region, jobs) is sent.

- `grid.http_get` refuses any other scheme, host or path prefix, refuses redirects, and checks the
  final URL.
- A response may be 8 MB at most, and the whole request, DNS lookup included, must finish within
  30 seconds, however slowly it trickles in. Python cannot cancel a stuck resolver, so at most two
  such requests are left running in the background.
- Report URLs are built only from a fixed folder and a file name from the listing that matches
  that report's name pattern exactly (`PUBLIC_PREDISPATCHIS_<12 digits>_<14 digits>.zip` and so
  on). Links to anywhere else are ignored.
- No API keys, no accounts, no telemetry. `GRIDHOUR_AU_OFFLINE=1` turns the network off.

**Untrusted input.** Listings, reports, the cache and the config file are all treated as hostile.

- A zip must hold exactly one file, a CSV. It is unzipped as a stream and refused past 96 MB or a
  line over 64 kB, so a small zip cannot expand without limit. At most 10 000 rows of the wanted
  tables are read.
- Every field is checked before use: numbers must be plain decimals within a plausible range (no
  `nan`, `inf`, hex or other digits), region ids must be the five National Electricity Market
  (NEM) regions, unit ids must match `[A-Za-z0-9_#/-]{1,20}` (`W/HOE#1` is a real one), and times
  must be the Australian Energy Market Operator's (AEMO's) `YYYY/MM/DD HH:MM:SS`. A row with more
  or fewer columns than its header is dropped rather than read with blanks. A bad row costs that
  half hour, not the whole forecast; a bad price row costs only that half hour's price.
- A report is reduced to numbers and checked before it is cached, so a broken one is never stored.
  A cache entry that no longer passes the checks, or carries a timestamp from the future, is
  ignored.
- Text that is shown (job names, error messages) loses control characters, bidirectional-text
  (bidi) and zero-width characters, combining marks and double-width characters, so it can neither
  send escape sequences to your terminal nor shift or reorder the screen. In `--tmux` output every
  `#` is doubled, so no text can become a tmux format or a `#(command)`. `--json` output is pure
  ASCII.
- Error messages never repeat response content.
- Malformed reports, config files and arguments produce an error message, not a crash. The test
  suite in `tests/test_security.py` holds the cases.

**Files.** `~/.config/gridhour-au/config.json` (postcode or region, jobs, theme) and
`~/.cache/gridhour-au/nem-*.json` (checked numbers from the reports). Both directories are kept at
`0700` (tightened if they already exist with looser modes) and the files at `0600`. Writes go to a
fresh temporary file created with `O_EXCL` and are renamed into place, so a planted symlink is
never followed. `gridhour-au --reset` deletes the config and the cache.

**Dependencies.** None at runtime, only the Python standard library.

## Supply chain

- Every GitHub Action is pinned to a full commit SHA. Workflows run with read-only tokens unless a
  job needs more, and never keep the checkout credentials.
- CI installs its tools from hash-locked files (`requirements/*.txt`, `pip --require-hashes`).
