#!/usr/bin/env python3
"""
SaafHawa tool-latency timing script  (Section 2, item 6)

WHAT IT DOES
  Times four things (the task needs at least 20 calls to each API):
    1. weather tool      -> Open-Meteo forecast API (temperature, humidity, wind)
    2. air-quality tool  -> Open-Meteo Air Quality API (PM2.5, PM10)
                            or OpenAQ v3 if you set OPENAQ_API_KEY (see below)
    3. heat-index tool   -> local calculation (NWS Rothfusz formula)
    4. knowledge-base    -> local keyword lookup over a small advisory list
  Every call is appended to tool_latency_log.csv. That file is your RAW TIMING LOG.

HOW TO USE
  Day 1:   python timing_log.py
  Day 2:   python timing_log.py        (must be a DIFFERENT calendar day)
  Summary: python timing_log.py --summary

  Needs only Python 3.8+ . No pip install needed.

OPTIONAL: use OpenAQ instead of Open-Meteo for air quality
  1. Make a free key at https://explore.openaq.org  (Account -> API key)
  2. Windows PowerShell:   $env:OPENAQ_API_KEY="your_key"
     Mac/Linux:            export OPENAQ_API_KEY="your_key"
  3. Run the script as usual. The log's "source" column shows what was used.

NOTES
  - Only calls with status "ok" are used for medians.
  - The knowledge base here is PLACEHOLDER text, only so the lookup has realistic
    size. Replace it with real advisory text in the final system; the timing
    will not change meaningfully.
"""

import argparse
import csv
import json
import math
import os
import random
import ssl
import statistics
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path

LAT, LON = 24.8607, 67.0011          # Karachi
LOG_FILE = "tool_latency_log.csv"
SIM_FILE = "tool_latency_log_SIMULATED.csv"   # only used by --offline test mode
FIELDS = ["run_id", "timestamp", "date", "tool", "source",
          "call_no", "latency_ms", "status"]

WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast"
    f"?latitude={LAT}&longitude={LON}"
    "&current=temperature_2m,relative_humidity_2m,wind_speed_10m"
)
AQ_URL = (
    "https://air-quality-api.open-meteo.com/v1/air-quality"
    f"?latitude={LAT}&longitude={LON}&current=pm10,pm2_5"
)
OPENAQ_BASE = "https://api.openaq.org/v3"


# ----------------------------------------------------------------------------
# SSL setup (fixes "certificate verify failed: certificate has expired")
# ----------------------------------------------------------------------------
CTX = None          # SSL context used for every request
SSL_NOTE = ""       # appended to the "source" column in the log


def build_ssl_context(insecure):
    """Default: use the up-to-date certifi CA list if installed (Anaconda has it).
    --insecure: skip certificate checks. Acceptable here because we only read
    public weather data, and the log marks those rows with '+noverify'."""
    global CTX, SSL_NOTE
    if insecure:
        CTX, SSL_NOTE = ssl._create_unverified_context(), "+noverify"
        print("WARNING: SSL certificate checks are OFF (--insecure).")
        return
    try:
        import certifi
        CTX = ssl.create_default_context(cafile=certifi.where())
        print("SSL: using certifi certificate list.")
    except Exception:
        CTX = None
        print("SSL: using Python's default certificate list.")


# ----------------------------------------------------------------------------
# Tools
# ----------------------------------------------------------------------------
def http_get_json(url, headers=None, timeout=20):
    req = urllib.request.Request(
        url, headers={"User-Agent": "SaafHawa-timing/1.0", **(headers or {})}
    )
    with urllib.request.urlopen(req, timeout=timeout, context=CTX) as resp:
        return json.loads(resp.read().decode("utf-8"))


def weather_call():
    d = http_get_json(WEATHER_URL)["current"]
    return {"temp_c": d["temperature_2m"],
            "humidity": d["relative_humidity_2m"],
            "wind_kmh": d["wind_speed_10m"]}


def air_quality_call_openmeteo():
    d = http_get_json(AQ_URL)["current"]
    return {"pm25": d["pm2_5"], "pm10": d["pm10"]}


def openaq_setup(api_key):
    """Find one nearby OpenAQ location with PM2.5. Done once, NOT timed."""
    hdr = {"X-API-Key": api_key}
    url = (f"{OPENAQ_BASE}/locations?coordinates={LAT},{LON}"
           "&radius=25000&limit=50")
    for loc in http_get_json(url, hdr).get("results", []):
        sensors = {s["id"]: s["parameter"]["name"] for s in loc.get("sensors", [])}
        if "pm25" in sensors.values():
            return loc["id"], sensors, hdr
    raise RuntimeError("No OpenAQ location with PM2.5 found within 25 km.")


def make_openaq_call(api_key):
    loc_id, sensors, hdr = openaq_setup(api_key)

    def call():
        data = http_get_json(f"{OPENAQ_BASE}/locations/{loc_id}/latest", hdr)
        out = {}
        for r in data.get("results", []):
            name = sensors.get(r.get("sensorsId"))
            if name in ("pm25", "pm10"):
                out[name] = r.get("value")
        if "pm25" not in out:
            raise RuntimeError("PM2.5 missing in OpenAQ response")
        return out

    return call


