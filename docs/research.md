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
| Does BSE's API answer a script? | **Only with a browser's TLS handshake.** Every `api.bseindia.com` path returns Akamai's 403 to httpx and curl from an Indian address, whatever the headers; the same request through curl_cffi impersonating Chrome returns 200. `BrowserTransport` does this, and `ingest announcements` uses it for both exchanges. NSE answers it too. |
| Which timestamp is official? | Both exchanges state when they received a filing and when they disseminated it (NSE `an_dt` and `exchdisstime`, BSE `News_submission_dt` and `DissemDT`, IST). The dissemination time, when the public could see it, is stored as the official timestamp and is the query layer's as-of cut. They differ by one or two seconds; BSE's own notices have no submission time. |
| What identifies the company? | NSE gives the NSE symbol, name, and ISIN. BSE gives only its scrip code and its company name ("Infosys Ltd"), sometimes blank, with no ISIN, so BSE filings are matched by name alone. Against the full master that day 536 of 545 NSE filings and 619 of 940 BSE filings matched; most of the BSE rest are companies not listed on NSE. |
| Is a real company name ambiguous? | Yes: NSE's master names JISLJALEQS and JISLDVREQS (and two other pairs) identically, so BSE's "Jain Irrigation Systems Ltd" cannot be matched. |

## Kite minute history depth

Probed on 2026-10-21 from live Kite Connect minute responses (not recorded,
except the two days under `tests/fixtures/ingest_minute_candles/`).

| Question | Answer | Checked on |
|---|---|---|
| How far back does Kite minute history go? | **2015-02-02** for INFY, RELIANCE and TCS: a request for 2015-01-01 to 2015-02-28 starts at 2015-02-02 09:15, and any earlier window (2010, 2014, December 2014 to January 2015) returns no bars. About eleven years and eight months to 2026-10-21. | 2026-10-21 |
| What span does one minute request accept? | 61 days inclusive of both ends (1 July to 30 August 2024 answered; to 31 August refused with `InputException: interval exceeds max limit: 60 days`). The ingest asks for 60 inclusive. | 2026-10-21 |
| Is a date-only `to` inclusive of that day? | Yes: `from=2024-07-31&to=2024-08-01` returns 750 bars, 375 a day, 09:15 to 15:29 IST, each stamped with its start minute and `+0530`. | 2026-10-21 |
| Are minute bars adjusted? | **Yes, like daily bars.** INFY's 1 July 2024 09:15 open is 1526.5, Kite's adjusted daily open for that day; February 2015 bars are at a quarter to a half of the prices then quoted. Some adjusted minute prices are not on any tick (TCS 2015-02-02 09:15 high 1243.63). `candles_minute` therefore holds Kite's adjusted series as of the fetch, as `candles_daily` does. | 2026-10-21 |

## Ten-year backfill and acceptance run (#13)

Run on 2026-10-21 against live NSE and Kite responses, on a laptop against the
Compose Postgres. Universe months are November 2016 to October 2026 (120); NSE
end-of-day files were loaded from 2015-01-01 so that the first month has its
126-day window and every symbol's listing history before it.

| Step | Result | Runtime |
|---|---|---|
| `ingest eod --from 2015-01-01 --to 2026-10-20` | 2,923 trading days, 6,483,258 rows, 7,653 symbols, 0 anomalies. 167 weekdays without a file (exchange holidays, about 14 a year), 11 weekend sessions. | 23 min (two runs; see below) |
| `ingest corporate-actions --from 2015-01-01 --to 2026-10-21` | 26,264 actions in 12 requests: 412 splits, 443 bonuses, 291 rights, 15,611 dividends, 9,507 other. | 20 s |
| `universe --month 2016-11 --to 2026-10`, twice | 120 months of 200 members; 544 distinct symbols. The second run recomputed every month and found 0 differences. | 174 s, 167 s |
| `ingest candles --interval day --from 2016-10-21 --to 2026-10-20` for the 454 ever-members with a Kite token | 978,885 bars, 908 requests, 0 anomalies. **Partial**: 454 of 544 ever-members; the other 90 have no Kite token (below). | 5 min |
| `reconcile --from 2016-10-21 --to 2026-10-20` | 985,902 rows, 87,502 discrepancies (8.88 percent): **fail** against 0.1 percent. Causes below. | 7 s |
| `compare-adjusted` for 20 symbols with splits or bonuses | 26,310 of 48,699 bars equal to the paisa; IRCTC alone matches on every bar: **fail**. Every mismatch is explained below. | 3 s |
| `verify-universe` (October 2026) | 154 of 200 NIFTY 200 constituents (77 percent): **fail** against 85 percent. 0 members without a sector. | 2 s |

### What the run found

- **A legacy file with a two-digit year.** NSE's file for 2020-07-13 writes
  `TIMESTAMP` as `13-Jul-20`; the parser now reads both. The first run stopped
  there (recorded as a failed `nse_eod` run) and was resumed from 2020-07-11.
- **A split subject without "From".** GRANULES ex 2015-03-23, "Face Value Split
  Rs 10 To Rs 1", is now read as a split. MONNETISPA's 2018 "Capital Reduction
  Rs 10 To Rs 3.30 / Consolidation Rs 3.30 To Rs.10" stays unparsed on purpose.
- **The Universe query was quadratic in history.** Postgres inlined the
  trading-calendar CTE into a per-symbol count, so a month took about 90
  seconds with ten years loaded. Materialized, it takes under two.
