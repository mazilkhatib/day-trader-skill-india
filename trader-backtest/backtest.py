#!/usr/bin/env python3
"""trader-backtest: replay the pack's ORB day trade on real NSE five-minute bars,
rupee for rupee, with one account, real charges and slippage.

    python3 backtest.py --data DIR --capital 15000
    python3 backtest.py --data DIR --capital 15000 --target 1R --json
    python3 backtest.py --data DIR --capital 15000 --margin 5      # MIS leverage

DIR holds one Yahoo v8 chart per stock (range=60d, interval=5m), named
<TICKER>.json or <TICKER>.json.gz, e.g. RELIANCE.json. Yahoo keeps 60 days of
five-minute bars, so a run covers about the last 58 NSE sessions.

What it does, every session, bar by bar, exactly as the pack would on the day:
  - scanner (orb): the opening range is the first --or-minutes (15) of each
    stock (9:15 to 9:30 IST). A hit is the first five-minute bar to CLOSE above
    the range high. When several stocks hit on the same bar, the most traded
    so far (price x volume) goes first, like `scan.py orb`.
  - risk window: no entry before no_entries_before (09:30) or on a bar that
    closes after no_entries_after (14:45); at most max_trades_per_day; no new
    entry once the day's realised loss reaches max_daily_loss.
  - entry: a market order at the hit bar's close, plus --slip-entry.
  - exit (trader-exit's rules, on the NSE tick): stop = opening-range midpoint
    floored to the tick, target = --target R rounded to the tick. A plan the
    exit planner refuses (stop above the entry, too tight) is skipped.
  - size (trader-budget's size_trade, unchanged): whole shares, bound by the
    max loss a trade, the position cap, the max order and the cash.
  - one position at a time (one account, cash only unless --margin).
  - after the entry bar: a bar that OPENS through the stop fills at its open
    (a gap), a bar that touches the stop fills at the stop less --slip-stop, a
    bar that reaches the target fills at the target (a limit order). A bar that
    touches both counts as the stop (the conservative call, as in the time
    machine). Still open at flat_by (15:15): out at that bar's open less
    --slip-flat.
  - charges (Zerodha equity intraday, per executed order): brokerage 0.03 %
    or Rs 20 whichever is lower, STT 0.025 % on the sell, NSE transaction
    0.00307 %, SEBI Rs 10 a crore, stamp duty 0.003 % on the buy, GST 18 % on
    brokerage + transaction + SEBI. Override with --brokerage-cap etc. if your
    broker differs.

The equity compounds: each day's sizing uses the account as it stands. The
report is a replay of the past, not a prediction, and not financial advice.
"""

