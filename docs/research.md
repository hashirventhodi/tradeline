# Research and feasibility

Findings from the planning session on 2026-10-21. These shaped the ADRs. Verify prices and rules against primary sources before relying on them; vendor blogs were the only sources for several items.

## What has been demonstrated

- **LLM agents trading profitably out of sample: not demonstrated.** TradingAgents (arXiv 2412.20138), the best-known multi-agent LLM trading paper, backtests five mega-cap US stocks over three months of 2024 and reports beating simple baselines. The no-lookahead claim is self-reported, the window is tiny, and the model's training data overlaps the test period.
- **Lookahead contamination is measured.** Point-in-time benchmarks (arXiv 2512.23847, arXiv 2601.13770) show LLM backtest returns collapse once the test period passes the model's knowledge window. Any backtest of an LLM component on a period before its training cutoff is contaminated by default. Our mitigation: the AI layer is evaluated only in Shadow Mode on live data, never on historical backtests.
- **Live results are poor.** In nof1's Alpha Arena (late 2025), six frontier models each traded real capital for about 17 days with identical prompts; four of six lost money and a follow-up study found general model ranking has almost no relation to trading results.
- **Most retail active traders lose money.** This is the base rate the system starts from.

## What is feasible

Every component in the architecture exists in some form today: event-driven backtesters, broker APIs with order placement, structured-output LLM calls, deterministic risk engines, Telegram approval flows. The engineering is routine. The edge is the open question, and the Gates are how it is answered.

## India-specific findings

- **Zerodha Kite Connect.** Personal API free but no market data. Kite Connect at ₹500 per month per app includes live WebSocket and historical candles (the separate ₹2,000 historical fee was dropped in 2025). No paper or sandbox environment. Access tokens expire daily. Historical API rate limit about 3 requests per second; depth of daily history unconfirmed in official docs. Candles are believed unadjusted for corporate actions (forum evidence, unconfirmed).
- **SEBI retail algo framework, in force from April 2026.** API orders must originate from a static IP whitelisted with the broker (one or two per client, changeable about weekly). Orders need standard API tagging. Strategies at or under 10 orders per second per exchange need no exchange registration; above that, registration through the broker. Order records retained five years. Our system places a handful of orders a day.
- **Delivery costs.** STT 0.1% each side, stamp duty 0.015% on buys, exchange charge about 0.003%, SEBI ₹10 per crore, GST 18% on charges, DP about ₹16 per scrip on sell. Round trip about 0.3% before slippage at a zero-brokerage broker.
- **Pattern-day-trader minimum removal (US, June 2026)** is irrelevant here; Indian intraday has no such rule but is excluded on cost grounds anyway.
- **Alternative brokers.** Upstox has a sandbox; Dhan charges about ₹499 per month for data; Angel One and Fyers have free APIs. The Broker Adapter interface keeps a switch possible.

## Kite daily history depth and adjustment

**Status on 2026-10-21: not yet verified from real responses.** `login` and
`ingest candles --interval day` exist (issue #4), but a Kite access token needs
the Operator's interactive Session Login, so the implementing session could not
capture a live response. Both answers must be recorded here before Phase 2
starts; if candles turn out to be adjusted, ADR 0006 is revisited first.

| Question | Answer | Checked on |
|---|---|---|
| How far back does Kite daily history go for NSE equities? | pending | |
| Are Kite candles adjusted for corporate actions? | pending (forum evidence says unadjusted; unconfirmed) | |

Procedure, about ten minutes once the day's login is done:

1. `uv run tradeline login`, open the printed URL, paste the `request_token` back.
2. Depth: `uv run tradeline ingest candles --interval day --symbol RELIANCE --symbol INFY --from 1995-01-01 --to <today> --record-fixtures tests/fixtures/kite_depth`.
   The earliest `trade_date` that `queries.daily_candles` returns for each symbol
   is the depth; record the date and whether the two symbols agree.
3. Adjustment: RELIANCE issued a 1:1 bonus with ex-date 2024-10-28. Run
   `uv run tradeline ingest eod --from 2024-10-25` and compare the NSE end-of-day
   close for 2024-10-25 (`queries.end_of_day`) with Kite's bar for the same day
   (`queries.daily_candles`, loaded in step 2). Equal closes mean Kite candles are
   raw; a Kite close of about half the NSE close means Kite adjusts for bonuses and
   splits, and ADR 0006 must be revised before Phase 2.
4. Fill in the table above with the date checked, and replace the synthesised
   fixtures under `tests/fixtures/ingest_candles/` with a real recording (see the
   README there).

## US-market findings (for reference; not the chosen market)

Alpaca offers free paper trading with IEX data and ₹99-equivalent full SIP data; Massive (formerly Polygon) runs 29 to 199 dollars a month; SEC EDGAR is free. These made the US the easier engineering choice. The Operator chose Indian equities, which cost more in friction (no sandbox, daily login, static IP) but is the market they trade.

## Model pricing used in cost estimates (Anthropic, October 2026)

| Model | Input per MTok | Output per MTok |
|---|---|---|
| Claude Opus 5.5 | $4 | $20 |
| Claude Haiku 5.5 | $0.10 | $0.50 |

Prompt caching and the Batch API (half price) apply. Estimated AI spend at ten Candidates a day: about ₹2,000 a month.

## Monthly running cost estimate

| Item | ₹ per month |
|---|---|
| Kite Connect | 500 |
| AWS t4g.small, Elastic IP, S3 | 2,000 to 2,500 |
| Anthropic API | 1,500 to 3,000 |
| Telegram, Sentry free tier, uptime ping | 0 |
| Total | 4,000 to 6,000 |

Within the agreed ₹5,000 to ₹12,000 band with headroom for a paid data upgrade later.

## Sources

- Zerodha pricing: zerodha.com/products/api, support.zerodha.com (Kite Connect sign-up article), marketcalls.in on the 2025 pricing change
- SEBI algo rules: inthemoneybyzerodha.substack.com (April 2026 changes), tradejini.com, algobulls.com, icicidirect.com FAQ
- Delivery charges: nseindia.com first-time-investor levies page, groww.in STT explainer, stockcalc.in Zerodha 2026 breakdown
- TradingAgents: arxiv.org/abs/2412.20138
- Lookahead bias: arxiv.org/pdf/2512.23847, arxiv.org/pdf/2601.13770
- Alpha Arena: protos.com coverage, iweaver.ai season 1 results
- US reference: docs.alpaca.markets market data plans, massive.com pricing (via third-party guides)
