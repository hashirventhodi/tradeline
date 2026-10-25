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

## Limits by family

Every Limit above is declared on or off, with its value, in each family's Protocol, and the engine refuses a Protocol that leaves one unstated (ADR 0020). The table above is `pullback_v1`'s. For `lowvol`:

| Limit | `lowvol` |
|---|---|
| Risk per trade, stop required, min reward to risk, max new entries per Run | off: a portfolio Plan has no stop |
| Max open Positions | 30 |
| Max per Position | 15% of Equity at entry |
| Max per sector | off, as in the NIFTY100 Low Volatility 30; sector weights reported at every Rebalance, with the peak between them |
| Max gross exposure | 100% of Equity |
| Participation Cap | 1% of the 20-day average daily traded value |
| Min Order value | Rs 5,000 |
| Daily loss halt, drawdown halt | off, replaced by the Relative Drawdown Halt |
| Relative Drawdown Halt | the worst Relative Drawdown of the NIFTY100 Low Volatility 30 TRI against the NIFTY 500 TRI, 2005-04 to 2019-09, rounded up to the next 5 points (ADR 0020) |

`lowvol` sizing: at a Rebalance, leavers are sold at the Rebalance day's open, and entrants split all the cash then held equally at the next trading day's open, each at most 15% of Equity; any residual stays in cash, and cash as a share of Equity is reported at every Rebalance. Continuing names are never trimmed, and the largest weight any name reached is reported.

## Sizing

Shares = floor(Equity × risk_per_trade / (entry − stop)), then reduced to satisfy per-Position, sector, gross and Participation Cap Limits. No fractional shares exist on NSE. If the result is below the minimum Order value the Plan is Blocked, not rounded up. Equity is cash plus open Positions at the day's close. The stop and shares round down, the stop to the tick in force that day (a dated table of NSE tick sizes). The target is set at the Plan's own multiple of the risk over the rounded stop's risk and rounded up to the tick, so rounding never takes the reward to risk under the Plan's; it is at most one tick further than that multiple. The minimum reward to risk is re-checked after rounding. Until #85 the target also rounded down, so a Plan at r = 1.5 came out just under the minimum and was Blocked almost every time.

The table is `tick_sizes.csv`, each row citing NSE's circular. It follows the circulars rather than #72's summary of ticks: 5 paisa for every equity to 2024-06-09; from 2024-06-10, 1 paisa below Rs 250 (NSE/CMTR/62174); from 2025-04-15, inside the Holdout, also 10 paisa above Rs 1,000, 50 paisa above Rs 5,000, Re 1 above Rs 10,000 and Rs 5 above Rs 20,000 (NSE/CMTR/67133), bands the summary leaves out. A security's band is picked by its latest close on or before the previous month's end, as NSE fixes it monthly.

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

The schedule is `cost_rates.csv` (`[costs]` in `tradeline.toml`), each rate citing where it is published, from 2023-04-01, the first date every rate could be cited for; a trade before it is refused. STT and stamp duty round to the rupee and the other charges to the paisa, per Fill, as Zerodha's calculator rounds. c is `[costs] impact` (0.1), and a Protocol freezes it with the safety margin and the schedule's rates, so every backtest under the Protocol prices with the c it states; paper prices with the configured c. Real contract notes go in `contract_notes.csv`, each a test against the rates on its trade date; none has been supplied.

## Tax (portfolio families)

From `lowvol` on, the Backtest Gate compares Liquidation Value after tax with the Benchmark's (ADR 0015 amendment). The rates are a dated schedule like costs, applied as of the Protocol's freeze to every trade and recorded in the Protocol:

| Item | Rate |
|---|---|
| Short-term gain on listed equity (held twelve months or less) | 20% plus 4% cess: 20.8% |
| Long-term gain (held more than twelve months) | 12.5% plus 4% cess: 13% |
| Dividends | configured, default 31.2% (the 30% slab plus cess) |
| Surcharge | not modelled; the report says this is optimistic for high incomes |
| Rs 1.25 lakh long-term exemption | not applied, on either side |

Short-term losses set off against either kind of gain, long-term losses against long-term gains only, carried forward eight years. Each Indian financial year's net tax is debited from cash on the first trading day after March 31; liquidation settles the final partial year, and losses left unused are dropped and their size reported. The Benchmark is taxed once, on sale, by the same holding rule. Fund units pay stamp duty on purchase, STT on redemption and any exit load, from the cost schedule.

## Halt

Triggered by the Operator or by the daily loss or drawdown Limit. Cancels pending Orders, blocks new entries, keeps managing exits. Cleared by the Operator after review; a drawdown Halt also requires a written note in the Ledger.

A backtest never Halts: both Halts need the Operator to clear them, and a backtest has no honest way to simulate that decision. It reports every day either Limit would have fired, with the drawdown at that point, beside the Gate outcome.

## Flatten

Operator only. Typed confirmation. Closes every Position at market and enters Halt.

## Live mode

Off by default. Turned on by editing server configuration, not from any UI. Requires Pass plus Approval per Order until the Autonomy Gate passes. The Kite Broker refuses Orders without both.

## Gates

See ADR 0015. For `lowvol` and later portfolio families the Backtest Gate is against the Benchmark after tax and the Paper Gate is fidelity (ADR 0015 amendment of 2026-10-25). `pullback_v1`'s Backtest Gate: Sharpe > 1.0 after costs, max drawdown < 15% and profit factor > 1.3 on both the Stitched Curve and the Holdout; ≥ 150 trades on the Stitched Curve, the Holdout's count reported. Sharpe is the mean of the Equity curve's daily excess returns over the Protocol's risk-free rate (default 6.5% a year; idle cash earns nothing), over their standard deviation, times √252. Max drawdown is the largest peak-to-trough fall of the daily marked curve. Profit factor is gross profit of winning trades over gross loss of losers, after costs. Profit factor with winning trades and no losing one is unbounded (∞ in the report) and clears its threshold; with no closed trade it is not established and fails, as Sharpe does on a curve whose returns do not vary. Every curve starts at the starting Equity, so the first day's return counts. Expectancy is mean net rupee P&L per closed trade, also as a fraction of Equity at entry.

The full report (`tradeline backtest report --protocol P`) writes JSON and Markdown. The JSON holds no backtest id and no timing, so two runs of one Protocol over the same data write byte-identical JSON; the runtime of each stage (the Ledger's first write to its last, pauses included) is in the Markdown only. Paper Gate: 90 trading days, Expectancy within 30% of backtest, slippage within model, zero Limit breaches. Live Gate and Autonomy Gate: 90 days each with the same checks. AI Promotion Gate: ≥ 200 shadow decisions, vetoed trades show significantly lower Expectancy than kept trades (p < 0.05), confidence calibrated.
