"""Row-level data quality flags. Rows are flagged, never dropped."""
import re

_CALLSIGN = re.compile(r"^[A-Z0-9]{2,8}$")


def quality_flags(row, os_flight=None):
    """Pipe-joined flag string for one output row."""
    flags = []
    if row["callsign_icao"] and not _CALLSIGN.match(row["callsign_icao"]):
        flags.append("garbled_callsign")
    first, last = row["opensky_firstseen_utc"], row["opensky_lastseen_utc"]
    if first and last and last < first:  # ISO strings compare chronologically
        flags.append("impossible_times")
    # more than 6h early or 24h late is almost certainly a bad join or bad data
    if row["delay_minutes"] is not None and not -360 <= row["delay_minutes"] <= 1440:
        flags.append("implausible_delay")
    if os_flight:
        est = (os_flight.get("estDepartureAirport") if row["direction"] == "dep"
               else os_flight.get("estArrivalAirport"))
        if est and est != "LTFM":
            flags.append("airport_mismatch")
    delta = row["data_quality_delta_min"]
    if delta is not None and abs(delta) > 120:
        flags.append("actuals_disagree")
    return "|".join(flags)
