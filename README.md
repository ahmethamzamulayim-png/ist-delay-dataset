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
18:45 UTC daily (GitHub Actions) — captures TODAY in its late local evening
  OpenSky  /flights/departure + /flights/arrival  airport=LTFM   → actual movements
           (via a tiny Deno Deploy proxy — OpenSky firewalls GitHub runner IPs)
  aviationstack /v1/flights dep_iata=IST, real-time mode         → schedules + actuals
  aviationweather.gov METARs for LTFM                            → raw weather strings
  join on normalized ICAO callsign + closest scheduled time (±3h);
  a fuzzy second pass (airline prefix + far-end airport + time) recovers
  ATC callsigns like THY5KX that never equal the schedule's THY162;
  codeshare duplicate rows are dropped at fetch time
  → data/flights/YYYY-MM-DD.csv  (joined + cancelled)
  → data/schedules/YYYY-MM-DD.json.gz (raw schedules, kept for finalization)

next day ~18:45 UTC: FINALIZE yesterday — OpenSky's flight processing lags
  hours (same-day arrivals are near-empty), so each run re-fetches yesterday's
  movements and re-joins them against the stored schedules, rewriting the files.
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
(`missing_callsign`, `no_schedule_match`, `no_schedule_data`, `no_opensky_match`,
`no_movement_seen`). Cancelled flights go in the **main** CSV with
`status=cancelled` — signal, not noise. `no_movement_seen` (set at finalization)
means a flight was still "scheduled" at collection time and no transponder
movement ever matched it: a cancellation announced after the run, or an ADS-B
coverage gap — undecidable on the free tier, so it's labeled, not guessed.

## Workflow behavior

- Idempotent: per-date files are overwritten wholesale, `metrics.csv` is upserted.
- Partial failure: one source down → the other's rows are still saved and the day
  is annotated in `metrics.csv`. The workflow fails (and GitHub emails you) only
  when **both** flight sources fail.
- No auto-backfill: the free schedule source can't serve past days.
  `--backfill N` exists for manual (OpenSky-only) historical pulls.
- Every run regenerates `summary.json` from all accumulated data.

## Setup

1. **Deploy the OpenSky proxy** ([proxy/opensky-proxy.ts](proxy/opensky-proxy.ts))
   on Deno Deploy (dash.deno.com → new project/playground, paste the file).
   Set its env vars: `OPENSKY_CLIENT_ID`, `OPENSKY_CLIENT_SECRET` (your OpenSky
   API client) and `PROXY_KEY` (any long random string).
2. GitHub secrets: `gh secret set OPENSKY_PROXY_URL` (the `https://….deno.dev`
   URL), `gh secret set OPENSKY_PROXY_KEY` (same value as `PROXY_KEY`), and
   `gh secret set AVIATIONSTACK_KEY`.
3. Settings → Actions → General → Workflow permissions → read and write.
4. Optionally trigger the workflow once manually (Actions → Collect IST flight
   data → Run workflow) instead of waiting for the 18:45 UTC cron.
5. **Precise scheduling (recommended):** GitHub's cron queue fires 30–90+ min
   late. The proxy worker doubles as an on-time scheduler via `Deno.cron` —
   give the Deno project a `GITHUB_TOKEN` env var (fine-grained PAT scoped to
   this repo, Actions read+write), redeploy the current proxy file, then delete
   the `schedule:` block from `collect.yml` so runs aren't doubled.

## Verified the hard way (2026-07-18)

- **OpenSky drops connections from GitHub Actions runner IPs** (pure connect
  timeouts from the runner while the same requests answer in 0.5s from a home
  connection). Collection needs to reach OpenSky from a non-datacenter IP or an
  unblocked proxy.
- **aviationstack free plan rejects `flight_date` queries** with
  `function_access_restricted` — historical day queries need a paid plan; the
  free plan serves real-time flights only.
- **The real-time feed is a rolling ~3-day window, newest-first** (verified
  2026-07-20: `total=4196`, offset 0 = tomorrow's flights, deep offsets =
  yesterday's). Rows are therefore bucketed into schedule stores by their OWN
  scheduled date, and each day's store accumulates across ~3 days of runs
  before finalization reaps it.
- The OpenSky OAuth2 token endpoint below is correct (live `invalid_client`
  response to dummy credentials), and anonymous `/flights/*` access is refused
  with "You cannot access historical flights".

## Assumptions to verify against live docs

- **ASSUMPTION** OpenSky OAuth2 token URL is
  `https://auth.opensky-network.org/auth/realms/opensky-network/protocol/openid-connect/token`
  (client-credentials grant). An anonymous fallback is implemented, but **verified
  2026-07-18: anonymous requests to `/flights/*` return 403** — the OpenSky
  secrets are effectively required.
- **ASSUMPTION** aviationstack free tier ≈ 100 requests/month, `limit=100` max per
  page, pagination via `offset`, HTTP only. Daily budget defaults to **3 requests**
  (`AVIATIONSTACK_DAILY_BUDGET` env var) ≈ 300 of IST's ~700 daily departures,
  departures prioritized. The pages beyond the first are sampled at **random
  offsets** (seeded by date, so re-runs are idempotent) — a fixed head slice
  would bias the dataset toward one part of the day. Raise the budget if your
  plan allows; the paid tiers were judged not worth it for this project.
- **ASSUMPTION** aviationweather.gov `api/data/metar?ids=LTFM&format=json` returns
  `rawOb` + `obsTime`(epoch)/`reportTime`, with roughly ≤4 days of history.
- **VERIFIED 2026-07-20** aviationstack real-time timestamps are Istanbul local
  wall-clock mislabeled `+00:00`. OpenSky's epoch `firstSeen` (true UTC) ran a
  constant −178 min against them = the 3h IST offset. `parse_avs_utc` corrects
  local→UTC (Turkey is UTC+3 year-round, no DST). `delay_minutes` was already
  correct (both times shift equally) but absolute `*_utc` columns and the match
  window were 3h off before this fix.

## Known limitations

- The day is captured live at ~19:00 UTC and **finalized by the next day's run**
  once OpenSky's processing catches up; a day's numbers are provisional for
  ~24h. aviationstack `actual` times for post-run departures stay null, but
  delay still gets computed from OpenSky's movement time at finalization.
- Verified 2026-07-18: OpenSky same-day queries returned 206 departures and 0
  arrivals at 18:21 UTC — the finalize pass exists precisely because of this lag.
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
