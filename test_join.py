"""Smallest check that fails if the join logic breaks: `python test_join.py`."""
from datetime import datetime, timezone

from collector.join import join_day

# aviationstack times below are Istanbul local wall-clock mislabeled "+00:00"
# (the real-world quirk parse_avs_utc corrects): they read 3h ahead of the true
# UTC that OpenSky's epoch firstSeen/lastSeen encode.
D = "2026-07-17"
TS = int(datetime(2026, 7, 17, 8, 5, tzinfo=timezone.utc).timestamp())

opensky = [
    {"callsign": "THY1KM  ", "icao24": "4bb1c5", "direction": "dep",
     "firstSeen": TS, "lastSeen": TS + 7200,
     "estDepartureAirport": "LTFM", "estArrivalAirport": "EDDF"},
    {"callsign": None, "icao24": "abc123", "direction": "dep",
     "firstSeen": TS, "lastSeen": TS + 100,
     "estDepartureAirport": "LTFM", "estArrivalAirport": None},
]
schedules = [
    {"_direction": "dep", "flight_status": "active",
     "flight": {"icao": "THY1KM", "iata": "TK1617"},
     "airline": {"name": "Turkish Airlines"},
     "departure": {"scheduled": "2026-07-17T10:45:00+00:00",
                   "actual": "2026-07-17T11:03:00+00:00",
                   "terminal": "I", "gate": "A5"},
     "arrival": {"icao": "EDDF"}},
    {"_direction": "dep", "flight_status": "cancelled",
     "flight": {"icao": "PGT44T", "iata": "PC1234"},
     "airline": {"name": "Pegasus"},
     "departure": {"scheduled": "2026-07-17T12:00:00+00:00"},
     "arrival": {"icao": "EDDM"}},
]

# ATC callsign THY7CV != schedule callsign THY2408: only the fuzzy pass
# (airline prefix + destination + time window) can recover this pair
opensky.append(
    {"callsign": "THY7CV", "icao24": "4bb777", "direction": "dep",
     "firstSeen": TS + 7080, "lastSeen": TS + 12000,
     "estDepartureAirport": "LTFM", "estArrivalAirport": "LTAI"})
schedules.append(
    {"_direction": "dep", "flight_status": "active",
     "flight": {"icao": "THY2408", "iata": "TK2408"},
     "airline": {"name": "Turkish Airlines"},
     "departure": {"scheduled": "2026-07-17T12:40:00+00:00",
                   "actual": "2026-07-17T13:02:00+00:00"},
     "arrival": {"icao": "LTAI"}})

flights, unmatched = join_day(D, opensky, schedules)
by_cs = {r["callsign_icao"]: r for r in flights}

assert by_cs["THY1KM"]["delay_minutes"] == 18, by_cs["THY1KM"]
assert by_cs["THY1KM"]["flight_iata"] == "TK1617"
assert by_cs["THY1KM"]["data_quality_delta_min"] == 2
assert by_cs["THY1KM"]["destination_icao"] == "EDDF"
assert by_cs["PGT44T"]["status"] == "cancelled"
assert by_cs["PGT44T"]["delay_minutes"] is None
assert by_cs["THY7CV"]["flight_iata"] == "TK2408", by_cs["THY7CV"]
assert "fuzzy_callsign_match" in by_cs["THY7CV"]["quality_flags"]
assert by_cs["THY7CV"]["delay_minutes"] == 22
assert [r["reason"] for r in unmatched] == ["missing_callsign"], unmatched

# finalize pass: a still-"scheduled" row with no movement all day gets its own
# label (late cancellation or coverage gap), not generic join noise
ghost = {"_direction": "dep", "flight_status": "scheduled",
         "flight": {"icao": "PGT9ZZ", "iata": "PC9999"},
         "airline": {"name": "Pegasus"},
         "departure": {"scheduled": "2026-07-17T22:00:00+00:00"},
         "arrival": {"icao": "LTBS"}}
_, um_final = join_day(D, opensky, schedules + [ghost], final=True)
assert any(r["reason"] == "no_movement_seen" and r["callsign_icao"] == "PGT9ZZ"
           for r in um_final), um_final

# re-join with no schedules: everything lands in unmatched, nothing is lost
f2, u2 = join_day(D, opensky, None)
assert not f2 and {r["reason"] for r in u2} == {"missing_callsign", "no_schedule_data"}

print("join self-check OK")
