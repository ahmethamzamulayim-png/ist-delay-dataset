"""Join OpenSky movements with aviationstack schedules on callsign + closest scheduled time."""
from datetime import datetime, timedelta, timezone

from .quality import quality_flags

MATCH_WINDOW = timedelta(hours=3)

COLUMNS = [
    "date", "direction", "callsign_icao", "flight_iata", "airline",
    "origin_icao", "destination_icao", "scheduled_utc", "actual_utc",
    "opensky_firstseen_utc", "opensky_lastseen_utc", "delay_minutes", "status",
    "terminal", "gate", "icao24", "data_quality_delta_min", "quality_flags",
]
UNMATCHED_COLUMNS = COLUMNS + ["reason"]


def norm_callsign(cs):
    return (cs or "").strip().upper()


def parse_utc(s):
    """aviationstack ISO timestamp -> aware UTC datetime, or None."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:  # ASSUMPTION: naive aviationstack timestamps are UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _epoch_utc(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None


def _iso(dt):
    return dt.isoformat() if dt else ""


def _minutes(a, b):
    return round((a - b).total_seconds() / 60) if a and b else None


def _sched_side(a):
    """(IST-side schedule dict, other-end airport ICAO) for an aviationstack row."""
    if a["_direction"] == "dep":
        return a.get("departure") or {}, ((a.get("arrival") or {}).get("icao") or "")
    return a.get("arrival") or {}, ((a.get("departure") or {}).get("icao") or "")


def _row(date_str, os_f=None, a=None):
    """Build one schema row from an OpenSky flight, an aviationstack row, or both."""
    os_f = os_f or {}
    direction = os_f.get("direction") or (a or {}).get("_direction") or ""
    first = _epoch_utc(os_f.get("firstSeen"))
    last = _epoch_utc(os_f.get("lastSeen"))
    move = first if direction == "dep" else last  # OpenSky's "actual" at the IST side
    side, other_icao = _sched_side(a) if a else ({}, "")
    sched = parse_utc(side.get("scheduled"))
    actual = parse_utc(side.get("actual"))
    row = {
        "date": date_str,
        "direction": direction,
        "callsign_icao": norm_callsign(os_f.get("callsign")
                                       or ((a or {}).get("flight") or {}).get("icao")),
        "flight_iata": ((a or {}).get("flight") or {}).get("iata") or "",
        "airline": ((a or {}).get("airline") or {}).get("name") or "",
        "origin_icao": "LTFM" if direction == "dep" else (
            other_icao or os_f.get("estDepartureAirport") or ""),
        "destination_icao": "LTFM" if direction == "arr" else (
            other_icao or os_f.get("estArrivalAirport") or ""),
        "scheduled_utc": _iso(sched),
        "actual_utc": _iso(actual),
        "opensky_firstseen_utc": _iso(first),
        "opensky_lastseen_utc": _iso(last),
        "delay_minutes": _minutes(actual or move, sched),
        "status": (a or {}).get("flight_status") or "",
        "terminal": side.get("terminal") or "",
        "gate": side.get("gate") or "",
        "icao24": os_f.get("icao24") or "",
        "data_quality_delta_min": _minutes(move, actual),
        "quality_flags": "",
    }
    row["quality_flags"] = quality_flags(row, os_f or None)
    return row


def join_day(date_str, opensky, schedules):
    """Returns (flights_rows, unmatched_rows). Nothing is ever discarded."""
    opensky = opensky or []
    schedules = schedules or []
    by_cs = {}
    for a in schedules:
        by_cs.setdefault(norm_callsign((a.get("flight") or {}).get("icao")), []).append(a)

    used, flights, unmatched, leftovers = set(), [], [], []

    def nearest(cands, move):
        best, best_gap = None, MATCH_WINDOW
        for a in cands:
            if id(a) in used:
                continue
            sched = parse_utc(_sched_side(a)[0].get("scheduled"))
            if move is None or sched is None:
                continue
            gap = abs(move - sched)
            if gap <= best_gap:
                best, best_gap = a, gap
        return best

    # ponytail: greedy earliest-first matching, not optimal assignment — fine at
    # this scale; revisit only if multi-leg mismatches show up in data_quality_delta
    for f in sorted(opensky, key=lambda f: f.get("firstSeen") or 0):
        cs = norm_callsign(f.get("callsign"))
        if not cs:
            unmatched.append(_row(date_str, os_f=f) | {"reason": "missing_callsign"})
            continue
        move = _epoch_utc(f.get("firstSeen") if f["direction"] == "dep" else f.get("lastSeen"))
        best = nearest([a for a in by_cs.get(cs, [])
                        if a["_direction"] == f["direction"]], move)
        if best is None:
            leftovers.append((f, cs, move))
            continue
        used.add(id(best))
        flights.append(_row(date_str, os_f=f, a=best))

    # second pass: Turkish carriers often fly ATC callsigns (THY5KX) that never
    # equal the schedule's flight number (THY162). Recover those via airline
    # prefix + far-end airport + nearest scheduled time, flagged as fuzzy.
    for f, cs, move in leftovers:
        far = (f.get("estArrivalAirport") if f["direction"] == "dep"
               else f.get("estDepartureAirport"))
        best = None
        if far and cs[:3].isalpha():
            best = nearest([a for a in schedules
                            if a["_direction"] == f["direction"]
                            and _sched_side(a)[1] == far
                            and norm_callsign((a.get("flight") or {}).get("icao"))
                                .startswith(cs[:3])], move)
        if best is None:
            reason = "no_schedule_match" if schedules else "no_schedule_data"
            unmatched.append(_row(date_str, os_f=f) | {"reason": reason})
            continue
        used.add(id(best))
        row = _row(date_str, os_f=f, a=best)
        row["quality_flags"] = "|".join(
            x for x in (row["quality_flags"], "fuzzy_callsign_match") if x)
        flights.append(row)

    for a in schedules:
        if id(a) in used:
            continue
        if a.get("flight_status") == "cancelled":
            flights.append(_row(date_str, a=a))  # cancelled flights are signal, keep them
        else:
            unmatched.append(_row(date_str, a=a) | {"reason": "no_opensky_match"})
    return flights, unmatched
