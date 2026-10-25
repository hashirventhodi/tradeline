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

Every Limit above is declared on or off, with its value, in each family's Protocol, and the engine refuses a Protocol that leaves one unstated (ADR 0020). The table above is `pullback_v1`'s. The Risk Engine's Limits (`tradeline.risk.Limits`) are the rows from risk per trade to min reward to risk without the two Halts, which the Protocol states apart; the order types, short selling and derivatives are fixed in code, not declared. A Limit off is written as null in the Protocol and skipped by the Risk Engine. Stop required is written only when off: the Protocols frozen before ADR 0020, `pullback_v1`'s `41e47fd39af3` among them, always required a stop without naming it, so its absence means on and they keep their hashes. Risk per trade and min reward to risk are read off the stop, so they can be on only while a stop is required; with them off a portfolio Plan, an entry and its caller's size, is checked against the rest. For `lowvol`:

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
| Relative Drawdown Halt | the worst Relative Drawdown of the NIFTY100 Low Volatility 30 TRI against the NIFTY 500 TRI, 2005-04 to 2019-09, rounded up to the next 5 points (ADR 0020): 25.29% (2006-08-18 to 2008-01-14), so 30%, from niftyindices.com's history read 2026-10-25 |

`lowvol` sizing: at a Rebalance, leavers are sold at the Rebalance day's open, and entrants split all the cash then held equally at the next trading day's open, each at most 15% of Equity; any residual stays in cash, and cash as a share of Equity is reported at every Rebalance. Continuing names are never trimmed, and the largest weight any name reached is reported. Each sale is an after-market limit sell at the decision day's close less the Protocol's band, sent again each day the open does not fill it; each buy is a limit buy at the sizing day's close plus the band, rounded up to the tick, and its equal share of cash buys as many shares as fit at that limit with the buy's charges, so the account never spends cash it does not hold. `lowvol_v1`'s band is 1 percent (`[lowvol.protocol]`): a buy goes unfilled only on an open more than 1 percent over the close, and sizing at the limit leaves up to about 1 percent of each buy in cash. The Initial Buy is decided by a Run on the trading day before the account's first, from that day's close, and is not a day of the account's curve.

A family's Limits are configuration too: `[risk]` is `pullback_v1`'s and `[lowvol.risk]` `lowvol_v1`'s, each Limit a value or `false` when off; `tradeline protocol freeze --strategy lowvol_v1` freezes `[lowvol.risk]` with `[lowvol.protocol]`.

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

The schedule is `tax_rates.csv` (`[tax]` in `tradeline.toml`, with the dividend rate), each rate citing where it is enacted, from 2018-04-01. A Protocol that keeps tax (`[lowvol.protocol] tax = true`) freezes the rates in force on its freeze day, the dividend rate, the Benchmark's funds, the factor fund and the fund-unit rates. Its accounts record each day's Liquidation Value and the Benchmark's, bought for the starting Equity on the account's first day, and the report lists every tax debit and the losses a liquidation drops. A Boundary Exit settles the final partial year in cash; an account that ends without one (the Holdout, a Fresh Period) is judged on its last day's Liquidation Value.

## Halt

Triggered by the Operator or by the daily loss or drawdown Limit. Cancels pending Orders, blocks new entries, keeps managing exits. Cleared by the Operator after review; a drawdown Halt also requires a written note in the Ledger.

A backtest never Halts: both Halts need the Operator to clear them, and a backtest has no honest way to simulate that decision. It reports every day either Limit would have fired, with the drawdown at that point, beside the Gate outcome.

For `lowvol` the daily-loss and drawdown Halts are off (`false` in `[lowvol.risk]`, null in the Protocol) and the Relative Drawdown Halt is on (`relative_drawdown_halt = true`): it fires on a day whose Equity at the close over the Benchmark's NAV as of that day has fallen by at least the threshold from that ratio's running peak, before tax and with dividends counted. The threshold is not configured. `tradeline ingest index-history` stores niftyindices.com's Total Return Index history of both indices, and `tradeline protocol freeze --strategy lowvol_v1` computes the threshold from it and freezes the threshold, the worst Relative Drawdown it was rounded up from with its peak and trough days, the source, the backfill note, each index's range, count and hash of its closes, and the Benchmark's funds. Only with `--relative-drawdown-fallback`, and only while no index history is stored, does it freeze the white paper's worst calendar-year shortfall instead, marked as a fallback. The Walk-Forward report and the full report list every day the Halt would have fired, with its Relative Drawdown, after the Strategy (the full report: after the Gate). Under Halt a Rebalance still decides and sells its leavers and places no buy; an Initial Buy places none either (`engine.run`'s `halted`, for paper in Phase 3).

## Flatten

Operator only. Typed confirmation. Closes every Position at market and enters Halt.

## Live mode

Off by default. Turned on by editing server configuration, not from any UI. Requires Pass plus Approval per Order until the Autonomy Gate passes. The Kite Broker refuses Orders without both.

## Gates

See ADR 0015. For `lowvol` and later portfolio families the Backtest Gate is against the Benchmark after tax and the Paper Gate is fidelity (ADR 0015 amendment of 2026-10-25). `pullback_v1`'s Backtest Gate: Sharpe > 1.0 after costs, max drawdown < 15% and profit factor > 1.3 on both the Stitched Curve and the Holdout; ≥ 150 trades on the Stitched Curve, the Holdout's count reported. Sharpe is the mean of the Equity curve's daily excess returns over the Protocol's risk-free rate (default 6.5% a year; idle cash earns nothing), over their standard deviation, times √252. Max drawdown is the largest peak-to-trough fall of the daily marked curve. Profit factor is gross profit of winning trades over gross loss of losers, after costs. Profit factor with winning trades and no losing one is unbounded (∞ in the report) and clears its threshold; with no closed trade it is not established and fails, as Sharpe does on a curve whose returns do not vary. Every curve starts at the starting Equity, so the first day's return counts. Expectancy is mean net rupee P&L per closed trade, also as a fraction of Equity at entry.

The full report (`tradeline backtest report --protocol P`) writes JSON and Markdown. The JSON holds no backtest id and no timing, so two runs of one Protocol over the same data write byte-identical JSON; the runtime of each stage (the Ledger's first write to its last, pauses included) is in the Markdown only. Paper Gate: 90 trading days, Expectancy within 30% of backtest, slippage within model, zero Limit breaches. Live Gate and Autonomy Gate: 90 days each with the same checks. AI Promotion Gate: ≥ 200 shadow decisions, vetoed trades show significantly lower Expectancy than kept trades (p < 0.05), confidence calibrated.
