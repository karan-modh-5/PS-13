"""data_gen.py - generate a richer, documented SAMPLE dataset for PS-13.

There is no live feed from Metro/BRTS/AMTS. This script builds a realistic
synthetic feed so the brief has something to chew on. It writes four CSVs
to data/ (or --out):

  ridership.csv  date,system,unit,riders
  weather.csv    date,rain_mm,temp_c
  outages.csv    date,system,unit,asset,hours
  events.csv     date,system,unit,note        (blank unit = whole system)

Assumptions baked in (say these out loud in the demo):
  - 60 days of history so a "same weekday, last 4 weeks" baseline is stable.
  - Weekend ridership = 70% of weekday.
  - Per-unit level tapers with the unit's position in the list (busier at top).
  - Metro grows ~12% across the window, AMTS declines ~5% (city bus erosion).
  - ~4% daily random noise (weather, school terms, small events).
  - The final day ("today") carries a planted story: a rainy morning, two
    outages, three events, and 8 rider movements -- some explained, some not.

Deterministic: rerun and you get the same numbers (fixed seed).
"""
from __future__ import annotations

import argparse
import csv
import os
import random
from datetime import date, timedelta

# ------------------------------------------------------------------ network
METRO = ["Motera Stadium", "Sabarmati", "Akshardham", "Ranip", "Vadaj",
         "Vijay Nagar", "Usmanpura", "Old High Court", "Shahpur", "Gheekanta",
         "Kalupur", "Kankaria East", "Apparel Park", "Amraiwadi", "Rabari Colony",
         "Vastral", "Nirant Cross Road", "Thaltej", "Gurukul Road",
         "Commerce Six Road", "SP Stadium"]
BRTS = ["Iscon Cross Road", "Nehrunagar", "Shivranjani", "Vastrapur",
        "Mansi Circle", "Swarnim Park", "Sarkhej", "Chandkheda", "Naroda", "Bopal"]
AMTS = ["Route 101", "Route 42", "Route 7", "Route 35", "Route 50", "Route 8"]

UNITS = {"Metro": METRO, "BRTS": BRTS, "AMTS": AMTS}
BASE = {"Metro": 14000, "BRTS": 9000, "AMTS": 6000}
TREND = {"Metro": 0.05, "BRTS": 0.02, "AMTS": -0.04}   # fraction change end-to-start
DAYS = 60
NOISE = 0.03
RAIN_FACTOR = 0.95        # the wet final day costs everyone ~5% (kept inside the 10% threshold)

# planted movements on the final day -> multiplicative factor, chosen so the
# demo shows one clean case of each status: likely / possible / unexplained
PLANTED = {
    ("Metro", "Motera Stadium"): 1.42,     # cricket match  -> LIKELY (event)
    ("Metro", "Akshardham"): 0.90,         # gate outage   -> LIKELY (outage + rain)
    ("Metro", "Vastral"): 0.87,            # just the rain -> POSSIBLE (weather only)
    ("Metro", "SP Stadium"): 0.76,         # far below dip -> UNEXPLAINED
    ("BRTS", "Nehrunagar"): 0.78,          # road diversion -> LIKELY (event)
    ("BRTS", "Iscon Cross Road"): 1.13,    # upward, no cause -> UNEXPLAINED
    ("AMTS", "Route 7"): 1.18,             # procession    -> LIKELY (event)
    ("AMTS", "Route 35"): 0.76,            # far below dip -> UNEXPLAINED
}


def generate(days: int = DAYS, seed: int = 7, end: date | None = None) -> dict:
    rnd = random.Random(seed)
    end = end or (date.today() - timedelta(days=1))

    ridership, weather = [], []
    for k in range(days - 1, -1, -1):
        d = end - timedelta(days=k)
        frac = (days - 1 - k) / (days - 1)                 # 0 -> 1 across window
        wk = 0.70 if d.weekday() >= 5 else 1.0
        for system, units in UNITS.items():
            trend = 1.0 + TREND[system] * frac
            for i, unit in enumerate(units):
                level = BASE[system] * (1.25 - 0.15 * (i / max(len(units) - 1, 1)))
                v = level * trend * wk * rnd.gauss(1.0, NOISE)
                if d == end:
                    v *= PLANTED.get((system, unit), RAIN_FACTOR)   # mild rain dips everyone else
                ridership.append((d.isoformat(), system, unit, int(v)))
        # weather: hot and dry, except a wet final day
        if d == end:
            weather.append((d.isoformat(), 30.0, 28.0))
        else:
            weather.append((d.isoformat(), 0.0, round(36 + rnd.gauss(0, 1.5), 1)))

    outages = [
        (end.isoformat(), "Metro", "Akshardham", "Ticket gate G3", 5.0),
        (end.isoformat(), "Metro", "Sabarmati", "Lift L2", 3.0),      # real fault, no ridership impact
    ]
    events = [
        (end.isoformat(), "Metro", "Motera Stadium", "Cricket match at Narendra Modi Stadium (45,000 in)"),
        (end.isoformat(), "BRTS", "Nehrunagar", "Road diversion - SG Highway metro extension work"),
        (end.isoformat(), "AMTS", "Route 7", "Ganesh visarjan procession - corridor diverted"),
    ]
    return dict(ridership=ridership, weather=weather, outages=outages, events=events)


FILES = {
    "ridership": ("date,system,unit,riders", ["date", "system", "unit", "riders"]),
    "weather": ("date,rain_mm,temp_c", ["date", "rain_mm", "temp_c"]),
    "outages": ("date,system,unit,asset,hours", ["date", "system", "unit", "asset", "hours"]),
    "events": ("date,system,unit,note", ["date", "system", "unit", "note"]),
}


def save(data: dict, folder: str) -> None:
    os.makedirs(folder, exist_ok=True)
    for name, rows in data.items():
        header, _ = FILES[name]
        with open(os.path.join(folder, f"{name}.csv"), "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(header.split(","))
            w.writerows(rows)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Write the sample data CSVs.")
    ap.add_argument("--out", default="data", help="output folder (default: data)")
    ap.add_argument("--days", type=int, default=DAYS)
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args()
    d = generate(a.days, a.seed)
    save(d, a.out)
    units = len({(s, u) for _, s, u, _ in d["ridership"]})
    print(f"wrote {a.out}/ : {len(d['ridership'])} ridership rows, {units} units, "
          f"{len(d['weather'])} weather rows, {len(d['outages'])} outages, {len(d['events'])} events")