def heat_index_c(t_c, rh):
    """NWS heat index (Rothfusz regression with standard adjustments). Returns deg C."""
    t = t_c * 9 / 5 + 32
    hi = 0.5 * (t + 61.0 + ((t - 68.0) * 1.2) + (rh * 0.094))
    if (hi + t) / 2 >= 80:
        hi = (-42.379 + 2.04901523 * t + 10.14333127 * rh
              - 0.22475541 * t * rh - 0.00683783 * t * t
              - 0.05481717 * rh * rh + 0.00122874 * t * t * rh
              + 0.00085282 * t * rh * rh - 0.00000199 * t * t * rh * rh)
        if rh < 13 and 80 <= t <= 112:
            hi -= ((13 - rh) / 4) * math.sqrt((17 - abs(t - 95)) / 17)
        elif rh > 85 and 80 <= t <= 87:
            hi += ((rh - 85) / 10) * ((87 - t) / 5)
    return (hi - 32) * 5 / 9


# PLACEHOLDER knowledge base: for timing only. Replace with real advisory text later.
KB = [
    "heatwave drink water often avoid outdoor work between noon and four",
    "children school heat keep indoors shade water rest cooler hours",
    "elderly people heat stroke symptoms dizziness confusion cool body seek doctor",
    "asthma patient air pollution keep inhaler avoid outdoor exercise high pm2.5",
    "outdoor labourer heat breaks shade hydration salt rest every hour",
    "evening jog pollution check air quality avoid exercise near traffic",
    "pm2.5 unhealthy sensitive groups reduce outdoor activity wear mask",
    "pm10 dust hazardous stay indoors close windows",
    "heat index above 41 degrees dangerous limit outdoor exposure",
    "sea breeze humidity coastal heat feels hotter than air temperature",
    "heat exhaustion symptoms heavy sweating weakness cold skin move to shade",
    "pregnant women heat avoid midday sun stay hydrated",
    "pets and animals heat water shade avoid midday walks",
    "n95 mask fine particles smog outdoor protection",
    "school closure heatwave advisory guidance for parents",
    "ors oral rehydration solution dehydration diarrhoea heat",
    "air purifier indoor air quality windows closed pollution",
    "traffic police outdoor worker heat shift timing breaks",
    "morning haze industrial area pollution higher early hours",
    "cooling centre public shelter heatwave municipal advice",
]
KB_TOKENS = [set(doc.split()) for doc in KB]
QUERIES = [
    "heatwave school children outdoor",
    "evening jog pollution asthma",
    "elderly heat stroke symptoms",
    "pm2.5 unhealthy outdoor labourer mask",
    "aaj garmi bachon school",
    "heat index dangerous midday work",
]


def kb_lookup(query, top_k=3):
    q = set(query.lower().split())
    scored = sorted(((len(q & toks), i) for i, toks in enumerate(KB_TOKENS)),
                    reverse=True)[:top_k]
    return [KB[i] for score, i in scored if score > 0]


# ----------------------------------------------------------------------------
# Measurement
# ----------------------------------------------------------------------------
def log_rows(path, rows):
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        if new:
            w.writeheader()
        w.writerows(rows)


def measure(tool, source, n, fn, run_id, sleep_s, simulated=False):
    rows, result = [], None
    for i in range(1, n + 1):
        now = datetime.now().astimezone()
        status = "simulated" if simulated else "ok"
        t0 = time.perf_counter_ns()
        try:
            result = fn()
        except Exception as e:  # network error, timeout, bad JSON ...
            reason = str(getattr(e, "reason", e)).replace(",", ";").replace("\n", " ")
            status = "error: " + type(e).__name__ + " - " + reason[:90]
        ms = (time.perf_counter_ns() - t0) / 1e6
        rows.append({
            "run_id": run_id, "timestamp": now.isoformat(timespec="seconds"),
            "date": now.date().isoformat(), "tool": tool, "source": source,
            "call_no": i, "latency_ms": f"{ms:.4f}", "status": status})
        print(f"  {tool:<14} call {i:>2}/{n}  {ms:10.3f} ms  {status}")
        if (i == 3 and not simulated
                and all(r["status"].startswith("error") for r in rows)):
            sys.exit(
                f"\nThe first 3 {tool} calls all failed, so I stopped (nothing saved).\n"
                "Reason shown above. If it says CERTIFICATE_VERIFY_FAILED, run again with:\n"
                "    python timing_log.py --insecure\n"
                "Otherwise check your internet / VPN / firewall, or open\n"
                "this in your browser:  https://api.open-meteo.com/v1/forecast?"
                "latitude=24.86&longitude=67.0&current=temperature_2m\n"
                "Then send the error text to Claude.")
        if sleep_s:
            time.sleep(sleep_s)
    return rows, result


