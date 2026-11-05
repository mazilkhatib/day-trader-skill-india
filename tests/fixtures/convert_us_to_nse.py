#!/usr/bin/env python3
"""Re-time the original US Yahoo fixtures onto the NSE clock (run once, kept for the record).

The upstream tests replay real US sessions (TSLA on 2026-10-19 and friends). The
India fork keeps the prices and the bar-by-bar shape, and moves every bar onto an
NSE session on the same date:

    ET wall time - 15 minutes  ->  IST wall time     (9:30 ET -> 9:15 IST)

The US session is 15 minutes longer than NSE's (390 vs 375), so the three bars that
would land on 15:30, 15:35 and 15:40 IST (US 15:45 to 15:55) are dropped. Pre- and
after-market bars move too and stay outside the session, as before.

    python3 tests/fixtures/convert_us_to_nse.py FILE [FILE ...]   # rewrites in place
"""
import datetime
import gzip
import json
import sys

EPOCH = datetime.datetime(1970, 1, 1)
IST = datetime.timedelta(hours=5, minutes=30)
SHIFT = datetime.timedelta(minutes=15)
DROP_FROM, DROP_TO = datetime.time(15, 30), datetime.time(15, 45)


def _nth_sunday(year, month, n):
    d = datetime.date(year, month, 1)
    d += datetime.timedelta(days=(6 - d.weekday()) % 7)
    return d + datetime.timedelta(weeks=n - 1)


def et_offset(utc):
    start = datetime.datetime.combine(_nth_sunday(utc.year, 3, 2), datetime.time(7, 0))
    end = datetime.datetime.combine(_nth_sunday(utc.year, 11, 1), datetime.time(6, 0))
    return datetime.timedelta(hours=-4 if start <= utc < end else -5)


def us_to_nse(ts):
    utc = EPOCH + datetime.timedelta(seconds=int(ts))
    ist_wall = utc + et_offset(utc) - SHIFT
    return int((ist_wall - IST - EPOCH).total_seconds()), ist_wall.time()


def retime_result(res, intraday):
    meta = res.get("meta") or {}
    ts = res.get("timestamp") or []
    keep, new_ts = [], []
    for i, t in enumerate(ts):
        if t is None:
            keep.append(i); new_ts.append(None); continue
        nt, wall = us_to_nse(t)
        if intraday and DROP_FROM <= wall < DROP_TO:
            continue
        keep.append(i); new_ts.append(nt)
    if ts:
        res["timestamp"] = new_ts
        for q in (res.get("indicators") or {}).get("quote") or []:
            for k, v in list(q.items()):
                if isinstance(v, list) and len(v) == len(ts):
                    q[k] = [v[i] for i in keep]
        for a in (res.get("indicators") or {}).get("adjclose") or []:
            for k, v in list(a.items()):
                if isinstance(v, list) and len(v) == len(ts):
                    a[k] = [v[i] for i in keep]
        res["indicators"] = res.get("indicators") or {}
    if isinstance(meta.get("regularMarketTime"), (int, float)):
        nt, wall = us_to_nse(meta["regularMarketTime"])
        if intraday and DROP_FROM <= wall < DROP_TO:   # the clock read after the NSE close: pin it to 15:30
            d = (EPOCH + datetime.timedelta(seconds=nt) + IST).date()
            nt = int((datetime.datetime.combine(d, DROP_FROM) - IST - EPOCH).total_seconds())
        meta["regularMarketTime"] = nt
    for per in (meta.get("currentTradingPeriod") or {}).values():
        if isinstance(per, dict):
            for k in ("start", "end"):
                if isinstance(per.get(k), (int, float)):
                    per[k] = us_to_nse(per[k])[0]
            per.update(timezone="IST", gmtoffset=19800)
    meta.pop("tradingPeriods", None)
    meta.update(gmtoffset=19800, timezone="IST", exchangeTimezoneName="Asia/Kolkata")
    return res


def walk(doc):
    if isinstance(doc, dict) and "chart" in doc:
        for r in doc["chart"].get("result") or []:
            gran = (r.get("meta") or {}).get("dataGranularity", "5m")
            retime_result(r, intraday=not str(gran).endswith(("d", "wk", "mo")))
    elif isinstance(doc, dict) and "spark" in doc:
        for item in doc["spark"].get("result") or []:
            for r in item.get("response") or []:
                gran = (r.get("meta") or {}).get("dataGranularity", "5m")
                retime_result(r, intraday=not str(gran).endswith(("d", "wk", "mo")))
    elif isinstance(doc, dict):
        for v in doc.values():
            walk(v)
    return doc


def main(paths):
    for p in paths:
        opener = gzip.open if p.endswith(".gz") else open
        with opener(p, "rt", encoding="utf-8") as fh:
            doc = json.load(fh)
        walk(doc)
        with opener(p, "wt", encoding="utf-8") as fh:
            json.dump(doc, fh, separators=(",", ":"))
        print("re-timed", p)


if __name__ == "__main__":
    main(sys.argv[1:])
