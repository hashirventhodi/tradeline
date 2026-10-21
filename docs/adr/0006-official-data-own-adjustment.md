---
status: accepted
date: 2026-10-21
amended: 2026-10-21
---
# Price history comes from Kite and NSE only, with our own corporate-action adjustment

Kite Connect candles are stored exactly as returned. Splits and bonuses are pulled from NSE's official lists and the Adjusted Series is computed by us, deterministically and auditably. Yahoo Finance's adjusted .NS series was rejected despite being faster to start with: it is unofficial, its adjustment conventions differ from the broker's, and two sources with different conventions make backtest and paper disagree in ways that are hard to trace. Paid vendor history was rejected on budget.

## Amendment (2026-10-21, #5): which series is raw, and which the backtester reads

The original text called Kite's candles "raw and unadjusted". They are not: Kite's daily history is adjusted by Kite's own rules as of the day it is fetched (splits and bonuses in price and volume, and at least one large dividend in price alone; see `docs/research.md`). So:

- **The Raw Series is NSE's end-of-day file rows** (`eod_bhavcopy`): official, unadjusted, inserted once, never modified. They are the only input to the Adjusted Series.
- **Kite's candles** (`candles_daily`) stay stored exactly as returned, as the record of what the broker showed on the day they were fetched. They are the cross-check for our arithmetic (the RELIANCE bonus comparison is a test), and the source of minute bars later, never the input to adjustment.
- **The backtester, the engine, and the tests read the Adjusted Series through `queries.adjusted_daily_candles`** with an As-Of Date, and nothing else. It is computed on every read from the Raw Series and the Adjustment Factors in `corporate_actions`, is never written back, and applies only Corporate Actions whose Ex-Date is on or before the As-Of Date. Bars after the As-Of Date are refused.
- Splits (either direction) and bonuses are adjusted. Rights issues and dividends are recorded and flagged, not adjusted. Demergers and other schemes are recorded only (follow-up issue #15).
- Adjusted prices are rounded to the 5 paisa tick with ties to the even tick, the rule that reproduces Kite's bars exactly above Rs 250. Below Rs 250, where NSE's tick has been 1 paisa since June 2024, the broker's rounding is unverified and the same rule is applied until evidence says otherwise.

Alternatives rejected: adjusting Kite's bars a second time (double-counts every action Kite applied); storing the adjusted series (an audit cannot then tell raw from derived, and a new action would require rewriting history).