def run(n, offline, local_n, insecure=False):
    run_id = uuid.uuid4().hex[:8]
    log = SIM_FILE if offline else LOG_FILE
    print(f"Run {run_id}  |  {datetime.now().astimezone():%Y-%m-%d %H:%M %Z}  |  log: {log}")
    if not offline:
        build_ssl_context(insecure)
    sleep_s = 0 if offline else 1.5      # be polite to the free APIs
    rows = []

    if offline:
        print("OFFLINE TEST MODE: network calls are simulated.")
        def fake(vals):
            def f():
                time.sleep(random.uniform(0.05, 0.15))
                return vals
            return f
        w_fn = fake({"temp_c": 36.0, "humidity": 55, "wind_kmh": 14})
        a_fn, a_src = fake({"pm25": 80, "pm10": 150}), "simulated"
    else:
        w_fn = weather_call
        key = os.environ.get("OPENAQ_API_KEY")
        if key:
            print("Air quality source: OpenAQ v3")
            try:
                a_fn, a_src = make_openaq_call(key), "openaq_v3" + SSL_NOTE
            except Exception as e:
                sys.exit(f"OpenAQ setup failed: {e}\n"
                         "Check your key or unset OPENAQ_API_KEY to use Open-Meteo.")
        else:
            print("Air quality source: Open-Meteo Air Quality API")
            a_fn, a_src = air_quality_call_openmeteo, "open_meteo_aq" + SSL_NOTE

    print("\n[1/4] Weather tool")
    r, wx = measure("weather", "simulated" if offline else "open_meteo" + SSL_NOTE,
                    n, w_fn, run_id, sleep_s, offline)
    rows += r
    print("\n[2/4] Air-quality tool")
    r, _ = measure("air_quality", a_src, n, a_fn, run_id, sleep_s, offline)
    rows += r

    t = (wx or {}).get("temp_c", 36.0)
    h = (wx or {}).get("humidity", 55)
    print(f"\n[3/4] Heat-index tool (inputs {t} C, {h}% RH)")
    r, hi = measure("heat_index", "local", local_n,
                    lambda: heat_index_c(t, h), run_id, 0, offline)
    rows += r
    print(f"      heat index = {hi:.1f} C")

    print("\n[4/4] Knowledge-base lookup")
    qi = iter(range(10**9))
    r, _ = measure("kb_lookup", "local", local_n,
                   lambda: kb_lookup(QUERIES[next(qi) % len(QUERIES)]),
                   run_id, 0, offline)
    rows += r

    log_rows(log, rows)
    errs = sum(1 for x in rows if x["status"].startswith("error"))
    print(f"\nSaved {len(rows)} rows to {log}  ({errs} errors).")
    print("Remember: run again on a DIFFERENT calendar day, then use --summary.")


# ----------------------------------------------------------------------------
# Summary
# ----------------------------------------------------------------------------
def summary(path):
    if not Path(path).exists():
        sys.exit(f"{path} not found. Run the script first.")
    with open(path, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    good = [r for r in rows if r["status"] in ("ok", "simulated")]
    days = sorted({r["date"] for r in good})
    print(f"File: {path}  |  rows: {len(rows)}  |  usable: {len(good)}")
    print(f"Days covered: {len(days)} -> {', '.join(days)}\n")
    print(f"{'tool':<14}{'n':>5}{'median ms':>14}{'mean ms':>14}{'max ms':>14}")
    for tool in ("weather", "air_quality", "heat_index", "kb_lookup"):
        v = [float(r["latency_ms"]) for r in good if r["tool"] == tool]
        if v:
            print(f"{tool:<14}{len(v):>5}{statistics.median(v):>14.3f}"
                  f"{statistics.mean(v):>14.3f}{max(v):>14.3f}")
    allv = [float(r["latency_ms"]) for r in good]
    med = statistics.median(allv)
    print(f"\nMEDIAN OF ALL MEASUREMENTS: {med:.3f} ms  ({med/1000:.4f} s)")
    print("(Use this as the delay for every tool call, as the task says.)\n")
    for tool in ("weather", "air_quality"):
        n = sum(1 for r in good if r["tool"] == tool)
        print(f"Check {tool}: {n} calls {'OK' if n >= 20 else 'NEED MORE (>=20)'}")
    print(f"Check days: {len(days)} {'OK' if len(days) >= 2 else 'NEED A SECOND DAY'}")
    bad = len(rows) - len(good)
    if bad:
        print(f"Note: {bad} failed calls are excluded from medians; keep them in the log.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="SaafHawa tool-latency timing")
    ap.add_argument("--calls", type=int, default=30, help="calls per API tool (default 30, min 20)")
    ap.add_argument("--local-calls", type=int, default=20, help="calls per local tool (default 20)")
    ap.add_argument("--summary", action="store_true", help="print medians from the log")
    ap.add_argument("--offline", action="store_true", help="test mode, no network")
    ap.add_argument("--insecure", action="store_true",
                    help="skip SSL certificate checks (use only if the default fails)")
    a = ap.parse_args()
    if a.summary:
        summary(SIM_FILE if a.offline else LOG_FILE)
    else:
        run(max(a.calls, 20), a.offline, max(a.local_calls, 20), a.insecure)
