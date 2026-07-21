"""Idempotent CSV/JSON persistence under data/."""
import csv
import gzip
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .join import COLUMNS, UNMATCHED_COLUMNS

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
FLIGHTS_DIR = DATA / "flights"
UNMATCHED_DIR = DATA / "unmatched"
WEATHER_DIR = DATA / "weather"
SCHEDULES_DIR = DATA / "schedules"
METRICS = DATA / "metrics.csv"


def _write_csv(path, rows, columns):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def write_day(date_str, flights, unmatched, weather):
    """Overwrites the per-date files, so re-running a date never duplicates rows."""
    _write_csv(FLIGHTS_DIR / f"{date_str}.csv", flights, COLUMNS)
    _write_csv(UNMATCHED_DIR / f"{date_str}.csv", unmatched, UNMATCHED_COLUMNS)
    write_weather(date_str, weather)


def write_weather(date_str, weather):
    if weather:
        _write_csv(WEATHER_DIR / f"{date_str}.csv", weather, ["time_utc", "raw"])


def _trim_schedule(a):
    t = {"_direction": a.get("_direction"), "flight_status": a.get("flight_status"),
         "flight": {k: (a.get("flight") or {}).get(k) for k in ("icao", "iata")},
         "airline": {"name": (a.get("airline") or {}).get("name")}}
    for side in ("departure", "arrival"):
        t[side] = {k: (a.get(side) or {}).get(k)
                   for k in ("icao", "scheduled", "estimated", "actual",
                             "delay", "terminal", "gate")}
    return t


def save_schedules(schedules):
    """Bucket trimmed aviationstack rows into data/schedules/<scheduled-date>.json.gz.

    The real-time feed is a rolling ~3-day window (yesterday→tomorrow, verified
    total=4196 on 2026-07-20), so rows are routed to the store of their OWN
    scheduled date rather than the day we happened to ask for — every fetch
    enriches several days, and finalization reaps whatever accumulated. Stores
    merge across runs; later duplicates win (fresher actuals)."""
    buckets = {}
    for a in schedules or []:
        t = _trim_schedule(a)
        side = t["departure"] if t["_direction"] == "dep" else t["arrival"]
        d = (side.get("scheduled") or "")[:10]
        if d:
            buckets.setdefault(d, []).append(t)
    for d, rows in buckets.items():
        merged = {}
        for a in (load_schedules(d) or []) + rows:
            key = (a["flight"].get("icao"), a["_direction"],
                   a["departure"].get("scheduled"), a["arrival"].get("scheduled"))
            merged[key] = a
        SCHEDULES_DIR.mkdir(parents=True, exist_ok=True)
        with gzip.open(SCHEDULES_DIR / f"{d}.json.gz", "wt", encoding="utf-8") as fh:
            json.dump(list(merged.values()), fh)


def load_schedules(date_str):
    path = SCHEDULES_DIR / f"{date_str}.json.gz"
    if not path.exists():
        return None
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def update_metrics(date_str, **fields):
    """Upsert one date row in data/metrics.csv."""
    new = pd.DataFrame([{"date": date_str, **fields}])
    if METRICS.exists():
        old = pd.read_csv(METRICS)
        new = pd.concat([old[old["date"] != date_str], new], ignore_index=True)
    METRICS.parent.mkdir(parents=True, exist_ok=True)
    new.sort_values("date").to_csv(METRICS, index=False)


def build_summary():
    """Regenerate summary.json (for the dashboard) from all accumulated data."""
    files = sorted(FLIGHTS_DIR.glob("*.csv"))
    frames = [pd.read_csv(f) for f in files]
    df = (pd.concat(frames, ignore_index=True) if frames
          else pd.DataFrame(columns=COLUMNS))
    # Punctuality uses aviationstack's GATE-based delay, not our computed
    # scheduled-gate -> airborne number (which bundles in ~15-25min IST taxi and
    # made a punctual airport look 86% late). Verified 2026-07-21: gate delay ran
    # ~20-30min under the computed figure; on-time flights read 1-2min gate delay.
    if "avs_delay_minutes" not in df.columns:
        df["avs_delay_minutes"] = pd.NA
    delay = pd.to_numeric(df["avs_delay_minutes"], errors="coerce")
    known = df[delay.notna()]
    delayed = int((delay >= 15).sum())
    ontime = int((delay.notna() & (delay < 15)).sum())

    daily = []
    for d, g in df.groupby("date"):
        gd = pd.to_numeric(g.loc[g["direction"] == "dep", "avs_delay_minutes"],
                           errors="coerce").dropna()
        daily.append({
            "date": str(d), "flights": int(len(g)),
            "avg_dep_delay": round(float(gd.mean()), 1) if len(gd) else None,
        })

    latest_metar = ""
    wfiles = sorted(WEATHER_DIR.glob("*.csv"))
    if wfiles:
        w = pd.read_csv(wfiles[-1])
        if len(w):
            latest_metar = str(w.iloc[-1]["raw"])

    match_rate = None
    if METRICS.exists():
        m = pd.read_csv(METRICS).sort_values("date")
        if len(m) and "match_rate" in m.columns and pd.notna(m.iloc[-1]["match_rate"]):
            match_rate = float(m.iloc[-1]["match_rate"])

    summary = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "days": len(files),
        "first_date": files[0].stem if files else None,
        "last_date": files[-1].stem if files else None,
        "total_flights": int(len(df)),
        "delay_known": int(len(known)),  # flights with gate-based delay data
        "delayed_15": delayed,
        "ontime": ontime,
        "pct_delayed": round(100 * delayed / len(known), 1) if len(known) else None,
        "pct_ontime": round(100 * ontime / len(known), 1) if len(known) else None,
        "daily": daily,
        "latest_metar": latest_metar,
        "match_rate": match_rate,
    }
    # written twice: data/ is the canonical copy, docs/ is what GitHub Pages serves
    for path in (DATA / "summary.json", ROOT / "docs" / "summary.json"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(summary, ensure_ascii=False, indent=1) + "\n",
                        encoding="utf-8")
    return summary
