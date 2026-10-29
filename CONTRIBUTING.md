# Contributing

Bug reports, ideas and pull requests are welcome. For anything bigger than a small fix, open an
issue first so we can agree on the shape before you write the code.

## Setup

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install --require-hashes -r requirements/dev.txt
pip install --no-deps -e .
pre-commit install          # ruff, mdformat and a secret scan before each commit (first run downloads Go for gitleaks)
```

## Checks

```sh
pytest                      # a few seconds; sockets are blocked, nothing reaches the network
ruff check .
coverage run -m pytest && coverage report     # CI fails below 80%
python -m gridhour_au --once --size 120x40    # one frame of the UI with today's data
pip install --require-hashes -r requirements/docs.txt && python tools/screenshots.py   # docs/*.png
python tools/demo.py                          # docs/demo.gif, the animation at the top of the README
```

The tests replay real NEMWeb reports stored in `tests/fixtures/`, trimmed to the tables the code
reads (the full predispatch and seven-day predispatch (PD7Day) reports are 12 and 50 MB unzipped).
If you need new fixtures, fetch them from `https://nemweb.com.au/Reports/CURRENT/`, drop the
unused tables and commit them; `tests/conftest.py` serves them with a directory listing built in
the shape of `tests/fixtures/listing_dispatch_scada.html`.

If you touch `.github/workflows/`, also run `zizmor --persona=pedantic .github/workflows` and
`actionlint`.

## Dependencies

There are no runtime dependencies and there should not be any. Development tools are pinned with
hashes. To change them, edit `requirements/*.in` and recompile:

```sh
uv pip compile --universal --python-version 3.11 --generate-hashes requirements/dev.in -o requirements/dev.txt
```

Dependabot proposes updates weekly, a week after a release appears upstream.

## House rules

- Text from the network or a file passes through `safe.label` before it is stored, and everything
  drawn goes through `Canvas.put`. No text from outside is written straight to `sys.stdout`.
- New network calls go through `grid.http_get`, which checks the host and refuses redirects. New
  reports are added to `grid.REPORTS` with an exact file name pattern.
- New fields from a report are checked in its `extract_*` function and again in its parser (type,
  range, known values) and get a case in `tests/test_security.py`.
- Match the style around you: `%` formatting, short functions, comments that say why.
- Add a line to `CHANGELOG.md` under "Unreleased" for anything a user would notice.

## Releasing

In a pull request, bump `__version__` in `gridhour_au/__init__.py` and move the "Unreleased"
entries under `## [X.Y.Z] - YYYY-MM-DD`. After it is merged, tag the release commit on `main` as
`vX.Y.Z` and push the tag. The release workflow checks that the tag, `__version__` and the
CHANGELOG agree, publishes to PyPI and creates the GitHub release.
