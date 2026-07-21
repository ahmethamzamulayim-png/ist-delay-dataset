"""API fetchers: OpenSky (actual movements), aviationstack (schedules), aviationweather (METARs)."""
import logging
import os
import random
import time
from datetime import date, datetime, timedelta, timezone

import requests

log = logging.getLogger("collector")

OPENSKY_API = "https://opensky-network.org/api"
# ASSUMPTION: OAuth2 client-credentials token endpoint after OpenSky's auth migration.
OPENSKY_TOKEN_URL = ("https://auth.opensky-network.org/auth/realms/opensky-network"
                     "/protocol/openid-connect/token")
AVIATIONSTACK_API = "http://api.aviationstack.com/v1/flights"  # free tier is HTTP-only
METAR_API = "https://aviationweather.gov/api/data/metar"


class RateLimited(Exception):
    pass


def _get(url, *, params=None, headers=None, tries=4):
    """GET with exponential backoff on network errors/5xx. Raises RateLimited on 429,
    gives up immediately on other 4xx (retrying those is pointless), returns None on failure."""
    for attempt in range(tries):
        try:
            # 10s connect timeout: a firewalled host should fail fast, not eat a minute
            r = requests.get(url, params=params, headers=headers, timeout=(10, 60))
            if r.status_code == 429:
                raise RateLimited(url)
            if r.status_code == 404:  # OpenSky uses 404 for "no flights in interval"
                return r
            if 400 <= r.status_code < 500:
                log.warning("GET %s -> HTTP %d, not retrying: %s",
                            url, r.status_code, r.text[:300])
                return None
            r.raise_for_status()
            return r
        except RateLimited:
            raise
        except requests.RequestException as e:
            log.warning("GET %s failed (%s), attempt %d/%d", url, e, attempt + 1, tries)
            time.sleep(2 ** attempt)
    return None


def _opensky_headers():
    cid = os.getenv("OPENSKY_CLIENT_ID")
    secret = os.getenv("OPENSKY_CLIENT_SECRET")
    if not (cid and secret):
        log.info("No OpenSky credentials, using anonymous access")
        return {}
    try:
        r = requests.post(OPENSKY_TOKEN_URL, timeout=60, data={
            "grant_type": "client_credentials",
            "client_id": cid, "client_secret": secret})
        r.raise_for_status()
        return {"Authorization": "Bearer " + r.json()["access_token"]}
    except (requests.RequestException, KeyError, ValueError) as e:
        log.warning("OpenSky token fetch failed (%s), falling back to anonymous", e)
        return {}


def fetch_opensky(day: date):
    """LTFM departures + arrivals for one UTC day.

    Goes through the Deno Deploy proxy when OPENSKY_PROXY_URL is set (OpenSky
    firewalls GitHub's runner IPs; the proxy also handles OAuth). Direct access
    with local OAuth is kept for runs from residential IPs.

    Returns a list of flight dicts (each tagged with direction=dep/arr), which may
    be empty, or None when OpenSky was unreachable for both directions.
    """
    begin = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
    base = os.getenv("OPENSKY_PROXY_URL", "").rstrip("/")
    if base:
        headers = {"x-proxy-key": os.getenv("OPENSKY_PROXY_KEY", "")}
    else:
        base = OPENSKY_API
        headers = _opensky_headers()
    out, failures = [], 0
    for direction, endpoint in (("dep", "departure"), ("arr", "arrival")):
        try:
            r = _get(f"{base}/flights/{endpoint}",
                     params={"airport": "LTFM", "begin": begin, "end": begin + 86400},
                     headers=headers)
        except RateLimited:
            log.warning("OpenSky rate-limited (429), aborting politely")
            r = None
        if r is None:
            failures += 1
            continue
        flights = [] if r.status_code == 404 else r.json()
        for f in flights:
            f["direction"] = direction
        out.extend(flights)
        log.info("OpenSky %s: %d flights", endpoint, len(flights))
    return None if failures == 2 else out


