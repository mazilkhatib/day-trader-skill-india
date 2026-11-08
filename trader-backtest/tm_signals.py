#!/usr/bin/env python3
"""Run trader-timemachine on every ORB signal in a folder of charts, walk-forward,
and cache its verdict for backtest.py --tm-cache.

    python3 tm_signals.py --data DIR --out tm-cache.json --stop or-mid --target 2R
    python3 tm_signals.py --data DIR --out tm-cache.json --replays 10000 --workers 4

For every stock-day whose first five-minute close above the opening range lands
between 09:30 and 14:45 IST (the signal backtest.py would see), this builds the
plan exactly as backtest.py does (entry = that close plus --slip-entry, stop and
target from trader-exit on the NSE tick) and runs the time machine's own code on it:

  - the pool: the stock's last --lookback (59) full sessions strictly BEFORE the
    signal's day, the same window a live range=60d fetch gives. Nothing from the
    signal's day after the entry bar, and nothing from later days, is used.
  - simulate(): --replays (10,000) Monte Carlo replays of the bars after the entry
    to --until (15:15, the flat-by time), seed 19, window 2: the pack's defaults.
  - history_replay(): the same rule on each of those past sessions.
  - lean(): the target's share of the replays that hit a side, minus break-even.

Signals with fewer than 20 past full sessions (the time machine's minimum) are
left out. The cache holds one row per signal: p_target, p_stop, p_neither,
p_green, avg_R, lean, history avg_R and count.
"""

import argparse
import gzip
import importlib.util
import json
import multiprocessing as mp
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(folder, name):
    spec = importlib.util.spec_from_file_location("ts_" + name, os.path.join(ROOT, folder, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tm = _load("trader-timemachine", "timemachine")
exitp = _load("trader-exit", "exit")

A = None   # the parsed arguments, shared with the workers


def stock_signals(path):
    """One stock's file -> its cache rows."""
    from decimal import Decimal
    sym = os.path.basename(path).split(".json")[0].upper()
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        res = tm.chart_result(fh.read(), sym)
    _, bars, _ = tm.parse_bars(res, tm.parse_now(None))
    full, _ = tm.full_sessions(tm.split_sessions(bars), "9999-12-31")
    n = A.or_minutes // 5
    first = max(n, (9 * 60 + 30 - tm.OPEN_MIN) // 5)
    last_entry = (14 * 60 + 45 - tm.OPEN_MIN) // 5 - 1
    last = (tm.parse_clock(A.until, "--until") - tm.OPEN_MIN) // 5 - 1
    rule_name = "or_low" if A.stop == "or-low" else "or_mid"
    target_r = Decimal(A.target.rstrip("Rr"))
    rule = {"setup": "orb", "or_minutes": A.or_minutes, "stop_rule": rule_name, "target_r": float(target_r)}
    rows = []
    for j, (day, D) in enumerate(full):
        past = full[max(0, j - A.lookback):j]
        if len(past) < tm.MIN_SESSIONS:
            continue
        oh, ol = tm.opening_range(D, n)
        k = next((i for i in range(n, len(D)) if D[i]["c"] > oh), None)
        if k is None or k < first or k > last_entry or k >= last:
            continue
        fill = D[k]["c"] * (1 + A.slip_entry / 100)
        try:
            p = exitp.compute(fill, rule_name, None, "r", target_r, oh, ol)
        except exitp.PlanError:
            continue
        entry, stop, target = float(p["entry"]), float(p["stop"]), float(p["target"])
        pool = tm.build_pool(past)
        mc = tm.simulate(pool, entry, stop, target, k, last, A.replays, A.seed, A.window, "buy")
        lv = tm.lean(mc)
        hist = tm.history_replay(past, rule, last, "buy")
        rows.append({"sym": sym, "date": day, "slot": k, "entry": entry, "stop": stop, "target": target,
                     "p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"],
                     "p_green": mc["p_green"], "avg_R": mc["avg_R"],
                     "lean": round(lv[0], 4) if lv else None,
                     "hist_n": hist["triggered"], "hist_avg_R": hist["avg_R"], "pool": len(past)})
    return sym, rows


def _init(args):
    global A
    A = args


def main(argv=None):
    ap = argparse.ArgumentParser(description="Time-machine verdicts for every ORB signal, walk-forward.")
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--stop", default="or-mid", choices=["or-mid", "or-low"])
    ap.add_argument("--target", default="2R")
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--until", default="15:15", help="replay to this time (default 15:15, the flat-by)")
    ap.add_argument("--replays", type=int, default=10000)
    ap.add_argument("--seed", type=int, default=19)
    ap.add_argument("--window", type=int, default=2)
    ap.add_argument("--lookback", type=int, default=59, help="past full sessions in the pool (59 = a 60d fetch)")
    ap.add_argument("--slip-entry", type=float, default=0.05)
    ap.add_argument("--symbols", help="comma-separated subset")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 1)))
    a = ap.parse_args(argv)
    want = {s.strip().upper() for s in a.symbols.split(",")} if a.symbols else None
    files = sorted(os.path.join(a.data, f) for f in os.listdir(a.data)
                   if (f.endswith(".json") or f.endswith(".json.gz")) and not f.startswith("^")
                   and (not want or f.split(".json")[0].upper() in want))
    t0, out = time.time(), {}
    def note(sym, rows):
        out[sym] = rows
        print("\r  %3d/%d stocks  %5d signals  %4.0fs" % (len(out), len(files), sum(len(v) for v in out.values()),
                                                      time.time() - t0), end="", flush=True)
    if a.workers <= 1:
        _init(a)
        for f in files:
            note(*stock_signals(f))
    else:
        with mp.Pool(a.workers, initializer=_init, initargs=(a,)) as pool:
            for sym, rows in pool.imap_unordered(stock_signals, files):
                note(sym, rows)
    print()
    doc = {"created_by": "trader-backtest/tm_signals.py", "stop": a.stop, "target": a.target, "until": a.until,
           "replays": a.replays, "seed": a.seed, "window": a.window, "lookback": a.lookback,
           "slip_entry": a.slip_entry, "signals": {s: out[s] for s in sorted(out)}}
    with open(a.out, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    print("wrote %s: %d signals in %.0fs" % (a.out, sum(len(v) for v in out.values()), time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
