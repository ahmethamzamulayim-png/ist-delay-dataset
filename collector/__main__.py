"""CLI: python -m collector [--date YYYY-MM-DD] [--backfill N]"""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

from . import fetchers
from . import io as store
from .join import join_day

log = logging.getLogger("collector")


def collect(day):
    """Collect one UTC day. Returns True if at least one flight source delivered."""
    date_str = day.isoformat()
    log.info("=== Collecting %s ===", date_str)
    opensky = fetchers.fetch_opensky(day)
    schedules = fetchers.fetch_aviationstack(day)
    weather = fetchers.fetch_metars(day)

    if opensky is None and schedules is None:
        store.write_weather(date_str, weather)  # weather is still worth keeping
        store.update_metrics(date_str, rows=0, unmatched=0, opensky_rows=0,
                             avs_rows=0, match_rate=None, notes="both_sources_failed")
        log.error("%s: both flight sources failed", date_str)
        return False

    store.save_schedules(schedules)
    # join against today's merged bucket, not the raw fetch: the fetch spans ~3
    # days and earlier runs' rows for today survive in the store
    schedules = store.load_schedules(date_str) or []
    flights, unmatched = join_day(date_str, opensky, schedules)
    store.write_day(date_str, flights, unmatched, weather)

    matched = sum(1 for r in flights if r["icao24"] and r["scheduled_utc"])
    os_n = len(opensky or [])
    notes = "|".join(n for n, bad in (("opensky_down", opensky is None),
                                      ("no_schedule_data", not schedules),
                                      ("no_weather", not weather)) if bad)
    store.update_metrics(date_str, rows=len(flights), unmatched=len(unmatched),
                         opensky_rows=os_n, avs_rows=len(schedules or []),
                         match_rate=round(matched / os_n, 3) if os_n else None,
                         notes=notes)
    log.info("%s: %d flights, %d unmatched, %d weather obs", date_str,
             len(flights), len(unmatched), len(weather))
    return True


def finalize(day):
    """Re-join a previously collected day against fresh OpenSky data.

    Same-day OpenSky queries miss a lot (their flight processing lags by hours;
    arrivals especially), so the next run re-fetches the day's movements and
    rewrites its files using the schedules stored at collection time.
    """
    date_str = day.isoformat()
    schedules = store.load_schedules(date_str)
    if schedules is None:
        return
    log.info("=== Finalizing %s ===", date_str)
    opensky = fetchers.fetch_opensky(day)
    if opensky is None:
        log.warning("OpenSky unavailable, keeping provisional data for %s", date_str)
        return
    flights, unmatched = join_day(date_str, opensky, schedules, final=True)
    store.write_day(date_str, flights, unmatched, None)  # weather file stays as-is
    matched = sum(1 for r in flights if r["icao24"] and r["scheduled_utc"])
    os_n = len(opensky)
    store.update_metrics(date_str, rows=len(flights), unmatched=len(unmatched),
                         opensky_rows=os_n, avs_rows=len(schedules),
                         match_rate=round(matched / os_n, 3) if os_n else None,
                         notes="finalized")
    log.info("%s finalized: %d flights, %d unmatched", date_str, len(flights), len(unmatched))


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    p = argparse.ArgumentParser(prog="collector")
    p.add_argument("--date", help="UTC day to collect (default: yesterday)")
    p.add_argument("--backfill", type=int, default=0,
                   help="also collect N days before the target day")
    args = p.parse_args()

    # default = today (UTC): the cron runs at 18:45 UTC and the free aviationstack
    # plan only serves real-time (same-day) flights, so the day is collected live.
    # No auto-backfill — past days have no reachable schedule source on this plan.
    target = (datetime.strptime(args.date, "%Y-%m-%d").date() if args.date
              else datetime.now(timezone.utc).date())

    days = [target - timedelta(days=i) for i in range(args.backfill, -1, -1)]
    # don't re-spend API quota on backfill days that already exist
    days = [d for d in days
            if d == target or not (store.FLIGHTS_DIR / f"{d.isoformat()}.csv").exists()]

    # collect FIRST: today's fetch also banks yesterday's rows (the ones with
    # final actual times) into its store — finalize must run after, not before
    ok = [collect(d) for d in days]
    prev = target - timedelta(days=1)
    if prev not in days:
        finalize(prev)
    store.build_summary()
    if not any(ok):
        log.error("No flight source returned data for any requested day")
        sys.exit(1)


if __name__ == "__main__":
    main()
