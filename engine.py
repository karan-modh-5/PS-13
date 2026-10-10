"""engine.py - PS-13 analysis core (standard library only).

Loads the four daily feeds, builds a per-unit baseline, flags exceptions,
and attaches likely causes. Returns plain dicts so both the web server and
the CLI can render them however they like.

Method
------
usual  = mean of the same weekday over the last 4 weeks (falls back to the
         last 7 days for a unit with little history).
flag   when |change| >= PCT_THR AND |z| >= Z_THR   (z = (today-usual)/sd,
         sd floored at 2% of usual so a quiet unit can't trigger on noise).
cause  rule-matched from outages, events/diversions and weather.
       Weather only explains a drop that sits near the system-wide dip, so a
       station that fell for another reason is not blamed on the rain.
status likely   -> an outage or event matches
       possible -> only weather matches
       unexplained -> nothing matches; staff should look, not trust a guess
"""
from __future__ import annotations

import csv
import os
import statistics as st
from datetime import date

PCT_THR, Z_THR = 10.0, 2.0
RAIN_MM, HEAT_C = 10.0, 44.0
SYSTEM_ORDER = ["Metro", "BRTS", "AMTS"]

FEEDS = {
    "ridership": "date,system,unit,riders",
    "weather": "date,rain_mm,temp_c",
    "outages": "date,system,unit,asset,hours",
    "events": "date,system,unit,note",
}


def load(folder: str) -> dict:
    """Read the four CSVs (missing files become empty lists)."""
    out = {}
    for name in FEEDS:
        path = os.path.join(folder, f"{name}.csv")
        rows = []
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8") as f:
                rows = [{k: (v or "") for k, v in r.items()} for r in csv.DictReader(f)]
        out[name] = rows
    return out


def _f(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def analyse(data: dict, pct_thr: float = PCT_THR, z_thr: float = Z_THR) -> dict | None:
    series: dict[tuple, dict] = {}
    for r in data.get("ridership", []):
        series.setdefault((r["system"], r["unit"]), {})[r["date"]] = _f(r["riders"])
    if not series:
        return None

    target = max(d for v in series.values() for d in v)
    wd = date.fromisoformat(target).weekday()

    rows = []
    for (system, unit), v in series.items():
        if target not in v:
            continue
        hist = {d: x for d, x in v.items() if d < target}
        same = [x for d, x in sorted(hist.items()) if date.fromisoformat(d).weekday() == wd][-4:]
        ref = same if len(same) >= 3 else list(hist.values())[-7:]
        base = st.mean(ref)
        sd = st.pstdev(ref)
        pct = (v[target] - base) / base * 100 if base else 0.0
        z = (v[target] - base) / max(sd, 0.02 * base)
        rows.append(dict(system=system, unit=unit, today=v[target], base=base, pct=pct, z=z))

    present = [s for s in SYSTEM_ORDER if any(r["system"] == s for r in rows)]
    med = {s: st.median(r["pct"] for r in rows if r["system"] == s) for s in present}

    weather_today = next((w for w in data.get("weather", []) if w["date"] == target), None)
    rain = _f(weather_today["rain_mm"]) if weather_today else 0.0
    temp = _f(weather_today["temp_c"]) if weather_today else 0.0

    exceptions = []
    for r in rows:
        if abs(r["pct"]) < pct_thr or abs(r["z"]) < z_thr:
            continue
        causes = [f"{o['asset']} outage ({_f(o['hours']):g} h)"
                  for o in data.get("outages", [])
                  if o["date"] == target and o["system"] == r["system"] and o["unit"] == r["unit"]]
        causes += [e["note"] for e in data.get("events", [])
                   if e["date"] == target and e["system"] == r["system"]
                   and e["unit"] in (r["unit"], "")]
        weather_c = []
        if r["pct"] < 0 and abs(r["pct"] - med[r["system"]]) <= 10:   # only near the system-wide dip
            if rain >= RAIN_MM:
                weather_c.append(f"heavy rain ({rain:g} mm)")
            if temp >= HEAT_C:
                weather_c.append(f"extreme heat ({temp:g} C)")
        r["causes"] = causes + weather_c
        r["status"] = "likely" if causes else ("possible" if weather_c else "unexplained")
        exceptions.append(r)
    exceptions.sort(key=lambda r: -abs(r["today"] - r["base"]))

    # network totals per day (for the trend chart)
    per_day: dict[str, float] = {}
    for v in series.values():
        for d, x in v.items():
            per_day[d] = per_day.get(d, 0.0) + x
    trend = [{"date": d, "riders": per_day[d]} for d in sorted(per_day)]

    systems = []
    for s in present:
        sr = [r for r in rows if r["system"] == s]
        t = sum(r["today"] for r in sr)
        b = sum(r["base"] for r in sr)
        systems.append(dict(system=s, today=t, base=b,
                            pct=(t - b) / b * 100 if b else 0.0,
                            units=len(sr),
                            flagged=sum(1 for r in exceptions if r["system"] == s)))
    tot_t = sum(r["today"] for r in rows)
    tot_b = sum(r["base"] for r in rows)

    # per-unit recent history for the exceptions (sparklines)
    hist = {}
    for r in exceptions:
        pts = series[(r["system"], r["unit"])]
        hist[f"{r['system']}|{r['unit']}"] = [
            {"date": d, "riders": pts[d]} for d in sorted(pts) if d <= target][-21:]

    return dict(
        date=target,
        weekday=date.fromisoformat(target).strftime("%A"),
        systems=systems,
        totals=dict(today=tot_t, base=tot_b, pct=(tot_t - tot_b) / tot_b * 100 if tot_b else 0.0),
        exceptions=exceptions,
        network_trend=trend[-30:],
        history=hist,
        context=dict(weather=weather_today, outages=data.get("outages", []),
                     events=data.get("events", [])),
        meta=dict(pct_thr=pct_thr, z_thr=z_thr, units=len(rows), systems=present),
    )


def markdown(facts: dict) -> str:
    """Plain-text one-pager (also what /brief.md serves)."""
    if not facts:
        return "# Daily Transport Brief\n\nNo data.\n"
    L = [f"# Daily Transport Brief - {facts['date']} ({facts['weekday']})", ""]
    n = len(facts["exceptions"])
    un = sum(1 for x in facts["exceptions"] if x["status"] == "unexplained")
    t = facts["totals"]
    L.append(f"**Summary:** {n} exception(s); {un} unexplained. "
             f"Network {int(t['today']):,} riders ({t['pct']:+.1f}% vs usual).")
    L += ["", "## Network snapshot", "", "| System | Riders | Usual | Change |", "|---|---:|---:|---:|"]
    for s in facts["systems"]:
        L.append(f"| {s['system']} | {int(s['today']):,} | {int(s['base']):,} | {s['pct']:+.1f}% |")
    L += ["", "## Exceptions (ranked by riders gained/lost)", ""]
    if not facts["exceptions"]:
        L.append("None - all units within normal range.")
    for x in facts["exceptions"]:
        why = "; ".join(x["causes"]) or "no matching outage, event or weather"
        L.append(f"- **{x['unit']}** ({x['system']}) {x['pct']:+.0f}% "
                 f"({int(x['today']):,} vs usual {int(x['base']):,}) "
                 f"- {x['status'].upper()}: {why}")
    L += ["", f"_Method: usual = mean of same weekday over last 4 weeks; flagged if "
              f"change >= {facts['meta']['pct_thr']:g}% and z >= {facts['meta']['z_thr']:g}._"]
    return "\n".join(L) + "\n"