def fetch_aviationstack(day: date):
    """Scheduled/estimated/actual times for IST for one day.

    Departures first — the free-tier quota may not cover both directions.
    Returns None when there is no key or nothing could be fetched at all.
    """
    key = os.getenv("AVIATIONSTACK_KEY")
    if not key:
        log.warning("AVIATIONSTACK_KEY not set, skipping schedule source")
        return None
    # ASSUMPTION: free tier ~100 requests/month, 100 rows/page via limit+offset.
    # 3/day * 31 covers roughly the monthly quota; raise via env if on a paid plan.
    budget = int(os.getenv("AVIATIONSTACK_DAILY_BUDGET", "3"))
    out = []
    for direction, param in (("dep", "dep_iata"), ("arr", "arr_iata")):
        offsets = [0]  # the first page also tells us the feed's total row count
        planned = False
        while offsets and budget > 0:
            params = {"access_key": key, param: "IST", "limit": 100,
                      "offset": offsets.pop(0)}
            # free plan rejects flight_date (function_access_restricted, verified
            # 2026-07-18) — default is real-time mode: query today, filter below
            if os.getenv("AVIATIONSTACK_HISTORICAL"):
                params["flight_date"] = day.isoformat()
            try:
                r = _get(AVIATIONSTACK_API, tries=2, params=params)
            except RateLimited:
                log.warning("aviationstack rate-limited, stopping")
                return out or None
            if r is None:
                return out or None
            budget -= 1
            body = r.json()
            if not isinstance(body, dict) or body.get("error"):
                log.warning("aviationstack error: %s", body)
                return out or None
            raw = body.get("data") or []
            total = (body.get("pagination") or {}).get("total") or 0
            # drop codeshare rows (marketing-carrier duplicates); do NOT filter by
            # flight_date — its day-labeling semantics burned us twice, and the
            # join's ±3h window is the real day-bucketing authority anyway
            page = [a for a in raw if not (a.get("flight") or {}).get("codeshared")]
            for a in page:
                a["_direction"] = direction
            out.extend(page)
            dates = sorted({a.get("flight_date") for a in raw if a.get("flight_date")})
            log.info("aviationstack %s offset=%s: %d raw (%d kept), total=%s, flight_dates=%s",
                     direction, params["offset"], len(raw), len(page), total, dates)
            if not planned:
                planned = True
                # The real-time feed is newest-first spanning ~3 days
                # (tomorrow → today → yesterday). "Today" sits in the MIDDLE, so
                # random sampling can miss it entirely — it did on 2026-07-20,
                # which got 0 of its own rows. Hit the today-zone and yesterday-
                # zone (yesterday has actual times for finalization); offset 0
                # already grabbed tomorrow. A per-DAY seeded jitter within each
                # zone means consecutive days sample different times of day, so
                # coverage spreads across the clock instead of always hitting the
                # congested midday bank (which biased the delay stats high).
                # Mis-landing near a zone edge is harmless: rows bucket by their
                # own scheduled date regardless.
                rng = random.Random(day.toordinal())
                for lo, hi in ((0.38, 0.62), (0.72, 0.92)):  # today, yesterday
                    o = min(int(total * rng.uniform(lo, hi)) // 100 * 100,
                            max(total - 100, 0))
                    if o and o not in offsets:
                        offsets.append(o)
            if not raw:
                break
        log.info("aviationstack %s: %d rows total, %d requests left",
                 direction, len(out), budget)
    return out or None


def fetch_metars(day: date):
    """Raw LTFM METAR strings for one UTC day.

    aviationweather.gov only serves recent observations on this endpoint, so days
    older than ~4 days return []. TAFs are skipped: the API only returns *current*
    TAFs, which are useless for yesterday.
    """
    start = datetime(day.year, day.month, day.day, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    hours_back = (datetime.now(timezone.utc) - start).total_seconds() / 3600
    if hours_back > 96:
        return []
    try:
        r = _get(METAR_API, params={"ids": "LTFM", "format": "json",
                                    "hours": min(int(hours_back) + 1, 96)})
    except RateLimited:
        return []
    if r is None:
        return []
    try:
        obs = r.json()
    except ValueError:
        return []
    out = []
    for m in obs if isinstance(obs, list) else []:
        raw = m.get("rawOb") or m.get("raw_text") or ""
        # ASSUMPTION: obsTime is a unix epoch, reportTime an ISO string (new data API)
        t = m.get("obsTime") or m.get("reportTime")
        dt = None
        if isinstance(t, (int, float)):
            dt = datetime.fromtimestamp(t, tz=timezone.utc)
        elif isinstance(t, str):
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        if raw and dt and start <= dt < end:
            out.append({"time_utc": dt.isoformat(), "raw": raw})
    out.sort(key=lambda m: m["time_utc"])
    log.info("METARs for %s: %d", day, len(out))
    return out