import argparse
import datetime
import gzip
import importlib.util
import json
import os
import statistics
import sys
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _load(folder, name):
    path = os.path.join(ROOT, folder, name + ".py")
    spec = importlib.util.spec_from_file_location("bt_" + name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tm = _load("trader-timemachine", "timemachine")
exitp = _load("trader-exit", "exit")
size = _load("trader-budget", "size")


class BTError(Exception):
    pass


# ---------------------------------------------------------------- data

def read_text(path):
    opener = gzip.open if path.endswith(".gz") else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def load_universe(folder, now_utc, symbols=None):
    """DIR -> {SYM: {date: [75 bars]}} with full NSE sessions only."""
    out, skipped = {}, {}
    names = sorted(os.listdir(folder))
    for fn in names:
        if not (fn.endswith(".json") or fn.endswith(".json.gz")):
            continue
        sym = fn.split(".json")[0].upper()
        if sym.startswith("^") or (symbols and sym not in symbols):
            continue
        try:
            res = tm.chart_result(read_text(os.path.join(folder, fn)), sym)
            _, bars, _ = tm.parse_bars(res, now_utc)
            full, short = tm.full_sessions(tm.split_sessions(bars), "9999-12-31")
        except Exception as e:  # one bad file never stops the run
            skipped[sym] = str(e)[:80]
            continue
        if full:
            out[sym] = {d: b for d, b in full}
    if not out:
        raise BTError("no usable charts in %s" % folder)
    return out, skipped


# ---------------------------------------------------------------- money

def charges(value, side, c):
    """Zerodha-style equity intraday charges for one executed order of `value` rupees."""
    brokerage = min(c["brokerage_cap"], c["brokerage_pct"] / 100 * value)
    stt = c["stt_pct"] / 100 * value if side == "sell" else 0.0
    txn = c["txn_pct"] / 100 * value
    sebi = value * 10 / 1e7
    stamp = c["stamp_pct"] / 100 * value if side == "buy" else 0.0
    gst = 0.18 * (brokerage + txn + sebi)
    return brokerage + stt + txn + sebi + stamp + gst


def slot_time(slot):
    m = 9 * 60 + 15 + 5 * slot
    return "%02d:%02d" % divmod(m, 60)


def hhmm_to_slot(text):
    h, m = (int(x) for x in text.split(":"))
    return (h * 60 + m - (9 * 60 + 15)) // 5


# ---------------------------------------------------------------- the replay

def run(universe, a, cfg):
    n_range = a.or_minutes // 5
    first_slot = max(n_range, hhmm_to_slot(cfg["no_entries_before"]))
    last_entry_slot = hhmm_to_slot(cfg["no_entries_after"]) - 1        # the bar must CLOSE by the cutoff
    flat_slot = hhmm_to_slot(cfg["flat_by"])
    target_r = Decimal(str(a.target.rstrip("Rr")))

    days = sorted({d for s in universe.values() for d in s})
    days = [d for d in days if sum(1 for s in universe.values() if d in s) >= max(1, len(universe) // 2)]
    equity = float(a.capital)
    trades, daily = [], []
    stop_rule = "or_low" if a.stop in ("or-low", "or_low", "low") else "or_mid"
    last_close = {}                        # previous session's close, for the gap filter
    for day in days:
        book = {s: universe[s][day] for s in universe if day in universe[s]}
        gap = {s: (100 * (book[s][0]["o"] / last_close[s] - 1) if s in last_close else None) for s in book}
        for s in book:
            last_close[s] = book[s][-1]["c"]
        orng = {}
        for s, bars in book.items():
            oh = max(b["h"] for b in bars[:n_range])
            ol = min(b["l"] for b in bars[:n_range])
            orng[s] = (oh, ol)
        turnover = {s: 0.0 for s in book}
        triggered, pos = set(), None
        n_trades, day_loss, day_pnl = 0, 0.0, 0.0
        start_equity = equity
        for slot in range(len(next(iter(book.values())))):
            for s, bars in book.items():
                turnover[s] += bars[slot]["c"] * bars[slot]["v"]
            # 1) manage the open position on this bar
            if pos and slot > pos["slot"]:
                b = book[pos["sym"]][slot]
                ex = None
                if slot >= flat_slot:
                    ex = (b["o"] * (1 - a.slip_flat / 100), "flat by %s" % cfg["flat_by"])
                elif b["o"] <= pos["stop"]:
                    ex = (b["o"] * (1 - a.slip_stop / 100), "stop (gap)")
                elif b["l"] <= pos["stop"]:
                    ex = (pos["stop"] * (1 - a.slip_stop / 100), "stop")
                elif b["h"] >= pos["target"]:
                    ex = (pos["target"], "target")
                if ex:
                    px, why = ex
                    value_out = px * pos["qty"]
                    cost = charges(pos["value_in"], "buy", cfg) + charges(value_out, "sell", cfg)
                    pnl = value_out - pos["value_in"] - cost
                    equity += pnl
                    day_pnl += pnl
                    if pnl < 0:
                        day_loss += -pnl
                    trades.append(dict(pos, exit=round(px, 2), exit_time=slot_time(slot) if why.startswith("flat")
                                       else slot_time(slot + 1), outcome=why, charges=round(cost, 2),
                                       pnl=round(pnl, 2), R=round((px - pos["entry"]) / pos["risk"], 2),
                                       equity=round(equity, 2)))
                    pos = None
                    continue                       # no new entry on the bar that closed a trade
            # 2) look for an entry on this bar's close
            hits = []
            for s, bars in book.items():
                if s in triggered or slot < n_range:
                    continue
                if bars[slot]["c"] > orng[s][0]:
                    triggered.add(s)               # the first close above the range: later ones are stale
                    g = gap[s]
                    if a.max_gap is not None and (g is None or g > a.max_gap):
                        continue                   # e.g. --max-gap -1: only stocks that opened 1 %+ lower
                    if a.min_gap is not None and (g is None or g < a.min_gap):
                        continue
                    hits.append(s)
            if pos or not hits or slot < first_slot or slot > last_entry_slot:
                continue
            if n_trades >= cfg["max_trades_per_day"] or day_loss >= cfg["max_daily_loss_usd"]:
                continue
            for s in sorted(hits, key=lambda x: -turnover[x]):
                bar = book[s][slot]
                fill = bar["c"] * (1 + a.slip_entry / 100)
                oh, ol = orng[s]
                try:
                    p = exitp.compute(fill, stop_rule, None, "r", target_r, oh, ol)
                except exitp.PlanError:
                    continue
                entry, stop, target = float(p["entry"]), float(p["stop"]), float(p["target"])
                plan = {"symbol": s, "side": "buy", "date": day, "setup": "orb", "entry": entry,
                        "stop": stop, "target": target}
                buying = equity * a.margin
                sz = size.size_trade(plan, cfg, budget=buying, cash=buying)
                if not sz["qty"]:
                    continue
                qty = int(sz["qty"])
                pos = {"date": day, "sym": s, "slot": slot, "entry_time": slot_time(slot + 1), "qty": qty,
                       "entry": entry, "stop": stop, "target": target, "risk": entry - stop,
                       "value_in": entry * qty, "bound_by": sz["bound_by"]}
                n_trades += 1
                break
        daily.append({"date": day, "pnl": round(day_pnl, 2), "equity": round(equity, 2),
                      "trades": n_trades, "start": round(start_equity, 2)})
    return trades, daily


# ---------------------------------------------------------------- stats

def summarize(trades, daily, capital):
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    peak, mdd, mdd_at = capital, 0.0, None
    for d in daily:
        peak = max(peak, d["equity"])
        dd = (peak - d["equity"]) / peak
        if dd > mdd:
            mdd, mdd_at = dd, d["date"]
    end = daily[-1]["equity"] if daily else capital
    gross_w = sum(t["pnl"] for t in wins)
    gross_l = -sum(t["pnl"] for t in losses)
    by_month = {}
    for d in daily:
        k = d["date"][:7]
        by_month.setdefault(k, 0.0)
        by_month[k] += d["pnl"]
    outcomes = {}
    for t in trades:
        k = t["outcome"].split(" (")[0] if not t["outcome"].startswith("flat") else "flat by close"
        outcomes[k] = outcomes.get(k, 0) + 1
    return {
        "sessions": len(daily), "from": daily[0]["date"] if daily else None, "to": daily[-1]["date"] if daily else None,
        "start_capital": capital, "end_equity": round(end, 2), "net_pnl": round(end - capital, 2),
        "return_pct": round(100 * (end - capital) / capital, 2),
        "trades": len(trades), "wins": len(wins), "losses": len(losses),
        "win_rate": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "avg_win": round(statistics.mean(t["pnl"] for t in wins), 2) if wins else 0.0,
        "avg_loss": round(statistics.mean(t["pnl"] for t in losses), 2) if losses else 0.0,
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "avg_R": round(statistics.mean(t["R"] for t in trades), 3) if trades else 0.0,
        "charges": round(sum(t["charges"] for t in trades), 2),
        "max_drawdown_pct": round(100 * mdd, 2), "max_drawdown_date": mdd_at,
        "best_day": max(daily, key=lambda d: d["pnl"]) if daily else None,
        "worst_day": min(daily, key=lambda d: d["pnl"]) if daily else None,
        "green_days": sum(1 for d in daily if d["pnl"] > 0), "red_days": sum(1 for d in daily if d["pnl"] < 0),
        "flat_days": sum(1 for d in daily if d["trades"] == 0),
        "outcomes": outcomes, "by_month": {k: round(v, 2) for k, v in sorted(by_month.items())},
    }


def rs(x):
    s = "{:,.2f}".format(abs(x))
    return ("-Rs " if x < 0 else "Rs ") + s


def report(s, a, cfg, n_syms):
    L = ["ORB backtest on NSE: %d stocks, %d sessions (%s to %s)" % (n_syms, s["sessions"], s["from"], s["to"]),
         "Rules: 15-min opening range, entry on the first 5-min close above it (09:30-14:45 IST),",
         "       stop at the range %s, target %s, flat by %s, max %d trades and %s loss a day," % (
             "low" if a.stop == "or-low" else "midpoint", a.target, cfg["flat_by"], cfg["max_trades_per_day"],
             rs(cfg["max_daily_loss_usd"])),
         "       %s max loss a trade, %s%% position cap, %s." % (
             rs(cfg["max_loss_per_trade_usd"]), cfg["max_position_pct"],
             "cash only" if a.margin == 1 else "%gx MIS margin" % a.margin),
         "",
         "Start capital   %s" % rs(s["start_capital"]),
         "End equity      %s  (%+.2f %%)" % (rs(s["end_equity"]), s["return_pct"]),
         "Net result      %s after %s of charges" % (rs(s["net_pnl"]), rs(s["charges"])),
         "Trades          %d: %d won, %d lost (win rate %.1f %%)" % (s["trades"], s["wins"], s["losses"], s["win_rate"]),
         "Average         win %s, loss %s, R %+.3f, profit factor %s" % (
             rs(s["avg_win"]), rs(s["avg_loss"]), s["avg_R"], s["profit_factor"]),
         "Outcomes        " + ", ".join("%s %d" % kv for kv in sorted(s["outcomes"].items(), key=lambda kv: -kv[1])),
         "Max drawdown    %.2f %% (on %s)" % (s["max_drawdown_pct"], s["max_drawdown_date"]),
         "Days            %d green, %d red, %d with no trade" % (s["green_days"], s["red_days"], s["flat_days"]),
         "Months          " + ", ".join("%s %s" % (k, rs(v)) for k, v in s["by_month"].items()),
         "",
         "A replay of the past on %d sessions, not a prediction and not financial advice." % s["sessions"]]
    return L


def main(argv=None):
    ap = argparse.ArgumentParser(description="Replay the pack's ORB day trade on NSE bars with one account.")
    ap.add_argument("--data", required=True, help="folder of <TICKER>.json Yahoo 60d 5m charts")
    ap.add_argument("--capital", type=float, default=15000)
    ap.add_argument("--target", default="2R", help="target in R (default 2R, trader-exit's default)")
    ap.add_argument("--or-minutes", type=int, default=15)
    ap.add_argument("--stop", default="or-mid", choices=["or-mid", "or-low"],
                    help="stop at the opening range's midpoint (default) or its low")
    ap.add_argument("--max-gap", type=float, help="only stocks whose open gapped at most this %% from the "
                    "previous close (e.g. -1: opened 1 %% or more lower)")
    ap.add_argument("--min-gap", type=float, help="only stocks whose open gapped at least this %%")
    ap.add_argument("--margin", type=float, default=1.0, help="buying power as a multiple of equity (MIS: 5)")
    ap.add_argument("--max-loss-pct", type=float, default=1.0, help="max loss a trade, %% of the capital (1)")
    ap.add_argument("--daily-loss-pct", type=float, default=3.0, help="max loss a day, %% of the capital (3)")
    ap.add_argument("--max-trades", type=int, default=3)
    ap.add_argument("--slip-entry", type=float, default=0.05, help="%% added to the entry fill (0.05)")
    ap.add_argument("--slip-stop", type=float, default=0.05, help="%% taken off a stop fill (0.05)")
    ap.add_argument("--slip-flat", type=float, default=0.03, help="%% taken off a flat-by fill (0.03)")
    ap.add_argument("--brokerage-pct", type=float, default=0.03)
    ap.add_argument("--brokerage-cap", type=float, default=20.0)
    ap.add_argument("--symbols", help="comma-separated subset of the files in --data")
    ap.add_argument("--now", help="clock for the data (default: now)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trades-csv", help="write every trade to this CSV")
    a = ap.parse_args(argv)

    cfg = dict(size.DEFAULTS)
    cfg.update({
        "budget": a.capital, "cash_buffer_pct": 10, "max_position_pct": 75,
        "max_order_usd": 1e12, "min_order_usd": 500,
        "max_loss_per_trade_usd": round(a.capital * a.max_loss_pct / 100, 2),
        "max_daily_loss_usd": round(a.capital * a.daily_loss_pct / 100, 2),
        "max_trades_per_day": a.max_trades, "min_reward_risk": 1.0,
        "no_entries_before": "09:30", "no_entries_after": "14:45", "flat_by": "15:15",
        "brokerage_pct": a.brokerage_pct, "brokerage_cap": a.brokerage_cap,
        "stt_pct": 0.025, "txn_pct": 0.00307, "stamp_pct": 0.003,
    })
    now_utc = tm.parse_now(a.now)
    syms = {s.strip().upper() for s in a.symbols.split(",")} if a.symbols else None
    try:
        universe, skipped = load_universe(a.data, now_utc, syms)
        trades, daily = run(universe, a, cfg)
    except BTError as e:
        print("backtest.py: %s" % e, file=sys.stderr)
        return 1
    s = summarize(trades, daily, a.capital)
    if a.trades_csv:
        import csv
        cols = ["date", "sym", "entry_time", "qty", "entry", "stop", "target", "exit", "exit_time", "outcome",
                "charges", "pnl", "R", "equity", "bound_by"]
        with open(a.trades_csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            w.writerows(trades)
    if a.json:
        print(json.dumps({"summary": s, "config": {k: cfg[k] for k in (
            "max_loss_per_trade_usd", "max_daily_loss_usd", "max_trades_per_day", "flat_by")},
            "target": a.target, "margin": a.margin, "stocks": sorted(universe), "skipped": skipped,
            "daily": daily, "trades": trades}, indent=1, default=str))
    else:
        print("\n".join(report(s, a, cfg, len(universe))))
        if skipped:
            print("Skipped %d file(s): %s" % (len(skipped), ", ".join(sorted(skipped))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
