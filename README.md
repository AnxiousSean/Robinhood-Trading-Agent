# Robinhood Trading Agent

Prediction-market trading agent and terminal (PM Agent).

An automated agent for the event contracts Robinhood lists, plus a terminal
to watch it, trade manually and audit everything it does.

> **Read this first: `RESULTS.md`.** On 8,334 settled markets / 1.15M trades,
> no strategy showed a statistically reliable profit after fees on held-out data.
> Buying at the ask lost −2% to −4%. Posting limit orders (maker) was break-even
> with Robinhood's commission and +0.7% (CI −4%…+6%) on Kalshi directly. The agent
> ships in **paper mode** running the maker strategy, so you can test whether
> that edge exists *now*, before any money is at risk.

## How it connects to Robinhood

Robinhood has **no API for event contracts**. Its agentic-trading beta covers
equities only, and prediction markets are listed as "planned". Robinhood's
prediction markets are Kalshi-listed contracts, so the agent:

* reads **market data from Kalshi's public API**: the same contracts, prices and settlement results;
* executes in one of three modes:

| mode        | what happens                                                                                       |
|-------------|-----------------------------------------------------------------------------------------------------|
| `paper`     | (default) simulated fills at the live ask, with Robinhood fees. No money moves.                     |
| `robinhood` | The agent writes **trade tickets**. You place them in the Robinhood app, then `fill <id>` or `reject <id>` in the terminal. |
| `kalshi`    | Live IOC limit orders through Kalshi's API with your own Kalshi API key. Test with `kalshi_env: demo` first. |

Automating the Robinhood app itself (screen-scraping or an unofficial API)
would break Robinhood's terms and risk your account, so the agent doesn't do it.
When Robinhood ships event contracts in its agent API, add a broker class in
`pmagent/broker.py`; everything else stays the same.

## Install

```bash
cd Robinhood-Trading-Agent
pip install -e .
pm init            # writes data/config.json with safe defaults (paper mode, $1,000 bankroll)
```

## Workflow

```bash
pm research        # ~30-45 min: pull settled markets + trades, fit model, walk-forward backtest
pm tui             # open the terminal; the agent starts scanning every 3 minutes
```

Other entry points: `pm scan` (one cycle, prints signals, places nothing),
`pm run` (headless loop), `pm status`, `pm cmd quote <TICKER>`.

A scan pulls ~44,000 open markets and takes about 2.5 minutes (Kalshi pages
are sequential). The **EV/$** column on signals is the model's in-sample estimate
and runs optimistic; the Research tab's out-of-sample test is the number to trust.

Re-run `pm research` every week or two. The model and the list of approved
categories update from the newest data, and the edges do drift.

## The terminal

Tabs: **Dashboard** (equity, cash, exposure, P&L, win rate, P&L curve, open positions) ·
**Signals** (every opportunity in the last scan, with model probability, fee and EV) ·
**Positions** · **Tickets** (Robinhood mode) · **History** · **Strategy P&L**
(by strategy and by category) · **Research** (out-of-sample backtest, naive baselines,
calibration table) · **Log**.

Keys: `F5` scan now · `F6` pause/resume · `F8` kill switch · `Ctrl+Q` quit.

Commands (type in the bottom bar):

```
quote KXHIGHNY-26NOV16-B52.5        bid/ask + model probability + EV for both sides
buy  KXHIGHNY-26NOV16-B52.5 no 10   manual trade through the current broker (optional price)
close 12                            sell open position #12 into the bid
fill 7 0.91 20                      confirm Robinhood ticket #7 filled 20 @ 0.91
reject 7                            discard ticket #7
set min_edge 0.05                   any config key; saved to data/config.json
mode robinhood                      switch execution back-end
pause / resume / kill / unkill / scan / config / help
```

## Strategies

Default: `["maker", "arb"]`. Enable others with `set strategies maker,arb,calibration`.

0. **maker** (default): `pmagent/strategies/maker.py`. Posts limit bids one cent
   inside the best bid (or joins it) wherever the maker calibration model says
   *filled* bids at that price have historically won more than they cost. Fills
   are adverse-selected, and because the model is fit on actual filled maker
   prints, that is built into the estimate. Unfilled remainder is cancelled after
   `maker_ttl_min` (30). In paper mode, fills are simulated from the real trade
   prints that follow your order, honoring queue position: contracts already
   resting at your price must trade first. In Robinhood mode it produces LIMIT
   tickets.
1. **calibration** (off by default; lost money out-of-sample): `pmagent/strategies/calibration.py`. Learns the real win
   rate of a contract from its price, side (YES/NO), category and time to close,
   using tens of thousands of settled Kalshi trades. It buys when
   `(p_win − price − fee) / (price + fee) ≥ min_edge`. This captures the
   favorite-longshot bias documented on Kalshi (Bürgi, Deng & Whelan 2025): cheap
   contracts win less often than their price implies. Estimates use hierarchical
   shrinkage, so thin categories lean on the global estimate instead of noise.
   Only categories that were profitable on a held-out *validation* period trade.
2. **arb**: `pmagent/strategies/arb.py`. On mutually-exclusive events, buy NO on
   every outcome when the total cost plus fees is below N−1 (guaranteed payout ≥ N−1).
   YES baskets are allowed only on complete numeric ladders. These are rare and
   brief, and every leg is sized to the thinnest book.

## Risk controls (`pmagent/risk.py`)

* ¼-Kelly sizing, capped at 1% of bankroll per trade and 3% per event (outcomes in one event are correlated)
* total open exposure ≤ 60% of equity; cash can't go negative
* daily loss limit (5%) halts new orders until the next day
* kill switch: `kill` / F8 / creating the file `data/KILL`
* one position per market; Robinhood tickets expire after 30 minutes

## Live Kalshi execution

```bash
export KALSHI_KEY_ID=...                     # from kalshi.com → Account → API keys
export KALSHI_PRIVATE_KEY_PATH=~/kalshi.pem
pm cmd mode kalshi                           # kalshi_env defaults to "demo"
```

Order placement uses Kalshi's documented v2 `POST /portfolio/orders`. I couldn't
test it against a live account from this environment, so place a 1-contract
order on the demo exchange and check the fill in Kalshi's UI before setting
`kalshi_env` to `prod`.

## Files

```
pmagent/kalshi.py       API client (rate-limited, RSA-PSS signing)
pmagent/research.py     settled-market + trade collection → data/research.db
pmagent/edge.py         calibration model, fee-aware walk-forward backtester, event bootstrap CIs
pmagent/analysis.py     train / validate / test pipeline → data/model.json, data/research_report.json
pmagent/strategies/     maker quotes, calibration edge (taker), basket arbitrage
pmagent/risk.py         sizing + limits + kill switch
pmagent/broker.py       paper / Robinhood tickets / Kalshi live; settlement
pmagent/agent.py        scan → signal → risk → execute → settle loop
pmagent/tui.py          trading terminal
data/agent.db           every signal, order, fill, settlement, equity snapshot, log line
```
