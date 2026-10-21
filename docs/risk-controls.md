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
| Min Order value | ₹5,000 | avoids fee-dominated trades |
| Daily loss halt | 3% of start-of-day equity | realised plus unrealised |
| Drawdown halt | 10% from equity peak | triggers Halt and requires Operator review to clear |
| Max new entries per Run | 4 | throttles a screen that fires on everything |
| Stop required | yes | a Plan without a stop is Blocked |
| Min reward to risk | 1.5 | (target − entry) / (entry − stop) |
| Allowed order types | limit, after-market | market orders only through Flatten |
| Short selling | off | long-only cash equities |
| Derivatives | off | out of scope |

Checks run in Plan order as if each Pass had filled, so Plans that are fine alone but breach together are caught.

## Sizing

Shares = floor(equity × risk_per_trade / (entry − stop)), then reduced to satisfy per-Position, sector, and gross Limits. No fractional shares exist on NSE. If the result is below the minimum Order value the Plan is Blocked, not rounded up.

## Cost model (Indian equity delivery, zero-brokerage broker)

| Item | Rate |
|---|---|
| STT | 0.1% of value, buy and sell |
| Stamp duty | 0.015% of value, buy only |
| Exchange transaction charge | about 0.003% of value, both sides |
| SEBI turnover fee | ₹10 per crore |
| GST | 18% on brokerage plus exchange and SEBI charges |
| DP charge | about ₹16 per scrip per sell day |
| Slippage | half the quoted spread plus an impact term from Order value relative to average daily traded value |

A round trip on a liquid large cap costs roughly 0.3% of value before slippage. Every backtest and paper result is after these costs.

## Halt

Triggered by the Operator or by the daily loss or drawdown Limit. Cancels pending Orders, blocks new entries, keeps managing exits. Cleared by the Operator after review; a drawdown Halt also requires a written note in the Ledger.

## Flatten

Operator only. Typed confirmation. Closes every Position at market and enters Halt.

## Live mode

Off by default. Turned on by editing server configuration, not from any UI. Requires Pass plus Approval per Order until the Autonomy Gate passes. The Kite Broker refuses Orders without both.

## Gates

See ADR 0015. Backtest Gate: OOS Sharpe > 1.0 after costs, max drawdown < 15%, ≥ 150 trades, profit factor > 1.3. Paper Gate: 90 trading days, Expectancy within 30% of backtest, slippage within model, zero Limit breaches. Live Gate and Autonomy Gate: 90 days each with the same checks. AI Promotion Gate: ≥ 200 shadow decisions, vetoed trades show significantly lower Expectancy than kept trades (p < 0.05), confidence calibrated.
