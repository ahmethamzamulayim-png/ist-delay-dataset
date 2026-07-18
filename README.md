# ist-delay-dataset

There is no public historical flight delay dataset for Istanbul Airport
(IST / LTFM). This repo builds one: a GitHub Actions cron job collects
yesterday's flights every morning, joins actual movements (OpenSky) with
schedules (aviationstack) and archives METARs, then commits the CSVs back
to the repo. A [status dashboard](docs/) (GitHub Pages) shows progress.

**Phase 1 (this repo):** reliable zero-maintenance collection.
**Phase 2 (later):** train a delay prediction model on the accumulated data.

## How it works

```
06:17 UTC daily (GitHub Actions)
  OpenSky  /flights/departure + /flights/arrival  airport=LTFM   → actual movements
  aviationstack /v1/flights dep_iata=IST (arr if quota allows)   → schedules + actuals
  aviationweather.gov METARs for LTFM                            → raw weather strings
  join on normalized ICAO callsign + closest scheduled time (±3h)
  → data/flights/YYYY-MM-DD.csv  (joined + cancelled)
  → data/unmatched/YYYY-MM-DD.csv (never discarded, tagged with a reason)
  → data/weather/YYYY-MM-DD.csv
  → data/metrics.csv, data/summary.json (+ copy in docs/ for Pages)
  commit + push
```

Local run: `pip install -r requirements.txt && python -m collector --date 2026-07-17`
(secrets via env vars: `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET`,
`AVIATIONSTACK_KEY`; everything degrades gracefully without them).
Self-check: `python test_join.py`. Quick stats: `python analysis/explore.py`.

## Flights CSV schema

| column | meaning |
|---|---|
| `date` | UTC collection day |
| `direction` | `dep` or `arr` (relative to IST) |
| `callsign_icao` | normalized ICAO callsign (join key) |
| `flight_iata`, `airline` | from aviationstack |
| `origin_icao`, `destination_icao` | LTFM on the IST side; other end from aviationstack, falling back to OpenSky estimate |
| `scheduled_utc`, `actual_utc` | the movement **at IST** (departure time for `dep`, arrival time for `arr`), from aviationstack, UTC ISO |
| `opensky_firstseen_utc`, `opensky_lastseen_utc` | OpenSky transponder first/last seen |
| `delay_minutes` | (aviationstack actual, else OpenSky movement) − scheduled |
| `status` | aviationstack flight_status (`landed`, `cancelled`, …) |
| `terminal`, `gate` | IST side |
| `icao24` | airframe hex (from OpenSky) |
| `data_quality_delta_min` | OpenSky movement − aviationstack actual (cross-validation) |
| `quality_flags` | pipe-joined: `garbled_callsign`, `impossible_times`, `implausible_delay`, `airport_mismatch`, `actuals_disagree` |

Schema note vs. the original sketch: `scheduled_dep_utc`/`actual_dep_utc` became
direction-relative `scheduled_utc`/`actual_utc` so one column pair serves both
directions and `delay_minutes` is always "movement at IST vs. its schedule".

`data/unmatched/` uses the same schema plus `reason`
(`missing_callsign`, `no_schedule_match`, `no_schedule_data`, `no_opensky_match`).
Cancelled flights go in the **main** CSV with `status=cancelled` — signal, not noise.

## Workflow behavior

- Idempotent: per-date files are overwritten wholesale, `metrics.csv` is upserted.
- Partial failure: one source down → the other's rows are still saved and the day
  is annotated in `metrics.csv`. The workflow fails (and GitHub emails you) only
  when **both** flight sources fail.
- Very first run backfills up to 6 extra days (OpenSky reaches a few days back).
- Every run regenerates `summary.json` from all accumulated data.

## Setup

1. `gh secret set OPENSKY_CLIENT_ID`, `gh secret set OPENSKY_CLIENT_SECRET`
   (an OpenSky account's API client), `gh secret set AVIATIONSTACK_KEY`.
2. Settings → Pages → deploy from branch `main`, folder `/docs`.
3. Settings → Actions → General → Workflow permissions → read and write.
4. Optionally trigger the workflow once manually (Actions → Collect IST flight
   data → Run workflow) instead of waiting for the cron.

## Assumptions to verify against live docs

- **ASSUMPTION** OpenSky OAuth2 token URL is
  `https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token`
  (client-credentials grant). An anonymous fallback is implemented, but **verified
  2026-07-18: anonymous requests to `/flights/*` return 403** — the OpenSky
  secrets are effectively required.
- **ASSUMPTION** aviationstack free tier ≈ 100 requests/month, `limit=100` max per
  page, pagination via `offset`, HTTP only. Daily budget defaults to **3 requests**
  (`AVIATIONSTACK_DAILY_BUDGET` env var) — that covers ~300 departures/day of
  IST's ~700, departures prioritized. Raise the budget if your plan allows.
- **ASSUMPTION** aviationweather.gov `api/data/metar?ids=LTFM&format=json` returns
  `rawOb` + `obsTime`(epoch)/`reportTime`, with roughly ≤4 days of history.
- **ASSUMPTION** aviationstack timestamps carry a UTC offset; naive ones are
  treated as UTC.

## Known limitations

- OpenSky has no scheduled times; delays exist only for rows matched to
  aviationstack (or cancelled). Match rate is tracked per day in `metrics.csv`.
- ADS-B coverage gaps over Turkey can shift `firstSeen`/`lastSeen` by minutes —
  `data_quality_delta_min` measures this against aviationstack's actuals.
- aviationstack free quota can't cover all IST traffic; unmatched OpenSky rows
  are still archived and can be re-joined later against any schedule source.
- METARs older than ~4 days can't be backfilled; TAF history isn't available on
  the free endpoint at all.

## Phase 2 roadmap

Feature engineering (hour-of-day, airline, destination, METAR-derived wind/vis/
precip, holiday calendar), then a baseline gradient-boosted model predicting
P(delay ≥ 15 min) for departures; re-join `data/unmatched/` once a fuller
schedule source is available.
