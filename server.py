"""server.py - PS-13 "Two-Minute Morning Brief" web server.

Serves a one-page operations dashboard for the latest day's transport data,
plus a JSON API and the raw CSVs. Standard library only - no pip install, no
internet, no CDN (charts are drawn as inline SVG on the server), so it keeps
working when the network drops.

Run:
    python server.py                     # http://127.0.0.1:8000
    python server.py --port 9000
    python server.py --data-dir data     # folder holding the four CSVs
    python server.py --refresh 30        # auto-reload the page every 30 s

Routes:
    /                 the dashboard (the "report")
    /api/brief.json   the analysed facts as JSON
    /brief.md         the plain-text one-pager
    /health           "ok" + latest date
    /data/<name>.csv  the raw input feed
"""
from __future__ import annotations

import argparse
import html
import json
import os
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import engine

DATA_DIR = os.environ.get("PS13_DATA", "data")


# --------------------------------------------------------------------- charts
def _spark(values, w=200, h=48, stroke="#4f46e5", fill="rgba(79,70,229,.12)", last_dot=True):
    if not values:
        return "<svg class='spark'></svg>"
    lo, hi = min(values), max(values)
    rng = (hi - lo) or 1.0
    pad = 5
    n = len(values)
    X = lambda i: pad + i * (w - 2 * pad) / max(n - 1, 1)
    Y = lambda v: h - pad - (v - lo) / rng * (h - 2 * pad)
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(values))
    area = f"{X(0):.1f},{h - pad} {pts} {X(n - 1):.1f},{h - pad}"
    dot = (f"<circle cx='{X(n - 1):.1f}' cy='{Y(values[-1]):.1f}' r='3' fill='{stroke}'/>"
           if last_dot else "")
    return (f"<svg class='spark' viewBox='0 0 {w} {h}' preserveAspectRatio='none'>"
            f"<polygon points='{area}' fill='{fill}'/>"
            f"<polyline points='{pts}' fill='none' stroke='{stroke}' stroke-width='2' "
            f"stroke-linejoin='round'/>{dot}</svg>")


