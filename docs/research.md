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

- **Zerodha Kite Connect.** Personal API free but no market data. Kite Connect at ₹500 per month per app includes live WebSocket and historical candles (the separate ₹2,000 historical fee was dropped in 2025). No paper or sandbox environment. Access tokens expire daily. Historical API rate limit about 3 requests per second. Depth of daily history and adjustment were verified on 2026-10-21; see the section below: history goes back to the early 2000s and candles are adjusted.
- **SEBI retail algo framework, in force from April 2026.** API orders must originate from a static IP whitelisted with the broker (one or two per client, changeable about weekly). Orders need standard API tagging. Strategies at or under 10 orders per second per exchange need no exchange registration; above that, registration through the broker. Order records retained five years. Our system places a handful of orders a day.
- **Delivery costs.** STT 0.1% each side, stamp duty 0.015% on buys, exchange charge about 0.003%, SEBI ₹10 per crore, GST 18% on charges, DP about ₹16 per scrip on sell. Round trip about 0.3% before slippage at a zero-brokerage broker.
- **Pattern-day-trader minimum removal (US, June 2026)** is irrelevant here; Indian intraday has no such rule but is excluded on cost grounds anyway.
- **Alternative brokers.** Upstox has a sandbox; Dhan charges about ₹499 per month for data; Angel One and Fyers have free APIs. The Broker Adapter interface keeps a switch possible.

## Kite daily history depth and adjustment

Verified on 2026-10-21 from live Kite Connect responses through `ingest candles
--interval day` (recorded; the test fixtures under `tests/fixtures/ingest_candles/`
are from the same session) and compared with NSE's official end-of-day files
through the query layer.

| Question | Answer | Checked on |
|---|---|---|
| How far back does Kite daily history go for NSE equities? | INFY from 2003-01-01, RELIANCE from 2000-01-03 (5,869 and 6,664 bars to 2026-10-21). Depth varies by instrument and is well over the ten years Phase 1 needs. | 2026-10-21 |
| Are Kite candles adjusted for corporate actions? | **Yes.** Bonuses and splits are adjusted in price and volume; a dividend of about 2 percent of the price was adjusted in price only, an ordinary dividend was not. | 2026-10-21 |
| How does Kite round an adjusted price? | To the 5 paisa tick, ties to the even tick. Checked on RELIANCE's 43 bars of July and August 2024 across its 1:1 bonus: 172 prices, 55 of them exact ties, all reproduced; rounding to the paisa or ties away from zero does not. `tradeline.adjust` uses this rule. | 2026-10-21 |
| And below Rs 250, where NSE's tick has been 1 paisa since 2024-06-10? | **Unverified.** No fixture symbol has an adjusted price under Rs 250. The 5 paisa rule is applied there too until a split that lands a price below Rs 250 is compared against Kite's bars. | 2026-10-21 |

Evidence:

- RELIANCE, 1:1 bonus ex 2024-10-28. On 2024-10-25 Kite's close is 1327.85 and
  NSE's official close is 2655.70: exactly half, and Kite's volume is exactly
  double. From 2025-03-03 onward (sampled quarterly to 2026-10-01) Kite and NSE
  agree to the paisa.
- INFY, 1:1 bonus ex 2018-09-04. Kite's bars before the ex-date are halved and
  volume doubled against NSE, as for RELIANCE.
- INFY also carries a uniform factor of 0.9788 on every bar from at least
  2024-10-25 to 2026-04-01 (sampled: 2024-10-25, 2025-03-03, 2025-06-02,
  2025-09-01, 2025-11-03, 2025-12-01, 2026-01-05, 2026-04-01), with volume
  unchanged, and none from 2026-07-01 on. No split or bonus explains it. NSE's
  corporate-action list (ingested by #5) shows one INFY action in that window:
  a dividend of Rs 25 ex 2026-06-10, which is 2.12 percent of the 2026-06-09
  close of 1180.30. Kite left RELIANCE's Rs 10 dividend of 2024-08-19 (0.3
  percent) unadjusted. A hypothesis from these two data points, not a finding:
  Kite follows the index convention of adjusting only dividends of 2 percent or
  more of the price. We adjust no dividends (#5), so our series and Kite's
  chart agree across splits and bonuses and differ by that factor before a
  large dividend.
- The Kite values are stored exactly as returned. `candles_daily` therefore holds
  Kite's adjusted series as of the day it was fetched, not raw prices, and a
  later corporate action will make a refetch disagree with stored bars, which the
  ingest records as anomalies rather than applying.

Consequence: **ADR 0006 is flagged for revision before Phase 2.** It states that
Kite candles are stored raw and unadjusted and that we compute the adjusted
series ourselves. Kite's history is already adjusted, by Kite's rules, which
include large dividends. The decision to revisit is which source is the raw
series, what Kite's bars are used for, and how a resend after a corporate
action is reconciled. #5 took the first part as far as its query needed: the
adjusted daily series (`queries.adjusted_daily_candles`) is computed from NSE's
end-of-day rows, the official unadjusted prices, and Kite's bars serve as the
cross-check (the RELIANCE comparison is a test). The ADR's wording and the role
of `candles_daily` remain to be settled.

## NSE and BSE announcement feeds

Verified on 2026-10-21 from live responses for 23 July 2024 through `ingest
announcements` (fixtures under `tests/fixtures/ingest_announcements/`).

| Question | Answer |
|---|---|
| Where do the filings come from? | NSE: `www.nseindia.com/api/corporate-announcements`, one JSON list per day (545 filings that day). BSE: `api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w`, 50 per page with the day's count in `Table1` (940 filings, 19 pages). |
| Does BSE's API answer a script? | **Only with a browser's TLS handshake.** Every `api.bseindia.com` path returns Akamai's 403 to httpx and curl from an Indian address, whatever the headers; the same request through curl_cffi impersonating Chrome returns 200. `LiveTransport(browser_tls=True)` does this, and `ingest announcements` uses it for both exchanges. NSE answers it too. |
| Which timestamp is official? | Both exchanges state when they received a filing and when they disseminated it (NSE `an_dt` and `exchdisstime`, BSE `News_submission_dt` and `DissemDT`, IST). The dissemination time, when the public could see it, is stored as the official timestamp and is the query layer's as-of cut. They differ by one or two seconds; BSE's own notices have no submission time. |
| What identifies the company? | NSE gives the NSE symbol, name, and ISIN. BSE gives only its scrip code and its company name ("Infosys Ltd"), sometimes blank, with no ISIN, so BSE filings are matched by name alone. Against the full master that day 536 of 545 NSE filings and 619 of 940 BSE filings matched; most of the BSE rest are companies not listed on NSE. |
| Is a real company name ambiguous? | Yes: NSE's master names JISLJALEQS and JISLDVREQS (and two other pairs) identically, so BSE's "Jain Irrigation Systems Ltd" cannot be matched. |

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
