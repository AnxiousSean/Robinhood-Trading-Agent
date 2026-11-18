# Research results (data pulled 2026-11-16)

**Short version: none of the strategies has a proven edge after fees on held-out data.**
Doing 10,000 trades doesn't change that. Volume only makes your results converge
on the true edge, and for the strategies tested that edge is zero or negative.
The agent and terminal are built so you can run the most promising candidate
(maker) in paper mode and see whether it works *going forward* before risking money.

## Data

* 8,334 settled Kalshi markets (3,917 events), 1.15 million trade prints, from
  both the recent API and the historical archive. Sampled at most 25 markets per
  series so a few huge series don't dominate.
* Every print gives a taker observation (paid the ask) and a maker observation
  (resting bid got hit), each with the known outcome.
* Weighting: each market counts once, however many times it traded.
* Splits by event close time: **train** = oldest 50% (fit the model) →
  **validate** = next 25% (pick thresholds and categories) → **test** = newest 25%.
* Backtest trades are sized like the live agent (¼-Kelly, ≤1% of bankroll).
  95% CIs come from resampling whole events.

## What the market looks like (calibration, all data, Robinhood fees)

The favorite-longshot bias is real and large:

| You buy at… | as taker: realized win rate | ROI after fees | as maker: win rate | ROI after fees |
|---|---|---|---|---|
| 1-3¢ | 0.1% | −94.7% | 0.6% | −74.9% |
| 10-15¢ | 4.5% | −67.1% | 8.0% | −37.1% |
| 40-50¢ | 38.4% | −18.9% | 44.8% | −1.7% |
| 60-70¢ | 68.2% | +1.9% | 73.3% | +12.2% |
| 80-85¢ | 87.4% | +3.9% | 91.6% | +10.4% |
| 90-94¢ | 95.5% | +2.5% | 97.4% | +5.2% |

(Equal $ per market. The full table is in the terminal's Research tab.)

Takers lose money on average, longshot buyers lose the most, and makers
bidding for favorites earned money **on average across the whole history**. The
question is whether that holds up *going forward*, and the walk-forward test below
says: not reliably.

## Out-of-sample test

| Strategy | Validation ROI | **Test ROI** | Test 95% CI | Test P&L, % bankroll | Max drawdown |
|---|---|---|---|---|---|
| Taker calibration, Robinhood fees | −2.9% | **−3.8%** | −6.3% … −1.0% | −41% | 46% |
| Taker calibration, Kalshi fees | −3.1% | **−2.1%** | −3.9% … −0.2% | −29% | 38% |
| Maker (limit bids), Robinhood fees | +4.6% | **−1.2%** | −6.4% … +3.4% | −12% | 33% |
| Maker (limit bids), Kalshi-direct fees | +6.2% (CI +0.5% … +11.4%) | **+0.7%** | −4.0% … +6.4% | +8% | 39% |

Naive rules on the test set (equal $ per trade, Robinhood fees):
buy YES longshots ≤10¢ **−29%** · buy YES coin-flips **−6.5%** ·
buy NO on longshots **−3.3%** · buy YES favorites ≥90¢ **−1.4%**.

### Reading this

1. **Buying at the ask loses money**, and the confidence interval excludes zero.
   It's off by default (`calibration` strategy).
2. **Robinhood's $0.01/contract commission kills the maker edge.** On a 60¢
   contract that's 1.7% of the stake, about the size of the edge itself.
3. **The best candidate is maker on Kalshi directly** (no commission; most
   series charge makers nothing). It was statistically significant in validation
   (+6.2%) but only +0.7% on the newer test period. It may have shrunk as more
   market makers joined, or the validation result may have been partly luck.
4. **Almost all signals are Sports**, where most of Kalshi's volume is. Drawdowns
   of 30-45% of bankroll at 1% per bet show how correlated those outcomes are.
5. **Arbitrage**: mutually-exclusive baskets priced below their guaranteed payout
   after fees showed up 0 times in the live scans I ran. Pros take these in milliseconds.
6. **The "EV/$" shown on live signals is the model's in-sample estimate** (often
   +15-20%). The test-set row above is what that kind of signal actually earned
   on unseen data. Trust the test row.
7. Methodology note: I looked at the test set more than once while fixing two
   scoring problems (prices that round to 0/1, and equal-$ weighting letting
   ≤10¢ lottery tickets dominate). Those fixes moved the numbers *toward* zero,
   not away from it. The truly clean test is the agent's live paper record.

## What I'd do with this

1. Run `pm tui` in **paper** mode for 2-4 weeks with the maker strategy. Paper
   fills are simulated from real trade prints with queue position, so the
   History and Strategy P&L tabs will show whether the edge exists *now*.
2. If maker paper ROI is clearly positive across 300+ settled fills, consider
   live trading on **Kalshi directly** (`mode kalshi`, demo first) instead of
   Robinhood: the commission is the difference between break-even and losing.
3. Re-run `pm research` every week or two; edges move.
4. Don't run the taker (`calibration`) strategy with real money unless a fresh
   research run shows a positive test ROI with a CI above zero.

All raw numbers: `data/research_report_robinhood.json`, `data/research_report_kalshi.json`.