- **90 of the 544 ever-members have no Kite token today.** 38 were renamed
  (ZOMATO to ETERNAL, LTI to LTIM to LTM, TATAMOTORS to TMPV);
  Kite serves the whole history under the new symbol's token. 52 were delisted,
  merged (HDFC, MINDTREE, GRUH, the merged PSU banks) or now trade only in BE
  or BZ. NSE's end-of-day rows cover all 544 for every month they were members,
  so the Universe and the adjusted series (both computed from NSE's rows) are
  complete; Kite's candles, the cross-check, are not.

### Why reconcile fails

| Cause | Discrepancies |
|---|---|
| **Renames.** Kite files a renamed instrument's whole history under the new symbol; NSE's rows before the rename carry the old one, so every earlier bar is "extra" (36 symbols, LTM 2,318 bars, TMPV 2,231, ...). | 44,408 extra |
| **Series changes.** Kite's EQ token carries on through days NSE lists the symbol in BE, BZ, or another series. | 11,685 extra |
| **Kite's additive adjustment.** For 19 symbols (GNFC, HINDPETRO, BANKBARODA, CANBK, IOC, UNIONBANK, OFSS, VEDL, TATASTEEL, MPHASIS, M&MFIN, GAIL, BPCL, ASHOKLEY, TECHM, HEROMOTOCO, LICHSGFIN, POWERGRID and, in part, NMDC), Kite's bars before an action are NSE's times a factor *minus a constant*: GNFC before September 2023 is 0.93205 × NSE − 43.35, OFSS before May 2024 0.942 × NSE − 226.09, fitted to within a tick on every bar. Reconcile's model is a factor per stretch, so the ratio drifts with the price and the closes mismatch. Adjustments Kite has made since about mid-2024 are purely multiplicative. | about 25,500 close |
| **Kite rounds adjusted history to a coarser tick.** After their 2025 and 2026 splits and bonuses, Kite rounds the earlier history of MCX, BSE, BAJFINANCE, TATAINVEST, GODFRYPHLP and TDPOWERSYS to whole rupees (MCX 253, 236, 292; BAJFINANCE 106, 423), seemingly the tick in force for the instrument when the action happened. On a ₹100 price that is beyond the 0.1 percent tolerance. | about 3,900 close |
| **Muhurat sessions before 2019.** Kite has no bars for the Diwali sessions of 2016-10-30, 2017-10-19, and 2018-11-07. | 935 missing |
| **BAFNAPH.** A BE symbol with July and August 2024 candles from earlier work; reconcile compares every day NSE lists it. | 998 missing |

With renames, series changes, the muhurat days, BAFNAPH, and the additive
adjustment accounted for, about 3,900 discrepancies remain (0.40 percent),
almost all of them the coarse rounding. None suggests an error in NSE's rows.

### How the adjusted series compares with Kite's chart

Kite's stored daily candles stand in for the broker's displayed chart, as in
#5's RELIANCE comparison; the chart itself was not read. The 20 symbols, with
splits and bonuses across the decade: RELIANCE, INFY,
TCS, WIPRO, HDFCBANK, BAJFINANCE, BAJAJFINSV, BEL, EICHERMOT, TATASTEEL,
KOTAKBANK, DRREDDY, BRITANNIA, ICICIBANK, NTPC, GAIL, CONCOR, MCX, BSE, IRCTC.

- **At 30 of the 35 split and bonus Ex-Dates in them, the ratio of Kite's close
  to ours is the same on the five days either side**, so our factor equals
  Kite's. The other five (WIPRO 2019, TATASTEEL 2022, GAIL 2017, 2018 and 2019)
  sit in stretches where the ratio moves with the price, which the
  scale-and-offset fit above describes; that is a fit to the data, not a rule
  Kite has stated.
- 26,310 bars are equal to the paisa.
- 5,636 differ only in rounding, by at most 10 paisa: Kite rounds its older
  adjusted prices to the paisa (TCS before its 2018 bonus: 1199.125 to
  1199.13), to 10 paisa (HDFCBANK, KOTAKBANK), or to 5 paisa with ties down
  (EICHERMOT 2477.975 to 2477.95), where ours rounds to 5 paisa ties to even.
  The tick-rounding row of the table above is the same thing at ₹1.
- 12,110 are at a steady Kite factor for an action we do not adjust:
  RELIANCE 0.9532 before the Jio Financial demerger of 2023-07-20, INFY 0.9788
  before its ₹25 dividend of 2026-06-10, WIPRO 0.9558 and 0.9748 across its
  buybacks and dividends.
- 4,643 fit no single factor: TATASTEEL and GAIL (the additive adjustment)
  and MCX, BSE and BAJFINANCE (rounding to the rupee).

### Why the Universe overlaps the NIFTY 200 by 77 percent

All 46 NIFTY 200 constituents outside the Universe rank 201 to 448 by traded
value, except VAML (listed 76 trading days, under the rule's 126). The 46
members outside the index rank 18 to 197 by traded value. The cause is the
rule, not the data: the NIFTY 200 is chosen by free-float market capitalisation,
the Universe by six-month traded value (ADR 0005). Heavily traded small and
mid caps come in (NETWEB, HSCL, CUPID, IFCI, OLAELEC, PCJEWELLER) and thinly
traded large caps drop out (MRF, PAGEIND, COLPAL, DABUR, BAJAJHLDNG, IRCTC).

### Reproducibility

The 24,000 membership rows hash to `45fffd690fa35a94de779ca12d02f489` (md5
of month, rank, symbol and traded value in month and rank order) under rule
version `53181efb8726`. Nine months were stored by the query before it was
materialized; the second run reproduced them too.

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
