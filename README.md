# The Day Trader skill, India (NSE) edition

10 Claude skills for intraday trading on NSE: scan Nifty charts, time every entry and exit on IST, replay every outcome, and backtest an account before you risk a rupee.

A fork of Jake Schincariol's [day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill)
(MIT), re-timed and re-priced for the National Stock Exchange of India. The
tools are plain Python 3.8+ with nothing to pip install. They read free
public data: Yahoo Finance charts (`RELIANCE.NS`) and NSE's symbol lists.

No trade goes through until you say yes, and nothing in this pack can place
an order on its own. Paper trading is the default.

## What changed from the US original

| | US original | this fork |
| --- | --- | --- |
| clock | New York, with US daylight saving | IST, UTC+5:30 all year |
| session | 9:30 to 16:00, 78 five-minute bars | 9:15 to 15:30, 75 five-minute bars |
| entries | 09:45 to 15:30, flat by 15:55 | 09:30 to 14:45, flat by 15:15 (before MIS auto square-off) |
| calendar | NYSE holidays and early closes, 2026-27 | NSE equity holidays 2026, no early closes |
| universe | Nasdaq Trader lists | NSE lists: Nifty 50/100/200/500 or every EQ-series stock |
| symbols | `BRK.B` -> `BRK-B` | `M&M` -> `M%26M.NS`, `BAJAJ-AUTO` -> `BAJAJ-AUTO.NS` |
| prices | cents | NSE tick by price band (0.01 under Rs 250, 0.05 to 1,000, 0.10 to 5,000, ...) |
| money | dollars, `_usd` keys | rupees; `_inr` keys accepted (`max_loss_per_trade_inr`) |
| broker | Alpaca / IBKR connectors | `"broker": "manual"`: tickets you enter in Kite, Dhan, Upstox |
| news | Yahoo + SEC EDGAR 8-K | Yahoo (region IN); SEC only with `--sec` |
| removed | | trader-analyst (SEC XBRL), trader-pelosi (US Congress) |
| added | | trader-backtest: the whole account, rupee for rupee, with charges |

## Install

```bash
git clone https://github.com/akshaym579/day-trader-skill-india.git
cp -r day-trader-skill-india/trader-* ~/.claude/skills/
```

Or as a Claude Code plugin from your fork:

```
/plugin marketplace add akshaym579/day-trader-skill-india
/plugin install day-trader-skill-india@day-trader-skill-india
```

If the US pack is installed too, the skill names clash (`trader-scanner`
and so on): keep one of them.

## The ten

| skill | what it does |
| --- | --- |
| `/trader-scanner` | NSE universe (Nifty 500 default), daily setups, the opening-range breakout scan at 9:30+ |
| `/trader-news` | Yahoo headlines for the stocks you watch or hold, tagged for day trading |
| `/trader-entry` | watches a setup bar by bar and says the moment it triggers |
| `/trader-exit` | stop and target before the entry, on the NSE tick, flat by 15:15 |
| `/trader-timemachine` | replays the rest of the session 10,000 times from the stock's own bars |
| `/trader-backtest` | replays the whole pack on 60 days of NSE bars with one account and real charges |
| `/trader-risk` | the last check: window, daily loss, trades a day, the stop's side |
| `/trader-budget` | sizes the trade in whole shares from your rupee limits |
| `/trader-broker` | the order ticket, your yes, the log; manual legs for MIS orders |
| `/trader-alerts` | stop, target, flat-by-close and price alerts on the NSE clock |

## Config

`trader/config.json` (start from `trader-budget/config.example.json`):

```json
{
  "budget": 100000, "max_loss_per_trade_inr": 500, "max_daily_loss_inr": 1500,
  "max_trades_per_day": 3, "no_entries_before": "09:30", "no_entries_after": "14:45",
  "flat_by": "15:15", "paper": true
}
```

## Holidays

Each tool carries `NSE_HOLIDAYS` for 2026. NSE publishes the next year's list
in December: add it to every tool (search for `NSE_HOLIDAYS`) and the tests in
`tests/test_alerts.py`.

## Tests

```bash
python3 -m pytest tests
```

The original's fixtures are real US sessions. `tests/fixtures/convert_us_to_nse.py`
re-timed them onto NSE sessions (9:30 ET -> 9:15 IST, the last three bars
dropped), so the prices stay real while every clock rule is NSE's. The numbers
pinned in the tests are the tools' answers on those re-timed bars.

## Fine print

Not financial advice. A backtest or a replay is the past, not a prediction.
Free quotes are unofficial and can lag. Day trading loses most people money:
SEBI's own study of intraday traders found that most of them lost. SEBI's
retail algo framework puts rules on automated orders through a broker API;
check with your broker before you wire one in.

MIT licensed, like the original.
