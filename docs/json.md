# JSON output: schema 1

`gridhour-au --json` prints one JSON object. Check `schema` before consuming it. Within schema 1,
changes are additive only: existing keys are not removed or renamed, and their types, units and
meanings stay compatible. A breaking change requires a new schema number. Consumers should ignore
unknown keys, accept documented nulls and avoid depending on object key order.

All date-times are UTC ISO 8601 strings ending in `Z`, with minute precision. Carbon intensity is
an estimate in gCO₂e/kWh (see "Where the numbers come from" in the README); prices are the
wholesale spot price in c/kWh, excluding the goods and services tax (GST). A missing measurement
is `null`, never zero: zero and negative prices are real values. `errors` describes refresh
problems; cached or partial data can still be present. `slots` contains only half hours with some
carbon or price data, so it can have gaps and can be empty. The current half hour may be missing
or contain null measurements.

## Top-level fields

| Key | Type | Meaning and nullability |
| -- | -- | -- |
| `schema` | integer | Format version, currently `1`; never null. |
| `at` | string | Evaluation time (including a requested `--at`), not fetch time. |
| `region` | object | Region fields below; never null. |
| `units` | object | `carbon` and `price` units as text, for people reading the output; never null. |
| `mode` | string | Effective ranking mode: `green`, `cheap` or `both`; may fall back when prices are missing. |
| `now` | slot or null | Half hour containing `at`; null when no slot contains it. |
| `green_now` | boolean | Whether the current slot is graded low or very low; false without one. |
| `green_until` | string or null | End of the current continuous green period; null if not green now. |
| `next_green` | object or null | Next/current green slot; null when none exists in the forecast. |
| `jobs` | array of jobs | Saved jobs and their recommended windows; can be empty. |
| `slots` | array of slots | Available half-hour measurements in chronological order; can be empty. |
| `fetched` | string or null | Oldest fetch time of the reports shown (not counting the emissions factors or the seven-day predispatch (PD7Day) prices); null if none was fetched. |
| `errors` | array of strings | Refresh/cache diagnostics; empty when none are reported. |
| `best` | window or null, optional | Present only with `--best DURATION`; null without a complete window. |

## Nested fields

| Object | Key | Type | Meaning and nullability |
| -- | -- | -- | -- |
| region | `id` | string | National Electricity Market (NEM) region: `NSW1` (New South Wales and the Australian Capital Territory), `QLD1` (Queensland), `VIC1` (Victoria), `SA1` (South Australia) or `TAS1` (Tasmania); never null. |
| region | `name` | string | Region display name; never null. |
| region | `postcode` | string or null | Four-digit postcode; null when using a region. |
| slot | `from`, `to` | string | Half-hour start (inclusive) and end (exclusive). |
| slot | `carbon` | integer or null | Estimated carbon intensity in gCO₂e/kWh; null if unavailable. |
| slot | `index` | string or null | Grade of `carbon`: very low (under 100), low (under 250), moderate (under 450), high (under 650) or very high; null if unavailable. |
| slot | `price` | number or null | Spot price in c/kWh excluding GST, to 3 decimal places; null if unavailable. |
| slot | `mix` | object or null | Fuel names mapped to percentages of supply, rooftop solar included; null if unavailable. Fuel keys can vary. |
| next_green | `from` | string | Start of the green half hour. |
| next_green | `in_minutes` | integer | Whole minutes until it starts, clamped to zero for a current slot. |
| job | `name` | string | Saved job name. |
| job | `minutes` | integer | Required running duration in minutes. |
| job | `deadline` | string or null | `HH:MM` deadline in the region's local time; null without one. |
| job | `deadline_at` | string or null | Resolved deadline in UTC; null without one. |
| job | `split` | boolean | Whether the job may run in separate pieces. |
| job | `best` | window or null | Best complete window; null when no complete window is available. |
| job | `if_started_now` | object | Starting-now comparison; each of `carbon` and `price` is a number or null, with the units above. |
| window | `from`, `to` | string | First start and final end, including gaps for split jobs. |
| window | `starts_in_minutes` | integer | Whole minutes to the first start, clamped to zero. |
| window | `carbon`, `price` | number or null | Whole-window averages, rounded to 1/2 decimal places respectively; null when unavailable. |
| window | `parts` | array of objects | Continuous running pieces; each has UTC string `from` and `to`. |

## Example shape

This fixture-derived example (`gridhour-au 2000 --json --best 2h`) shortens `jobs` and `slots` to
one entry each. Its values illustrate the format; they are not live market data. The documented
keys are checked against fixture-backed output in `test_json_documented_shape`.

```json
{
  "schema": 1,
  "at": "2026-10-09T05:50Z",
  "region": {
    "id": "NSW1",
    "name": "New South Wales",
    "postcode": "2000"
  },
  "units": {
    "carbon": "gCO2e/kWh, estimated",
    "price": "c/kWh, wholesale spot, excluding GST"
  },
  "mode": "both",
  "now": {
    "from": "2026-10-09T05:30Z",
    "to": "2026-10-09T06:00Z",
    "carbon": 247,
    "index": "low",
    "price": 5.315,
    "mix": {
      "solar": 31.0,
      "wind": 5.7,
      "rooftop": 33.7,
      "other": 0.5,
      "gas": 0.5,
      "hydro": 1.0,
      "coal": 27.6
    }
  },
  "green_now": true,
  "green_until": "2026-10-09T06:00Z",
  "next_green": {
    "from": "2026-10-09T05:30Z",
    "in_minutes": 0
  },
  "jobs": [
    {
      "name": "Washing",
      "minutes": 120,
      "deadline": null,
      "deadline_at": null,
      "split": false,
      "best": {
        "from": "2026-10-11T01:00Z",
        "to": "2026-10-11T03:00Z",
        "starts_in_minutes": 2590,
        "carbon": 151.5,
        "price": -0.71,
        "parts": [
          {
            "from": "2026-10-11T01:00Z",
            "to": "2026-10-11T03:00Z"
          }
        ]
      },
      "if_started_now": {
        "carbon": 378.0,
        "price": 8.53
      }
    }
  ],
  "slots": [
    {
      "from": "2026-10-09T05:30Z",
      "to": "2026-10-09T06:00Z",
      "carbon": 247,
      "index": "low",
      "price": 5.315,
      "mix": {
        "solar": 31.0,
        "wind": 5.7,
        "rooftop": 33.7,
        "other": 0.5,
        "gas": 0.5,
        "hydro": 1.0,
        "coal": 27.6
      }
    }
  ],
  "fetched": "2026-10-09T05:45Z",
  "errors": [],
  "best": {
    "from": "2026-10-11T01:00Z",
    "to": "2026-10-11T03:00Z",
    "starts_in_minutes": 2590,
    "carbon": 151.5,
    "price": -0.71,
    "parts": [
      {
        "from": "2026-10-11T01:00Z",
        "to": "2026-10-11T03:00Z"
      }
    ]
  }
}
```
