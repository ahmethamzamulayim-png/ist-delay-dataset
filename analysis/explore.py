"""Quick look at collected data: `python analysis/explore.py`"""
from pathlib import Path

import pandas as pd

data = Path(__file__).resolve().parent.parent / "data"
files = sorted((data / "flights").glob("*.csv"))
if not files:
    raise SystemExit("no data collected yet")

df = pd.concat([pd.read_csv(f) for f in files], ignore_index=True)
print(f"{len(files)} days, {len(df)} flights ({df['date'].min()} -> {df['date'].max()})")

known = df[df["delay_minutes"].notna()]
if len(known):
    print(f"delay known for {len(known)} rows | mean {known['delay_minutes'].mean():.1f} min"
          f" | p50 {known['delay_minutes'].median():.0f}"
          f" | p90 {known['delay_minutes'].quantile(.9):.0f}")
    print(f"delayed 15+ min: {(known['delay_minutes'] >= 15).mean():.1%}")

print("\ntop destinations:")
print(df[df["direction"] == "dep"]["destination_icao"].value_counts().head(10).to_string())

dep = known[known["direction"] == "dep"].groupby("airline")["delay_minutes"].agg(["count", "mean"])
dep = dep[dep["count"] >= 20].sort_values("mean", ascending=False)
if len(dep):
    print("\nhighest mean departure delay (min 20 flights):")
    print(dep.head(10).round(1).to_string())

if (data / "metrics.csv").exists():
    print("\nmetrics tail:")
    print(pd.read_csv(data / "metrics.csv").tail(7).to_string(index=False))
