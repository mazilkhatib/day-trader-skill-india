---
name: trader-backtest
description: >-
  Account backtest. Replays the pack's opening-range breakout day trade on
  real NSE five-minute bars, rupee for rupee, with one account: the scanner's
  first close above the 15-minute range, trader-exit's stop and target on the
  NSE tick, trader-budget's sizing, trader-risk's window and daily limits,
  flat by 15:15, Zerodha-style intraday charges and slippage. Prints the end
  equity, the return, win rate, profit factor, max drawdown, the outcomes and
  the months. Use when the user says "backtest this", "how would it have done",
  "simulate with Rs 15,000", "test the strategy on Nifty stocks", "what if I
  had started with", or wants to see an account's result rather than one
  trade's odds (that is /trader-timemachine). Not financial advice.
---

> **India version (NSE).** Times are IST (UTC+5:30); the session is 9:15 to 15:30,
> entries 09:30 to 14:45, flat by 15:15. Money is in rupees.

# trader-backtest

The time machine answers "how does this one trade tend to go from here". The
backtest answers "if I had run the pack every day with this much money, where
would the account be". It uses the pack's own code for every step, so the
numbers move when the rules move.

## Run it

```bash
python3 "${CLAUDE_SKILL_DIR}/backtest.py" --data DIR --capital 15000
python3 "${CLAUDE_SKILL_DIR}/backtest.py" --data DIR --capital 15000 --target 1R
python3 "${CLAUDE_SKILL_DIR}/backtest.py" --data DIR --capital 15000 --margin 5     # MIS leverage
python3 "${CLAUDE_SKILL_DIR}/backtest.py" --data DIR --capital 15000 --json --trades-csv trades.csv
```

`DIR` holds one Yahoo v8 chart per stock, `range=60d&interval=5m`, named
`<TICKER>.json` (or `.json.gz`). Yahoo keeps 60 days of five-minute bars, so a
run covers about the last 58 NSE sessions. Files whose names start with `^`
(an index such as `^NSEI`) are skipped.

To get the files, fetch `https://query1.finance.yahoo.com/v8/finance/chart/<TICKER>.NS?range=60d&interval=5m`
for each stock (the Nifty 100 list is at nsearchives.nseindia.com,
`/content/indices/ind_nifty100list.csv`). Yahoo is free, unofficial and can
rate-limit: pause between requests.

## The rules, step by step

| step | what the replay does | from |
| --- | --- | --- |
| range | the first 15 minutes of each stock, 9:15 to 9:30 | trader-scanner orb |
| hit | the first five-minute bar to close above the range high; later closes are stale | trader-entry |
| order | several hits on one bar: the most traded so far (price x volume) first | trader-scanner |
| window | no entry before 09:30 or on a bar closing after 14:45; max trades a day; stop for the day at the max daily loss | trader-risk |
| plan | stop at the range midpoint floored to the NSE tick, target in R rounded to the tick; a plan the planner refuses is skipped | trader-exit |
| size | whole shares, the smallest of max loss a trade, position cap, max order and cash | trader-budget |
| fill | entry at the hit bar's close plus `--slip-entry` | |
| exit | gap through the stop: the bar's open; touch: the stop less `--slip-stop`; target: the limit; both in one bar: the stop; still open at 15:15: that bar's open less `--slip-flat` | trader-exit, trader-timemachine |
| money | brokerage 0.03 % or Rs 20 (lower), STT 0.025 % on the sell, NSE 0.00307 %, SEBI Rs 10 a crore, stamp 0.003 % on the buy, GST 18 % | Zerodha's published intraday rates |

One position at a time: one account, cash only unless `--margin`. The account
compounds: each trade is sized from the equity as it stands.

## Defaults for a small account

| flag | default | meaning |
| --- | --- | --- |
| `--capital` | 15000 | starting rupees |
| `--target` | 2R | trader-exit's default |
| `--stop` | or-mid | `or-low` puts the stop at the opening range's low (wider: costs weigh less in R) |
| `--max-gap` / `--min-gap` | off | trade only stocks whose open gapped within these % from the previous close (`--max-gap -1`: opened 1 %+ lower) |
| `--max-loss-pct` | 1 | max loss a trade, % of the capital (Rs 150 on 15,000) |
| `--daily-loss-pct` | 3 | max loss a day (Rs 450 on 15,000) |
| `--max-trades` | 3 | entries a day |
| `--margin` | 1 | buying power as a multiple of equity; MIS gives about 5 |
| `--slip-entry` / `--slip-stop` / `--slip-flat` | 0.05 / 0.05 / 0.03 | percent |

## Reading the result

- **Net result and return** are after every charge.
- **Profit factor** is gross wins over gross losses: under 1.0 loses money.
- **Average R** is per trade in units of the planned risk, before charges.
- **Max drawdown** is the worst fall from a peak in the daily equity.
- **Outcomes** split stop, target and flat-by-close exits.
- 58 sessions is a small sample. One quarter's market can flatter or punish
  a rule; say so when you show the numbers.
- Survivorship: the stock list is today's index, applied to the past.

## Safety

- A backtest is a replay of the past, not a prediction. Day trading loses
  most people money; SEBI's own study found most intraday traders lose.
- It never places an order and never talks to a broker.
- Not financial advice.