def _trend_chart(points, w=880, h=240, stroke="#4f46e5"):
    """points: list of {'date','riders'} - a labelled line chart with gridlines."""
    if not points:
        return "<p class='mut'>No history.</p>"
    vals = [p["riders"] for p in points]
    labels = [p["date"] for p in points]
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    lo -= rng * 0.15
    hi += rng * 0.15
    rng = hi - lo
    L, R, T, B = 64, 16, 16, 28
    n = len(vals)
    X = lambda i: L + i * (w - L - R) / max(n - 1, 1)
    Y = lambda v: T + (hi - v) / rng * (h - T - B)
    grid, ticks = "", ""
    for k in range(5):
        v = lo + rng * k / 4
        y = Y(v)
        grid += f"<line x1='{L}' y1='{y:.1f}' x2='{w - R}' y2='{y:.1f}' stroke='#eef1f7'/>"
        ticks += (f"<text x='{L - 8}' y='{y + 4:.1f}' text-anchor='end' font-size='11' "
                  f"fill='#94a3b8'>{int(v):,}</text>")
    pts = " ".join(f"{X(i):.1f},{Y(v):.1f}" for i, v in enumerate(vals))
    area = f"{X(0):.1f},{h - B} {pts} {X(n - 1):.1f},{h - B}"
    xlab = ""
    for i in (0, n // 2, n - 1):
        xlab += (f"<text x='{X(i):.1f}' y='{h - 8}' text-anchor='middle' font-size='11' "
                 f"fill='#94a3b8'>{labels[i][5:]}</text>")
    return (f"<svg class='trend' viewBox='0 0 {w} {h}' role='img' aria-label='network ridership trend'>"
            f"{grid}{ticks}"
            f"<polygon points='{area}' fill='rgba(79,70,229,.10)'/>"
            f"<polyline points='{pts}' fill='none' stroke='{stroke}' stroke-width='2.5' "
            f"stroke-linejoin='round'/>"
            f"<circle cx='{X(n - 1):.1f}' cy='{Y(vals[-1]):.1f}' r='4' fill='{stroke}'/>"
            f"{xlab}</svg>")


# --------------------------------------------------------------------- helpers
def ni(v):        # int with thousands separators
    return f"{int(round(v)):,}"


def pc(v):        # signed percent
    return f"{v:+.1f}%"


def dirclass(v):
    return "up" if v >= 0 else "down"


# --------------------------------------------------------------------- page
CSS = """
:root{--bg:#eef2f9;--card:#fff;--ink:#0f172a;--mut:#64748b;--line:#e6ebf3;
--up:#0e9f6e;--down:#dc2626;--pri:#4f46e5;--likely:#f59e0b;--possible:#64748b;--unex:#dc2626}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--pri)}
.wrap{max-width:1080px;margin:0 auto;padding:0 20px 56px}
header{background:linear-gradient(135deg,#4338ca,#2563eb);color:#fff;padding:26px 0 30px;
box-shadow:0 6px 24px rgba(37,99,235,.25)}
header .wrap{padding-bottom:0}
.brand{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap}
.brand h1{margin:0;font-size:28px;letter-spacing:-.3px}
.brand .tag{font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;
background:rgba(255,255,255,.18);padding:4px 10px;border-radius:999px}
.sub{margin:8px 0 0;opacity:.9;font-size:14px}
.meta{margin-top:14px;display:flex;gap:18px;flex-wrap:wrap;font-size:13px;opacity:.92}
.meta b{font-weight:700}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;
box-shadow:0 1px 2px rgba(15,23,42,.04);margin-top:18px}
.card h2{margin:0 0 4px;font-size:16px}
.card .cap{color:var(--mut);font-size:12.5px;margin:0 0 14px}
.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:14px;margin-top:18px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:16px 18px;
box-shadow:0 1px 2px rgba(15,23,42,.04)}
.kpi .k{font-size:12px;color:var(--mut);text-transform:uppercase;letter-spacing:.5px;font-weight:600}
.kpi .v{font-size:30px;font-weight:800;letter-spacing:-1px;margin-top:6px}
.kpi .n{font-size:12.5px;color:var(--mut);margin-top:2px}
.kpi .v.up{color:var(--up)}.kpi .v.down{color:var(--down)}
.pill{display:inline-block;font-size:12px;font-weight:700;padding:3px 9px;border-radius:999px}
.pill.up{background:#dcfce7;color:#166534}.pill.down{background:#fee2e2;color:#991b1b}
.badge{display:inline-block;font-size:11px;font-weight:800;letter-spacing:.6px;text-transform:uppercase;
padding:3px 9px;border-radius:999px}
.badge.likely{background:#fef3c7;color:#92400e}
.badge.possible{background:#e2e8f0;color:#334155}
.badge.unexplained{background:#fee2e2;color:#991b1b}
.sys{display:grid;grid-template-columns:repeat(3,1fr);gap:14px}
.sysc{border:1px solid var(--line);border-radius:14px;padding:14px 16px;background:#fbfcfe}
.sysc .nm{font-weight:700;display:flex;justify-content:space-between;align-items:center}
.sysc .big{font-size:26px;font-weight:800;letter-spacing:-.6px;margin:8px 0 2px}
.sysc .sm{color:var(--mut);font-size:12.5px}
.exc{list-style:none;padding:0;margin:0}
.exc li{border:1px solid var(--line);border-left-width:6px;border-radius:12px;padding:14px 16px;
margin-bottom:12px;display:flex;gap:16px;align-items:center;background:#fff}
.exc li.likely{border-left-color:var(--likely)}
.exc li.possible{border-left-color:var(--possible)}
.exc li.unexplained{border-left-color:var(--unex);background:#fff7f7}
.exc .info{flex:1;min-width:0}
.exc .top{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.exc .unit{font-weight:800;font-size:16px}
.exc .sysname{color:var(--mut);font-size:12.5px}
.exc .delta{font-weight:800;font-size:15px}
.exc .delta.up{color:var(--up)}.exc .delta.down{color:var(--down)}
.exc .num{color:var(--mut);font-size:12.5px;margin:4px 0 6px}
.exc .why{font-size:13px;color:#334155}
.exc .why b{color:#0f172a}
.exc .sp{width:190px;flex:0 0 190px}
.spark{width:100%;height:46px;display:block}
.grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
.sig{font-size:13.5px}
.sig h4{margin:0 0 8px;font-size:12px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut)}
.sig ul{margin:0;padding-left:16px}
.sig li{margin-bottom:5px}
.trend{width:100%;height:auto;display:block}
.legend{display:flex;gap:16px;flex-wrap:wrap;font-size:12.5px;color:var(--mut);margin-top:10px}
.legend span{display:flex;align-items:center;gap:6px}
.legend i{width:12px;height:12px;border-radius:3px;display:inline-block}
.src{background:#f1f5ff;border:1px dashed #c7d2fe;border-radius:12px;padding:12px 16px;
font-size:13px;color:#334155;margin-top:18px}
.foot{color:var(--mut);font-size:12.5px;margin-top:22px;line-height:1.7}
.foot code{background:#eef1f7;padding:1px 6px;border-radius:6px;font-size:12px}
.mut{color:var(--mut)}
.clock{font-variant-numeric:tabular-nums}
@media(max-width:820px){.kpis{grid-template-columns:repeat(2,1fr)}.sys{grid-template-columns:1fr}
.grid3{grid-template-columns:1fr}.exc li{flex-direction:column;align-items:flex-start}
.exc .sp{width:100%;flex:1 1 auto}}
"""

JS = """
function clock(){var e=document.getElementById('clock');if(e){
e.textContent=new Date().toLocaleTimeString();}}
setInterval(clock,1000);clock();
"""


def render_page(facts, src_meta, refresh):
    e = html.escape
    if not facts:
        body = "<div class='card'><h2>No data</h2><p class='mut'>Put the four CSVs in the data folder.</p></div>"
        return _shell("No data", body, refresh, "", "")

    t = facts["totals"]
    n = len(facts["exceptions"])
    un = sum(1 for x in facts["exceptions"] if x["status"] == "unexplained")
    likely = sum(1 for x in facts["exceptions"] if x["status"] == "likely")
    worst = facts["exceptions"][0] if facts["exceptions"] else None

    # KPI row
    kpis = f"""
    <div class="kpis">
      <div class="kpi"><div class="k">Riders (all systems)</div>
        <div class="v">{ni(t['today'])}</div>
        <div class="n">vs usual {ni(t['base'])} - <span class="{dirclass(t['pct'])}">{pc(t['pct'])}</span></div></div>
      <div class="kpi"><div class="k">Exceptions flagged</div>
        <div class="v">{n}</div><div class="n">{likely} likely, {n - likely - un} possible</div></div>
      <div class="kpi"><div class="k">Need follow-up</div>
        <div class="v {'down' if un else 'up'}">{un}</div>
        <div class="n">no matching cause</div></div>
      <div class="kpi"><div class="k">Largest movement</div>
        <div class="v {'up' if worst and worst['pct'] > 0 else 'down'}" style="font-size:22px">{pc(worst['pct']) if worst else '-'}</div>
        <div class="n">{e(worst['unit']) if worst else ''}</div></div>
    </div>"""

    # trend chart
    trend = _trend_chart(facts["network_trend"])

    # system cards
    syscards = ""
    for s in facts["systems"]:
        syscards += (f"<div class='sysc'><div class='nm'><span>{e(s['system'])}</span>"
                     f"<span class='pill {dirclass(s['pct'])}'>{pc(s['pct'])}</span></div>"
                     f"<div class='big'>{ni(s['today'])}</div>"
                     f"<div class='sm'>usual {ni(s['base'])} &middot; {s['units']} units &middot; "
                     f"{s['flagged']} flagged</div></div>")

    # exceptions
    items = ""
    for x in facts["exceptions"]:
        key = f"{x['system']}|{x['unit']}"
        vals = [h["riders"] for h in facts["history"].get(key, [])]
        story = f"{ni(x['today'])} riders vs usual {ni(x['base'])}"
        why = "; ".join(x["causes"]) or "no matching outage, event or weather &mdash; a human should check"
        up = x["pct"] > 0
        spark = _spark(vals, stroke="#0e9f6e" if up else "#dc2626",
                       fill="rgba(14,159,110,.12)" if up else "rgba(220,38,38,.10)") if vals else ""
        items += (f"<li class='{x['status']}'><div class='info'>"
                  f"<div class='top'><span class='unit'>{e(x['unit'])}</span>"
                  f"<span class='sysname'>{e(x['system'])}</span>"
                  f"<span class='delta {dirclass(x['pct'])}'>{pc(x['pct'])}</span>"
                  f"<span class='badge {x['status']}'>{x['status']}</span></div>"
                  f"<div class='num'>{story}</div>"
                  f"<div class='why'><b>{e(x['status'].capitalize())}:</b> {e(why).replace('&amp;mdash;','&mdash;')}</div>"
                  f"</div><div class='sp'>{spark}</div></li>")
    if not items:
        items = "<li class='possible'><div class='info'><div class='top'><span class='unit'>None</span></div><div class='why'>All units within normal range today.</div></div></li>"

    # signals panel
    ctx = facts["context"]
    w = ctx.get("weather") or {}
    rain = w.get("rain_mm", "0")
    temp = w.get("temp_c", "-")
    outs = "".join(f"<li>{e(o['system'])} &middot; {e(o['unit'])} &mdash; {e(o['asset'])} ({o['hours']} h)</li>"
                   for o in ctx.get("outages", [])) or "<li class='mut'>none logged</li>"
    evs = "".join(f"<li>{e(ev['system'])} &middot; {e(ev['unit']) or 'all'} &mdash; {e(ev['note'])}</li>"
                  for ev in ctx.get("events", [])) or "<li class='mut'>none logged</li>"
    signals = f"""
    <div class='grid3'>
      <div class='sig'><h4>Weather</h4><ul>
        <li>Rain: <b>{rain} mm</b>{' <span class="badge likely">wet</span>' if float(rain or 0) >= 10 else ''}</li>
        <li>Temp: <b>{temp} &deg;C</b>{' <span class="badge unexplained">hot</span>' if str(temp).replace('.','',1).isdigit() and float(temp) >= 44 else ''}</li></ul></div>
      <div class='sig'><h4>Asset outages</h4><ul>{outs}</ul></div>
      <div class='sig'><h4>Events / diversions</h4><ul>{evs}</ul></div>
    </div>"""

    src = (f"<b>Where this data came from:</b> {e(src_meta)} "
           f"Latest day <b>{facts['date']} ({facts['weekday']})</b>, "
           f"{facts['meta']['units']} units across {', '.join(facts['meta']['systems'])}."
           + (f" <a href='/brief.md'>plain-text brief</a> &middot; <a href='/api/brief.json'>JSON</a>."
              if True else ""))
    # data source links
    links = " &middot; ".join(f"<a href='/data/{k}.csv'>{k}.csv</a>" for k in engine.FEEDS)

    body = f"""
    <div class="src">{src}<br><span class="mut">Raw feeds: {links}</span></div>
    {kpis}
    <div class="card"><h2>Network ridership &mdash; last 30 days</h2>
      <p class="cap">Sum of Metro + BRTS + AMTS riders per day. The final point is the reporting day.</p>
      {trend}
      <div class="legend">
        <span><i style="background:var(--up)"></i>ridership up</span>
        <span><i style="background:var(--down)"></i>down vs usual</span>
      </div>
    </div>
    <div class="card"><h2>By system</h2>
      <p class="cap">Today's total vs the usual (mean of the same weekday over the last 4 weeks).</p>
      <div class="sys">{syscards}</div>
    </div>
    <div class="card"><h2>Exceptions</h2>
      <p class="cap">Ranked by riders gained/lost. Each shows a 21-day trend for that unit.</p>
      <ul class="exc">{items}</ul>
      <div class="legend">
        <span><i style="background:var(--likely)"></i>LIKELY - outage or event matches</span>
        <span><i style="background:var(--possible)"></i>POSSIBLE - weather only</span>
        <span><i style="background:var(--unex)"></i>UNEXPLAINED - needs a human</span>
      </div>
    </div>
    <div class="card"><h2>Signals used to explain the day</h2>
      <p class="cap">Every cause above is matched against these inputs &mdash; nothing is guessed.</p>
      {signals}
    </div>
    <div class="foot">
      <b>Method.</b> usual = mean of the same weekday over the last 4 weeks; a unit is flagged when
      change &ge; <code>{facts['meta']['pct_thr']:g}%</code> and z &ge; <code>{facts['meta']['z_thr']:g}</code>.
      Causes are rule-matched from the outage, event and weather feeds; weather only explains a drop
      that sits near the system-wide dip, so an unrelated fall is reported as UNEXPLAINED rather than
      blamed on the rain. Data here is <b>synthetic sample data</b> (see <code>data_gen.py</code>) &mdash;
      swap the CSVs in <code>data/</code> with a real feed to go live.<br>
      <b>PPP.</b> A private partner runs this as a managed reporting service for the three agencies,
      billed per agency/month; feeds stay on the authority's servers and the whole stack is offline-capable.
    </div>"""
    title = f"Daily Transport Brief {facts['date']}"
    return _shell(title, body, refresh, facts["date"], facts["weekday"])


def _shell(title, body, refresh, day, weekday):
    head_refresh = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{head_refresh}
<title>{html.escape(title)}</title><style>{CSS}</style></head><body>
<header><div class="wrap">
  <div class="brand"><h1>Two-Minute Morning Brief</h1><span class="tag">PS-13</span></div>
  <p class="sub">Metro &middot; BRTS &middot; AMTS &mdash; daily exceptions with likely causes</p>
  <div class="meta">
    <span>Reporting day: <b>{html.escape(day)}{(' (' + html.escape(weekday) + ')') if weekday else ''}</b></span>
    <span>Rendered <b class="clock" id="clock">now</b></span>
    <span>Metro Hacks &middot; DevFest Gandhinagar</span>
  </div>
</div></header>
<div class="wrap">{body}</div>
<script>{JS}</script></body></html>"""


# --------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "PS13/1.0"

    def log_message(self, *a):        # quiet by default
        pass

    def _send(self, code, body, ctype="text/html; charset=utf-8"):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        refresh = int(q.get("refresh", ["0"])[0] or 0)
        path = u.path

        if path == "/health":
            f = engine.analyse(engine.load(DATA_DIR))
            self._send(200, json.dumps({"status": "ok", "date": f["date"] if f else None}),
                       "application/json")
            return

        if path == "/api/brief.json":
            f = engine.analyse(engine.load(DATA_DIR))
            self._send(200, json.dumps(f, indent=2, default=str), "application/json")
            return

        if path == "/brief.md":
            f = engine.analyse(engine.load(DATA_DIR))
            self._send(200, engine.markdown(f), "text/markdown; charset=utf-8")
            return

        if path.startswith("/data/"):
            name = os.path.basename(path)
            full = os.path.join(DATA_DIR, name)
            if name in {f"{k}.csv" for k in engine.FEEDS} and os.path.exists(full):
                with open(full, "rb") as fh:
                    self._send(200, fh.read(), "text/csv; charset=utf-8")
            else:
                self._send(404, "not found", "text/plain; charset=utf-8")
            return

        if path in ("/", "/index.html"):
            data = engine.load(DATA_DIR)
            f = engine.analyse(data)
            meta = (f"Synthetic sample generated by data_gen.py - {len(data['ridership'])} ridership rows "
                    f"over {len(data['weather'])} days. Not a live feed.")
            self._send(200, render_page(f, meta, refresh))
            return

        self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    global DATA_DIR
    ap = argparse.ArgumentParser(description="PS-13 morning brief web server")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=DATA_DIR)
    ap.add_argument("--refresh", type=int, default=0, help="auto-reload seconds (0 = off)")
    ap.add_argument("--open", action="store_true", help="open a browser on start")
    a = ap.parse_args()
    DATA_DIR = a.data_dir
    if not os.path.isdir(DATA_DIR):
        print(f"! data dir '{DATA_DIR}' not found - run: python data_gen.py")
    url = f"http://{a.host}:{a.port}/" + (f"?refresh={a.refresh}" if a.refresh else "")
    httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"PS-13 brief server running at {url}")
    print(f"data: {os.path.abspath(DATA_DIR)}   (Ctrl+C to stop)")
    if a.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
