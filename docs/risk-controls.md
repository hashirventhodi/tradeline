# Risk controls

All numbers are configuration with these defaults. The illustrative paper account is ₹5,00,000. None of this is financial advice.

## Limits (enforced by the Risk Engine, code only)

| Limit | Default | Checked against |
|---|---|---|
| Risk per trade | 1.0% of equity at the stop | Plan size × (entry − stop) |
| Max open Positions | 8 | existing Positions plus Passed Plans this Run |
| Max per Position | 15% of equity | notional at entry |
| Max per sector | 30% of equity | NSE industry classification |
| Max gross exposure | 100% of equity | no leverage, ever |
| Participation Cap | 1% of the 20-day average daily traded value | Order value; keeps a backtest's fills ones the market could have given as capital grows |
| Min Order value | ₹5,000 | avoids fee-dominated trades |
| Daily loss halt | 3% of start-of-day equity | realised plus unrealised |
| Drawdown halt | 10% from equity peak | triggers Halt and requires Operator review to clear |
| Max new entries per Run | 4 | throttles a screen that fires on everything |
| Stop required | yes | a Plan without a stop is Blocked |
| Min reward to risk | 1.5 | (target − entry) / (entry − stop) |
| Allowed order types | limit, after-market | market orders only through Flatten |
| Short selling | off | long-only cash equities |
| Derivatives | off | out of scope |

Checks run in Plan order as if each Pass had filled, so Plans that are fine alone but breach together are caught. The Strategy ranks its Plans deterministically (`pullback_v1`: 126-trading-day return descending, ties by Symbol), so the first Plans to Pass are the same on every run.

The sector Limit reads NSE's classification as of the Run. A day before the first sector load reads the earliest load; an Instrument with none is in the Unclassified sector, which the 30% Limit applies to like any other. In paper and live, a Universe member in Unclassified raises a stale-sector-map alert.

## Sizing

Shares = floor(Equity × risk_per_trade / (entry − stop)), then reduced to satisfy per-Position, sector, gross and Participation Cap Limits. No fractional shares exist on NSE. If the result is below the minimum Order value the Plan is Blocked, not rounded up. Equity is cash plus open Positions at the day's close. Stop, target and shares round down to the tick in force that day (a dated table of NSE tick sizes), and the minimum reward to risk is re-checked after rounding.

## Cost model (Indian equity delivery, zero-brokerage broker)

| Item | Rate |
|---|---|
| STT | 0.1% of value, buy and sell |
| Stamp duty | 0.015% of value, buy only |
| Exchange transaction charge | about 0.003% of value, both sides |
| SEBI turnover fee | ₹10 per crore |
| GST | 18% on brokerage plus exchange and SEBI charges |
| DP charge | about ₹16 per scrip per sell day, GST included |
| Slippage | a 5 basis point safety margin on value per side, plus an impact term c × (Order value / 20-day average daily traded value) |

Rates are a dated schedule, each with the date it came into force, checked against the broker's published charges when a Protocol is frozen. A backtest applies the schedule as of its Protocol's freeze to every trade, since the question is whether the Strategy clears the costs it will pay from now on; a contract note is checked against the rates in force on its own date. The safety margin is not a spread: entries and exits are limit Orders filled in the pre-open auction, which cross none. A round trip on a liquid large cap costs roughly 0.3% of value before slippage. Every backtest and paper result is after these costs; a backtest report also states its results with c at zero and at twice its value.

## Halt

Triggered by the Operator or by the daily loss or drawdown Limit. Cancels pending Orders, blocks new entries, keeps managing exits. Cleared by the Operator after review; a drawdown Halt also requires a written note in the Ledger.

A backtest never Halts: both Halts need the Operator to clear them, and a backtest has no honest way to simulate that decision. It reports every day either Limit would have fired, with the drawdown at that point, beside the Gate outcome.

## Flatten

Operator only. Typed confirmation. Closes every Position at market and enters Halt.

## Live mode

Off by default. Turned on by editing server configuration, not from any UI. Requires Pass plus Approval per Order until the Autonomy Gate passes. The Kite Broker refuses Orders without both.

## Gates

See ADR 0015. Backtest Gate: Sharpe > 1.0 after costs, max drawdown < 15% and profit factor > 1.3 on both the Stitched Curve and the Holdout; ≥ 150 trades on the Stitched Curve, the Holdout's count reported. Sharpe is the mean of the Equity curve's daily excess returns over the Protocol's risk-free rate (default 6.5% a year; idle cash earns nothing), over their standard deviation, times √252. Max drawdown is the largest peak-to-trough fall of the daily marked curve. Profit factor is gross profit of winning trades over gross loss of losers, after costs. Expectancy is mean net rupee P&L per closed trade, also as a fraction of Equity at entry. Paper Gate: 90 trading days, Expectancy within 30% of backtest, slippage within model, zero Limit breaches. Live Gate and Autonomy Gate: 90 days each with the same checks. AI Promotion Gate: ≥ 200 shadow decisions, vetoed trades show significantly lower Expectancy than kept trades (p < 0.05), confidence calibrated.
