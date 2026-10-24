---
status: accepted
date: 2026-10-21
amended: 2026-10-24
---
# Paper trading is an internal Paper Broker, not a broker sandbox

Zerodha Kite Connect has no paper or sandbox environment. Paper trading therefore runs through our own Paper Broker, which fills Orders against real Kite quotes using a conservative fill model (an after-market limit order fills only if the opening price trades through the limit), slippage from half the quoted spread plus an impact term from traded value, and the full Indian delivery cost stack (STT both sides, stamp duty on buys, exchange and SEBI charges, GST, DP charge on sells). The Paper Broker implements the same Broker Adapter interface as the Kite Broker so that promotion to live changes one configuration value.

## Amendment (2026-10-24, #72): the fill model against historical bars

Phase 2's backtest fills against daily bars, where the only prices known are the open, high, low and close, and the live system's Orders are limit and after-market Orders only (`docs/risk-controls.md`). So:

- **An entry fills at the next open, or not at all.** The entry is an after-market limit at the Run day's close; it fills at the open if the open is at or below the limit, at the open (NSE's pre-open auction fills a buy at or above the auction price at that price). Otherwise it is cancelled, and the Strategy may plan again next Run. A limit that stays live all day was rejected: a buy filled on an intraday dip is too often the trade that keeps falling.
- **Exits trigger on the completed bar and fill at the next open through a protective band.** A stop triggers when the day's low is at or below it, a target when the high is at or above it, a time stop when the Plan's holding period has passed since the Fill; when stop and target trigger on one bar the exit is recorded as a stop. The exit is an after-market limit sell at the close minus a band fixed in the Protocol (5 percent), never a grid parameter, or Walk-Forward would tune it to past gaps. It fills at the open if the open is at or above that limit, at the open. An open below the band leaves the Position open and the exit is re-sent next Run. A market-on-open sell was rejected: risk-controls allows market Orders only through Flatten.
- **A Circuit Lock fills nothing.** A bar with open, high, low and close equal at the price band is a day with no counterparty; the exit is re-sent next Run. Daily bars are enough to detect it.
- **Every Gapped Stop is reported** with its realised loss, since each broke the Plan's one percent risk assumption.
- **Slippage is a safety margin plus impact**, not a spread: 5 basis points of value per side, and c × (Order value / 20-day average daily traded value). A Participation Cap (`docs/risk-controls.md`) keeps the Order a fraction of that traded value, so the fill assumed stays one the market could have given as capital grows. The report states results with c at zero and at twice its value.
- **A Boundary Exit** closes every open Position at the last out-of-sample day's close where the Holdout begins, with costs. The boundary is ours and not the market's, so it carries no gap; the close, not the next open, is used.
- **Paper mode in Phase 2 uses the same model** against the next day's end-of-day open; live Kite quotes replace that source in Phase 3.

Open on #72 before the build: whether a resting limit sell at the target, filling intraday at the target, should be the one exception to "exits fill at the next open". Without it the backtest undercounts target exits while modelling stops faithfully.
