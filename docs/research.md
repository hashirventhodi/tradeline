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

## Why an ever-member has no Kite candles (#26)

Checked on 2026-10-21 against NSE's live list of delisted securities, fetched
through the NSE session (recorded and trimmed under
`tests/fixtures/end_reasons/`), and against the #13 database's corporate
actions.

| Question | Answer | Checked on |
|---|---|---|
| Where is NSE's delisted list? | `https://nsearchives.nseindia.com/content/equities/delisted.csv`, beside `EQUITY_L.csv` and `symbolchange.csv`. The JSON paths tried on `www.nseindia.com` (`/api/delisted-securities`, `/api/corporates-delisting`) answer 404. | 2026-10-21 |
| Does it say why a security was delisted? | **Only the kind of delisting.** Its columns are Symbol, Company, Delisted Date (`15-Apr-02`, a two-digit year), and Type of Delisting: Compulsory Delisting (137), Delisting - Liquidation (99), or Voluntary Delisting (92). It never says merged: HDFC, MINDTREE, GRUH, IDFC and the merged PSU banks are not on it. | 2026-10-21 |
| Is it current? | **No.** 328 records from 15 April 2002 to 9 November 2020 (HEXAWARE); the server's `Last-Modified` is 11 November 2020. Nothing delisted since is listed. | 2026-10-21 |
| How much of the #13 Universe does it cover? | One of the 544 ever-members: HEXAWARE. | 2026-10-21 |
| Does NSE's corporate-action list record mergers? | Rarely, and only for the company absorbed. Of 26,264 actions from 2015, three subjects name an amalgamation without a demerger: PENPEBS ex 2019-06-07 (its last row 2019-06-06), IFGLREFRAC ex 2017-09-14 (last row 2017-09-13), and SABTN 2016-03-22 (a composite scheme; SABTN kept trading). The merged large caps (HDFC, MINDTREE, the PSU banks) have no scheme action at all. | 2026-10-21 |

Consequence: an Instrument's end reason is **merged** only where NSE lists a
scheme of amalgamation for its last Symbol with an Ex-Date after its last row,
and **delisted** only where the delisted list names its last Symbol after its
last row, with the kind of delisting as the detail. Everything else that
ended is **ended, reason not stated**. Of the #13 run's 52 ever-members that
were delisted, merged, or now trade only in BE or BZ, expect the last to show
**series moved** and nearly all the rest **ended, reason not stated**. `ingest delistings` stores the list
as published; a newer official source would be a new ingest, not an inference.

## Instrument identity rerun on the #13 data (#29)

Run on 2026-10-21 on the #13 database, after #16's Instruments (ADR 0016),
against live NSE and Kite responses. The figures sit beside #13's, which
stand above as they were measured.

| Step | Result | Runtime |
|---|---|---|
| `migrate` (0011 to 0019) | Nine migrations. 0019 re-derives 14 stored splits (below). | under 1 s |
| `ingest symbol-changes` | 1,067 NSE records. | under 1 s |
| `ingest delistings` | 328 records, once its Windows encoding was read (below). | under 1 s |
| `rebuild-instruments`, first | 4,486 Instruments; 811 links (410 corporate action, 400 symbol change, 1 manual); 119 anomalies; 32 Link Candidates. | 29 s |
| `rebuild-instruments`, last | 4,449 Instruments; 848 links (424 corporate action, 400 symbol change, 24 manual); 104 anomalies, all reviewed (below); 11 Link Candidates, all reviewed. | 29 s |
| `universe --month 2016-11 --to 2026-10`, three times | Rule version `ded30b1014c4` (`rank_by = "instrument"`): 120 months of 200 members, 544 Symbols, 514 Instruments. The second run, before the SUBEX link, recomputed every month identically. The third, after it and after the same-Symbol rule was tightened, wrote nothing and chose the same members, ranks and traded values in all 120 months; six (2021-07 to 2021-12) differ only in SUBEXLTD's key (below). | 257 s |
| `ingest candles --interval day` for the nine renamed ever-members whose current Symbol was never a member | 9 loaded through their current Symbols' tokens (CENTURYTEX as ABREL, HEG as HEGAM, MAJESCO as AURUM, ...). | under 1 min |
| `reconcile --from 2016-10-21 --to 2026-10-20` | 1,008,280 rows, 45,324 discrepancies (4.50 percent, from 8.88): still **fail** against 0.1 percent. **No rename extras.** | 4 min (7 s by Symbol) |

### What the run found

- **NSE's delisted list is in Windows encoding.** Company names hold a 0xA0
  non-breaking space (BENTOLCHEM, "Chemox Laboratories"), so `ingest
  delistings` failed on the live list as UTF-8. The trimmed fixture had left
  those rows out; it now keeps one.
- **From 2016 to 2018 NSE wrote some splits as "Fv Splt Frm Rs 10 To Re 1".**
  The parser filed all 14 as "other", so they neither adjusted the Adjusted
  Series nor explained the ISIN change each brought. JSWSTEEL's 10-for-1
  (ex 2017-01-04), KARURVYSYA's 5-for-1 and SOLARINDS's were among them, all
  ever-members. The parser reads the form now. Migration 0019 re-derives the
  stored rows, as 0004 says a parser fix is applied, after which all 26,264
  stored actions agree with the parser. JSWSTEEL and KARURVYSYA now hold one
  Kite factor either side of the split.
- **YESBANK changed ISIN with no Corporate Action.** To apply the 2020
  Reconstruction Scheme's three-year lock-in of 75 percent of holdings
  (G.S.R. 174(E), clause 3(8)), the depositories moved every share from
  INE528G01027 to INE528G01035. Dealing in the new ISIN began on 2020-03-16
  (NSE circular 17/2020). Split there, the Instrument rule would have counted
  YESBANK, then ranked 4 to 41, as newly listed for about six months. A manual
  link may now name one Symbol on both sides for such a change, held to what
  a split is held to: dated on the new ISIN's first day, with the old ISIN's
  last row within seven days before it, so it cannot bridge a relisting years
  later (ADR 0016). A row that joins nothing is reported as an
  `unmatched_manual_link` anomaly; the last rebuild has none.
- **SUBEX was readmitted as SUBEXLTD under a new ISIN, with no symbol-change
  record.** An NCLT-approved capital reduction halved the face value
  (NSE/CML/46245, 2020-11-05). Its manual link removed the last 886 rename
  extras in reconcile. It was added after the Universe was computed, so it
  marks 54 months of the new version stale. Recomputed, all 54 match what is
  stored.
- **CCAVENUE (formerly INFIBEAM) consolidated 10 shares into 1 on
  2026-10-13** (NSE/CML/76849, NSE/CML/76916; new ISIN INE483S01038 from
  2026-10-19). NSE's Corporate Action list does not carry it yet, so its
  history is split and it is reported as an anomaly. A manual link is not
  used: it would join a tenfold unadjusted price into the Adjusted Series.
  Once NSE lists the action, an ingest links and adjusts it. No stored month
  is affected; November 2026's window would be.
- **An Instrument's listing date can follow its first row.** NSE's master
  gives the date a Symbol was last admitted, so SUBEX's Instrument is listed
  2020-11-05 and NESTLEIND's 2023-08-01. Four ever-members are affected
  (NESTLEIND, FORCEMOT, CAMS, SHAKTIPUMP) and none loses a month to it here:
  the Symbol rule read the same dates. Listing age from such a date
  understates a long history.

### Manual links added

23 rows, each to an official circular that was read. NSE's own circulars are
cited throughout (MSEI's 11349 confirms PCBL's). In
order of what they change: YESBANK's ISIN change; PHILIPCARB to PCBL
(2022-01-13; it was outside the Universe from 2018-10 to 2024-02, so no month
changes); SUBEX to SUBEXLTD; eight 2022 equity renames NSE's list omits
(PDSL, RBA, SEPC, ZFCVINDIA, KEEPLEARN, ESSENTIA, and the SME DIL and
DUGLOBAL); and twelve ETF renames (Nippon 2019 and 2022, ICICI Prudential
2023, Kotak 2024). For KEEPLEARN and ESSENTIA the circular's date is earlier
than the new Symbol's first row in NSE's files.

### Anomalies and Link Candidates

| What remains | Count | Explanation |
|---|---|---|
| Fund ISIN changes (INF) | 71 | ETF units under a new ISIN, in batches on one day per fund house (six Reliance (now Nippon India) ETFs on 2019-12-20; five HDFC on 2023-10-20; four ICICI on 2024-03-01). Explained as a class, not cited row by row. No Corporate Action, and the Universe excludes INF ISINs, so no month depends on them. Left split. |
| SME ISIN changes | 21 | SM and ST Symbols whose price falls four- to ten-fold on the new ISIN, which reads as SME bonuses and splits NSE's `index=equities` Corporate Action list does not carry. Inferred from the prices, not cited. No ever-member, and the Universe counts only EQ. Left split. |
| Other equity ISIN changes | 12 | Relistings after long suspensions (KSE from 2015, DSKULKARNI from 2018, EASTSILK, BURNPUR, MBECL, SUMEETINDS, KAUSHALYA, SHEKHAWATI), VERTOZ's consolidation 16 days before its relisting, TIDEWATER (2016) and KARDA (2020), whose prices fall about four-fold with no action NSE lists (unexplained), and CCAVENUE (above). No ever-member except CCAVENUE's Instrument. |
| Link Candidates | 11 | All end on 2015-01-29: companies listed on another exchange that traded on NSE as "permitted to trade" until NSE withdrew the facility from 30 January 2015 (NSE/CML/28574, which names companies listed on the MSE and the Madhya Pradesh Stock Exchange; corrected by #32), then listed on NSE directly. NSE dates them from that listing (AVANTIFEED 2015-04-15), so they are new listings, not renames. Left split. |

Before the run, 119 anomalies included the 14 "Fv Splt Frm" splits; CADILAHC's
2015 ISIN change is now joined, so ZYDUSLIFE's history runs from 2015. No
manual link is unmatched.

### Unresolved

- **CCAVENUE's consolidation** (#31; 10 to 1, new ISIN from 2026-10-19) is not in
  NSE's Corporate Action list. Its history is split, INFIBEAM's Instrument
  shows as ended, and November 2026's Universe window will see CCAVENUE as
  newly listed unless the action is listed and ingested first.
- **Listing dates later than an Instrument's first row** (#32): SUBEX (2020-11-05),
  NESTLEIND (2023-08-01), FORCEMOT, CAMS and SHAKTIPUMP among ever-members.
  No month changes here, but listing age from such a date understates a long
  history. Researched in "Listing dates later than an Instrument's first
  row (#32)" below.
- **Explained by inference, not citation** (#33): the 21 SME and 71 fund ISIN
  changes (as classes), and TIDEWATER's and KARDA's four-fold changes (not
  explained). None touches an ever-member or an EQ Universe month.
  Researched in "Fund, SME, TIDEWATER and KARDA ISIN changes (#33)" below:
  all are now cited.
- **MAZDOCK's 8 candles from 2017 and 2018** (#34), before its 2020 listing: an
  earlier instrument under a reused Kite token is likely but unconfirmed.
  Researched in "MAZDOCK's Kite candles before its listing (#34)" below:
  they are Farmax India's BZ bars under NSE token 509, with MAZDOCK's 2024
  split applied.
- **A stored member's key can be renamed under it.** The SUBEX link renamed
  the Instrument `SUBEXLTD@2020-11-05` to `SUBEX@2020-11-05` (its first Symbol
  is now SUBEX). SUBEXLTD's six stored member-months under `ded30b1014c4`
  (2021-07 to 2021-12) keep the old key, which no longer names an
  Instrument, and recomputing them reports a difference in that key alone.
  The stale check counts a new key over the same grouping as no change (#27),
  so it did not report them. Settled by #35: a recompute now compares
  members by Instrument identity (the Instrument a member's Symbol named on
  the day before its month), and a key a rebuild retired is reported, never
  rewritten (`queries.obsolete_universe_keys`, the `instruments` report, and
  each rebuild's run details). Recomputed again, all 120 months write
  nothing and none differs; the six are reported as obsolete keys, each now
  `SUBEX@2020-11-05`.
- **Reconcile still fails** at 4.50 percent against 0.1. The rest is series
  moves (for #17), Kite's additive adjustment and coarse rounding, the
  muhurat sessions and BAFNAPH, as in #13.

### Universe under the Instrument rule

The 24,000 rows under `53181efb8726` are unchanged: md5 of
`concat_ws('|', month, rule_version, rank, symbol, series, traded_value)`
joined by `,` in month and rank order is `93edba0bea8ba0a6bcf9b3def0d80003`
before migrating and after the rerun. Under `ded30b1014c4` the same digest
over `month, rank, instrument_key, symbol, series, traded_value` is
`be464f063b66c9df6de72833f9f54c17`.

- **108 member-months regained by 24 renamed Instruments**, among them
  ETERNAL 2025-05 to 2025-09, LTIM 2023-01 to 2023-06, LTM 2026-04 to 2026-08,
  ZYDUSLIFE 2022-04 to 2022-07, TMPV 2025-12 to 2026-04, and five months each
  for TATACONSUM, SHRIRAMFIN, NAM-INDIA, PVRINOX, COFORGE, MOTHERSON,
  ADANIENSOL, INDUSTOWER, IDFCFIRSTB, HBLENGINE and ANGELONE. The 108
  member-months they displace all ranked 198 to 200 under the old version.
- **Two of #16's seven renames keep their gaps, for want of liquidity.**
  L&TFH to LTF is out from 2024-05 to 2024-10 and MCDOWELL-N to UNITDSPR from
  2024-02 to 2025-01. In every one of those months the Instrument has all 126
  EQ days in its window, summed across the rename, and less traded value than
  the 200th member: UNITDSPR's 2024-06 window was ₹13,956 crore against
  ₹16,482 crore. No other Instrument in the seven loses a month.

### Adjusted Series

Read as of 2026-10-19 in EQ: ETERNAL (ZOMATO from 2021-07-23), LTM (LTI
2016-07-21, LTIM 2022-12-05, LTM 2026-02-27), HDFCBANK (factors 1/2 and 1/4
across the 2019 split and 2025 bonus), JSWSTEEL, YESBANK and ZYDUSLIFE each
have a bar on every EQ trading day from their first to 2026-10-19. The close
moves at most 1.8 percent across any rename (ZOMATO to ETERNAL, LTI to LTIM).
The largest day in any of them is YESBANK's 58 percent on 2020-03-17, the
rally after the rescue.

### Reconcile beside #13

| Cause | #13 | #29 |
|---|---|---|
| Rename extras | 44,408 | **0** |
| Series-move extras (our rows continue in BE, BZ, ...; for #17) | 11,685 | 13,596, over more Instruments compared |
| Other extras | | 8: MAZDOCK candles dated 2017-12 to 2018-07, before its 2020 listing, perhaps under a reused Kite token (unconfirmed) |
| Close mismatches (Kite's additive adjustment and coarse rounding, as in #13) | about 29,400 | 29,670, the same causes; COFORGE's history, read as NIITTECH, is compared now (162) |
| Muhurat sessions before 2019 | 935 | 1,044 |
| BAFNAPH | 998 | 998 |
| Other missing | | 8 single days |
| **Total** | **87,502 (8.88 percent)** | **45,324 (4.50 percent)** |

### Ever-members by end reason

#13 found 90 ever-member Symbols with no Kite token and counted 38 of them
renamed: those NSE's symbol-change list names as an old Symbol. Recounted
on the same 90:

| Population | Symbols | With Kite candles |
|---|---|---|
| Renamed by NSE's list (#13's 38) | 38 | 33, through the current Symbol's token |
| Renamed by a manual link only (PHILIPCARB, RUCHI) | 2 | 2 |
| Not renamed | 50 | 0 |
| **Total** | **90** | **35** |

The five of #13's 38 without candles are Instruments with no token today,
not renames left unjoined: GATI (ended as ACLGATI), IBVENTURES (ended as
DHANI), TATASPONGE (ended as TATASTLLP), STRTECH (moved to BE as STLTECH),
and INFIBEAM (cut by CCAVENUE's consolidation, below). They and the 50 make
55 Symbols in 54 Instruments (IBVENTURES and DHANI are one). Each is below as
`tradeline instruments` reported it on 2026-10-21, with the evidence the
reason rests on (#26's rule). "Merged" is given only for a scheme of
amalgamation in NSE's Corporate Actions, and none of the 54 has one; where
an official source outside that list says more, it is noted beside the
reason, not in place of it.

| Reason | Instruments |
|---|---|
| delisted | 1 |
| series moved | 8 |
| ended, reason not stated | 45 |
| merged | 0 |

| Instrument | Symbols (ever-members in bold) | Last day | Reason | Evidence |
|---|---|---|---|---|
| HEXAWARE@2015-01-01 | **HEXAWARE** | 2020-10-30 | delisted | NSE delisted list: Voluntary Delisting on 2020-11-09 |
| ABIRLANUVO@2015-01-01 | **ABIRLANUVO** | 2017-07-04 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| ALBK@2015-01-01 | **ALBK** | 2020-03-19 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| ANDHRABANK@2015-01-01 | **ANDHRABANK** | 2020-03-19 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| BHUSANSTL@2015-01-01 | BHUSANSTL, **TATASTLBSL** | 2021-11-12 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| CAIRN@2015-01-01 | **CAIRN** | 2017-04-25 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| CAPF@2015-01-01 | **CAPF** | 2018-12-27 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| DALMIABHA@2015-01-01 | **DALMIABHA** | 2018-11-28 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| DHFL@2015-01-01 | **DHFL** | 2021-06-11 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| DISHMAN@2015-01-01 | **DISHMAN** | 2017-05-29 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| EQUITAS@2016-04-21 | **EQUITAS** | 2023-02-02 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| EROSMEDIA@2015-01-01 | **EROSMEDIA** | 2025-06-30 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| FCEL@2015-01-01 | FCEL, **FCONSUMER** | 2026-08-31 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| FRETAIL@2016-08-29 | **FRETAIL** | 2024-08-01 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| GATI@2015-01-01 | **GATI**, ACLGATI (renamed, #13's 38) | 2025-11-11 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| GRUH@2015-01-01 | **GRUH** | 2019-10-15 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| GSKCONS@2015-01-01 | **GSKCONS** | 2020-04-15 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| HDFC@2015-01-01 | **HDFC** | 2023-07-12 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| IBSEC@2015-01-01 | IBSEC, **IBVENTURES**, **DHANI** (renamed, #13's 38) | 2025-10-27 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| IDFC@2015-01-01 | **IDFC** | 2024-10-09 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| INFIBEAM@2016-04-04 | **INFIBEAM**, CCAVENUE (renamed, #13's 38) | 2026-10-12 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions. Unresolved: cut by CCAVENUE's 10-to-1 consolidation (NSE/CML/76916), which NSE's Corporate Action list does not carry yet; trades on as CCAVENUE |
| ISEC@2018-04-04 | **ISEC** | 2025-03-21 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| JBCHEPHARM@2015-01-01 | **JBCHEPHARM** | 2026-07-16 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions. Amalgamated into Torrent Pharmaceuticals, record date 2026-07-17 (NSE/CML/75178); NSE's Corporate Action list has no scheme for it |
| JETAIRWAYS@2015-01-01 | **JETAIRWAYS** | 2024-11-07 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| JPASSOCIAT@2015-01-01 | **JPASSOCIAT** | 2026-03-17 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| MEGH@2015-01-01 | **MEGH** | 2021-05-17 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| MINDTREE@2015-01-01 | **MINDTREE** | 2022-11-22 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| MONSANTO@2015-01-01 | **MONSANTO** | 2019-09-26 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| ORIENTBANK@2015-01-01 | **ORIENTBANK** | 2020-03-20 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| PEL@2015-01-01 | **PEL** | 2025-09-22 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| PIPAVAVDOC@2015-01-01 | PIPAVAVDOC, RDEL, **RNAVAL** | 2023-07-13 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| RELCAPITAL@2015-01-01 | **RELCAPITAL** | 2024-02-26 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| SINTEX@2015-01-01 | **SINTEX** | 2023-02-10 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| SKSMICRO@2015-01-01 | SKSMICRO, **BHARATFIN** | 2019-07-02 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| SPICEJET@2019-08-19 | **SPICEJET** | 2023-04-28 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| SREINFRA@2015-01-01 | **SREINFRA** | 2023-08-11 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| SUNILHITEC@2015-01-01 | **SUNILHITEC** | 2019-07-16 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| SYNDIBANK@2015-01-01 | **SYNDIBANK** | 2020-03-19 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| TATACOFFEE@2015-01-01 | **TATACOFFEE** | 2024-01-12 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| TATAMETALI@2015-01-01 | **TATAMETALI** | 2024-02-05 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| TATAMTRDVR@2015-01-01 | **TATAMTRDVR** | 2024-08-29 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| TATASPONGE@2015-01-01 | **TATASPONGE**, TATASTLLP (renamed, #13's 38) | 2023-11-16 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| TINPLATE@2015-01-01 | **TINPLATE** | 2024-01-18 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| TV18BRDCST@2015-01-01 | **TV18BRDCST** | 2024-10-15 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| UJJIVAN@2016-05-10 | **UJJIVAN** | 2024-05-02 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| UPERGANGES@2015-01-02 | **UPERGANGES** | 2017-03-22 | ended, reason not stated | no later row; NSE's delisted list names no later delisting of its last Symbol, and no scheme of amalgamation follows in NSE's Corporate Actions |
| ABAN@1995-03-29 | **ABAN** | 2025-09-04 | series moved | Our rows continue in BE from 2025-09-05; Kite gives a token for EQ only |
| HDIL@2007-07-24 | **HDIL** | 2019-11-21 | series moved | Our rows continue in BE from 2019-11-22; Kite gives a token for EQ only |
| HFCL@2011-03-09 | **HFCL** | 2026-09-02 | series moved | Our rows continue in BE from 2026-09-03; Kite gives a token for EQ only |
| MEP@2015-05-06 | **MEP** | 2024-03-19 | series moved | Our rows continue in BE from 2024-03-20; Kite gives a token for EQ only |
| RAJESHEXPO@2000-02-09 | **RAJESHEXPO** | 2025-12-24 | series moved | Our rows continue in BZ from 2025-12-26; Kite gives a token for EQ only |
| RCOM@2006-03-06 | **RCOM** | 2022-05-24 | series moved | Our rows continue in BE from 2022-05-25; Kite gives a token for EQ only |
| RELINFRA@1995-02-08 | **RELINFRA** | 2025-06-04 | series moved | Our rows continue in BE from 2025-06-05; Kite gives a token for EQ only |
| STRTECH@2000-12-08 | **STRTECH**, STLTECH (renamed, #13's 38) | 2026-05-13 | series moved | Our rows continue in BE from 2026-05-14; Kite gives a token for EQ only |

## Fund, SME, TIDEWATER and KARDA ISIN changes (#33)

Researched on 2026-10-22 against the #29 database (the last rebuild's 104
`unexplained_isin_change` anomalies) and live NSE responses: the Corporate
Action API the repo calls (`www.nseindia.com/api/corporates-corporateActions`,
built by `corporate_actions_url` in `src/tradeline/sources/nse.py`), NSE's
circular search (`www.nseindia.com/api/circulars?fromDate=..&toDate=..`), the
circulars themselves at `nsearchives.nseindia.com/content/circulars/<dept><number>.pdf`,
and company filings through NSE's announcements API. Requests went through the
repo's own `NseSession` (browser User-Agent, homepage cookie first) over
`LiveTransport`. No code, config or stored data was changed; nothing was
ingested. All 94 changes in scope have a primary source, and none was a
reissue without a split except HDFCLIQUID.

| Class | Count | Status | Source |
|---|---|---|---|
| Fund (INF) ISIN changes, face-value splits of ETF units | 70 | **Cited** | An NSE "Change in ISIN" circular naming each new ISIN (31 batches, below); 66 also in NSE's Corporate Action API under `index=mf` |
| Fund (INF) ISIN change with no split (HDFCLIQUID, 2025-02-28) | 1 | **Cited** | NSE/CML/66863: change of scheme name and ISIN |
| SME (SM, ST) splits and bonuses | 19 | **Cited** | NSE's Corporate Action API under `index=sme` |
| SME splits not in that API (JSLL, GICL) | 2 | **Cited** | NSE circulars NSE/CML/68416, 70656, 70874 and the companies' filings |
| TIDEWATER (2016-03-17): split Rs 10 to Rs 5 plus a 1:1 bonus | 1 | **Cited** | NSE/CML/31924, NSE/CML/31978, NSE/CMPT/31946, NSE/CML/32044 |
| KARDA (2020-07-03): split Rs 10 to Rs 2 | 1 | **Cited** | NSE/CML/44773, NSE/CML/44835, NCL/CMPT/44827 |

None of these is an EQ ever-member, so nothing here changes a stored
Universe month. The other ten "other equity" changes in #29's table
(relistings, VERTOZ, CCAVENUE) were not part of this research.

### SME actions are served under `index=sme`

The repo asks only for `index=equities`. The same endpoint answers other
`index` values. All of these returned HTTP 200 (requested on 2026-10-22):

| Request (`/api/corporates-corporateActions?...`) | Returned |
|---|---|
| `index=equities&from_date=01-01-2023&to_date=31-01-2023` | 49 records, EQ and other main-board series |
| `index=sme&from_date=01-01-2023&to_date=31-01-2023` | 6 records, all series SM (GOLDSTAR's split and bonus among them) |
| `index=SME&...`, `index=emerge&...` (same dates) | `{"data":[],"msg":"no data found"}` |
| `index=sme&from_date=01-01-<year>&to_date=31-12-<year>`, 2015 to 2026 | 1,517 distinct (Symbol, series, Ex-Date, subject), all series SM |
| `index=sme&symbol=GOLDSTAR&from_date=01-01-2015&to_date=31-10-2026` | 8 records; the same query with `index=equities` returns `[]` |
| `index=mf&from_date=01-12-2019&to_date=31-12-2019` | 140 records, series MF (ETF and fund unit actions) |
| `index=mf&...`, year by year, 2015 to 2026 | 8,714 distinct (Symbol, series, Ex-Date, subject), series MF, EQ and BL; 6,854 have `exDate` `"-"` (redemptions, "Specified Transaction Period") |
| `index=etf&...`, `index=debt&...` (same dates) | `{"data":[],"msg":"no data found"}` |

The value is lower-case `sme`. The records differ from the equity ones in two
ways that matter to ingest:

- **The `isin` field is not an ISIN.** It holds a number (GOLDSTAR's split:
  `"isin":"386040"`, its bonus `"386046"`), so the builder could match an SME
  action only by Symbol, never by the old or new ISIN.
- **Subjects are in upper case** ("FACE VALUE SPLIT (SUB-DIVISION) - FROM RS
  10/- PER SHARE TO RE 1/- PER SHARE", "BONUS 4:5"). Run over all 1,517,
  `adjust.interpret` gives 19 splits and 153 bonuses, and leaves one
  adjusting subject without a factor: TOUCHWOOD's "BONUS 1.5:1" (ex
  2019-08-22), which `looks_adjusting` would flag.

**The 21.** Each SME change was checked against the `index=sme` records by
the builder's own rule (an adjusting action under the Symbol whose Ex-Date is
within `EX_DATE_SLACK`, seven days, of both the old ISIN's last row and the
new one's first). 19 of the 21 are explained; the price change on the
Ex-Date matches the factor in each. In 8 the fall shows on the old ISIN's
last row, the Ex-Date, a day or three before the new ISIN's first row
(SHRENIK 942.40 to 205.70 on 2018-07-09, new ISIN from 2018-07-10), which is
why #29 saw no fall "on the new ISIN" for some.

| Symbol | New ISIN from | Action in `index=sme` (Ex-Date: subject) | Status |
|---|---|---|---|
| SHRENIK | 2018-07-10 | 2018-07-09: split Rs 10 to Rs 2 | Cited |
| AIRAN | 2018-08-16 | 2018-08-14: split Rs 10 to Rs 2 | Cited |
| MCL | 2019-05-03 | 2019-05-02: split Rs 10 to Rs 5 | Cited |
| VCL | 2022-03-30 | 2022-03-28: split Rs 10 to Rs 2 | Cited |
| DUGLOBAL | 2022-07-01 | 2022-06-30: split Rs 10 to Rs 2 | Cited |
| GOLDSTAR | 2023-01-23 | 2023-01-20: split Rs 10 to Re 1 and bonus 4:5 (factor 1/18; 161.90 to 9.45) | Cited |
| LATTEYS | 2023-02-10 | 2023-02-10: split Rs 10 to Rs 2 | Cited |
| SECL | 2023-05-04 | 2023-05-03: split Rs 10 to Re 1 | Cited |
| CLOUD | 2023-05-12 | 2023-05-12: split Rs 10 to Rs 5 and bonus 1:1 | Cited |
| ISHAN | 2024-01-25 | 2024-01-25: split Rs 10 to Re 1 and bonus 2:1 (factor 1/30) | Cited |
| CELLECOR | 2024-08-09 | 2024-08-09: split Rs 10 to Re 1 | Cited |
| KCK | 2024-10-30 | 2024-10-30: split Rs 10 to Rs 2 | Cited |
| ENSER | 2025-02-07 | 2025-02-07: split Rs 10 to Rs 2 | Cited |
| PRITIKA | 2025-02-17 | 2025-02-14: split Rs 10 to Rs 5 | Cited |
| COOLCAPS | 2025-07-04 | 2025-07-04: split Rs 10 to Rs 2 and bonus 1:1 | Cited |
| MOS | 2025-08-08 | 2025-08-08: split Rs 10 to Rs 2 | Cited |
| DHARIWAL | 2026-02-06 | 2026-02-06: split Rs 10 to Rs 2 | Cited |
| PSRAJ | 2026-07-24 | 2026-07-24: split Rs 10 to Rs 2 | Cited |
| ORIANA | 2026-09-18 | 2026-09-18: split Rs 10 to Rs 2 | Cited |
| JSLL | 2025-06-12 | Not in `index=sme` (by year, by month, or with `symbol=JSLL`) nor `index=equities`. Split Rs 10 to Rs 2, record date 2025-06-12: NSE/CML/68231 (Face Value Split, 2025-05-28) and NSE/CML/68416 (Change in ISIN to INE0J5801029, effective 2025-06-12); the company's board outcome of 2025-04-21 (24,860,146 shares of Rs 10 into 124,300,730 of Rs 2, `nsearchives.nseindia.com/corporate/JEENASIKHO_21042025132621_OutcomeJSLLSigned.pdf`) and record-date notice (`JEENASIKHO_24052025183909_Record_Date_Notice.pdf`). 2,250.95 to 429.90. | Cited |
| GICL | 2025-10-15 | Not in `index=sme` nor `index=equities`. Split Rs 10 to Rs 5 (NSE/CML/70527; NSE/CML/70656, Change in ISIN to INE947T01022 effective 2025-10-15) and bonus 1:1 with the same record date, 2025-10-15 (NSE/CML/70874: 5,59,77,294 bonus shares, distinctive numbers 55,977,295 to 111,954,588, i.e. one for each existing share). The company's filings say the same (`GICL_28082025152224_BM_Outcome_28-08-2025.pdf`, `GICL_30092025162120_Intimation_Record_Date_corrected.pdf`). Factor 1/4; 173.70 to 46.50. | Cited |

So ingesting `index=sme` would explain 19 of the 21 under the current rule,
with no manual link. JSLL and GICL would remain anomalies until NSE lists
their actions (both in 2025; that year's `index=sme` list has 92 records
against 274 for 2024, and is not known to be complete). Whether ingesting `index=sme`
also changes SME Adjusted Series is a question for the ingest, not answered
here.

### Fund (INF) ISIN changes are unit splits, each with an NSE circular

The 71 are not reissued units. 70 are face-value splits (sub-divisions) of
ETF units. NSE announces each with a "Face Value Split" circular and a
"Change in ISIN" circular from its Listing department, plus an early pay-in
circular from the clearing corporation. Every one of the 71 new ISINs
appears in the text of a "Change in ISIN" circular, which was downloaded
and searched for it; the circular gives the symbol, the new ISIN, the date
it takes effect and the sub-division ("Sub-Division from Rs. 10/- to Re.
1/-"). The price falls about ten-fold (a hundred-fold for the Rs 100 to Re 1
gold ETFs) on the Ex-Date, the old ISIN's last row, a day before the new
ISIN's first (NIFTYBEES 1,292.54 on 2019-12-18, 130.20 on 2019-12-19 under
INF732E01011, 130.34 on 2019-12-20 under INF204KB14I2).

The same splits are in NSE's Corporate Action API under `index=mf`
(requested by half-year, 2015 to 2026). Under the builder's rule, 66 of the
71 match one there. The five that do not:

- **KOTAKGOLD 2015-04-15**: in `index=mf`, but filed under today's Symbol
  and ISIN (GOLD1, INF174KA1HJ8, ex 2015-04-13, Rs 100 to Rs 10), neither of
  which this change carries, so the rule cannot reach it.
- **IGOLD 2016-06-10, ICICIGOLD 2018-11-16, ICICIBANKN 2022-09-02**: not in
  `index=mf`. Each has its circulars (below).
- **HDFCLIQUID 2025-02-28**: not a split. NSE/CML/66863 changes the scheme's
  name (to "HDFC Nifty 1D Rate Liquid ETF - Growth") and its ISIN from
  INF179KC1HE2 to INF179KC1JG3 from 2025-02-28, and `index=mf` lists it as
  "Change In Fundamental Attribute". The price is unchanged (999.99,
  1,000.34). This one is a true same-unit ISIN change, of the kind ADR 0016's
  same-Symbol manual link exists for.

| New ISIN from | Fund house | Symbols | Split | Face Value Split circular | Change in ISIN circular | Status |
|---|---|---|---|---|---|---|
| 2015-04-15 | Kotak | KOTAKGOLD | Rs 100 to Rs 10 | NSE/CML/29389 | NSE/CML/29412 | Cited |
| 2016-06-10 | ICICI Prudential | IGOLD | Rs 100 to Rs 10 | NSE/CML/32517 | NSE/CML/32526 | Cited |
| 2017-07-28 | Kotak | KOTAKNIFTY | Rs 10 to Re 1 | NSE/CML/35394 | NSE/CML/35452 | Cited |
| 2018-11-16 | ICICI Prudential | ICICIGOLD, ICICINXT50 | Rs 10 to Re 1 | NSE/CML/39361, 39363 | NSE/CML/39383, 39385 | Cited |
| 2019-12-20 | Reliance Nippon Life (Nippon India) | NIFTYBEES, BANKBEES, PSUBNKBEES, GOLDBEES, NETFNV20, HNGSNGBEES | Rs 10 to Re 1; GOLDBEES Rs 100 to Re 1 | NSE/CML/42873 | NSE/CML/42888 | Cited |
| 2020-07-27 | Axis | AXISGOLD, AXISNIFTY | Rs 100 to Re 1; Rs 100 to Rs 10 | NSE/CML/45054 | NSE/CML/45074 | Cited |
| 2021-02-19 | HDFC | HDFCMFGETF, HDFCNIFETF, HDFCSENETF | ten-for-one; HDFCMFGETF Rs 100 to Re 1 | NSE/CML/47320 | NSE/CML/47344 | Cited |
| 2021-02-19 | UTI | UTIBANKETF, UTISXN50 | Rs 10 to Re 1 | NSE/CML/47279 | NSE/CML/47347 | Cited |
| 2021-03-26 | UTI | GOLDSHARE, UTINEXT50 | Rs 100 to Re 1; Rs 10 to Re 1 | NSE/CML/47665 | NSE/CML/47721 | Cited |
| 2021-06-18 | Motilal Oswal | MON100 (N100 in the circular) | Rs 10 to Re 1 | NSE/CML/48556 | NSE/CML/48595 | Cited |
| 2021-07-23 | Kotak | KOTAKGOLD | Rs 10 to Re 1 | NSE/CML/48928 | NSE/CML/48964 | Cited |
| 2021-10-29 | ICICI Prudential | ICICI500 | Rs 10 to Re 1 | NSE/CML/50053 | NSE/CML/50106 | Cited |
| 2021-11-26 | Aditya Birla Sun Life | ABSLBANETF, ABSLNN50ET, BSLGOLDETF, BSLNIFTY, BSLSENETFG | Rs 10 to Re 1; BSLGOLDETF Rs 10 to Re 0.10 | NSE/CML/50329 | NSE/CML/50382 | Cited |
| 2021-12-17 | Quantum | QGOLDHALF | Rs 100 to Rs 2 | NSE/CML/50496 | NSE/CML/50529 | Cited |
| 2022-01-07 | SBI | SETFGOLD | Rs 100 to Re 1 | NSE/CML/50859 | NSE/CML/50905 | Cited |
| 2022-08-12 | Motilal Oswal | MOLOWVOL, MOMOMENTUM | Rs 10 to Rs 2 | NSE/CML/53206 | NSE/CML/53260 | Cited |
| 2022-09-02 | ICICI Prudential | ICICIBANKN, ICICITECH | Rs 10 to Re 1 | NSE/CML/53404 | NSE/CML/53470 | Cited |
| 2023-09-25 | UTI | UTINIFTETF | Rs 10 to Re 1 | NSE/CML/58455 | NSE/CML/58523 | Cited |
| 2023-10-20 | HDFC | HDFCLOWVOL, HDFCMID150, HDFCMOMENT, HDFCNEXT50, HDFCNIF100 | ten-for-one | NSE/CML/58972 | NSE/CML/59009 | Cited |
| 2024-02-02 | HDFC | HDFCNIFBAN, HDFCNIFIT, HDFCPVTBAN, HDFCSENSEX | ten-for-one | NSE/CML/60421 | NSE/CML/60492 | Cited |
| 2024-03-01 | ICICI Prudential | AUTOIETF, LOWVOLIETF, NV20IETF, PVTBANIETF | Rs 10 to Re 1 | NSE/CML/60740 | NSE/CML/60870 | Cited |
| 2024-05-10 | ICICI Prudential | ALPL30IETF, FMCGIETF, MIDCAPIETF, MIDSELIETF, NIF100IETF, QUAL30IETF | Rs 10 to Re 1 | NSE/CML/61766 | NSE/CML/61914 | Cited |
| 2025-02-28 | HDFC | HDFCLIQUID | none: name and ISIN change | | NSE/CML/66863 | Cited |
| 2026-02-06 | Groww | GROWWGOLD, GROWWSLVR | Rs 10 to Re 1 | NSE/CML/72553 | NSE/CML/72617 | Cited |
| 2026-02-13 | Quantum | QNIFTY | Rs 10 to Re 1 | NSE/CML/72620 | NSE/CML/72718 | Cited |
| 2026-02-27 | Kotak | BANKNIFTY1, CONS, MIDCAP, NV20, SILVER1 | Rs 10 to Re 1 | NSE/CML/72916 | NSE/CML/72980 | Cited |
| 2026-03-06 | LIC | LICMFGOLD | Rs 100 to Re 1 | NSE/CML/73020 | NSE/CML/73115 | Cited |
| 2026-04-30 | Invesco | IVZINGOLD | Rs 100 to Re 1 | NSE/CML/73816 | NSE/CML/73909 | Cited |
| 2026-07-03 | DSP | HEALTHADD, MIDQ50ADD | Rs 10 to Re 1 | NSE/CML/74765 | NSE/CML/74912 | Cited |
| 2026-07-10 | Kotak | PSUBANK | Rs 10 to Re 1 | NSE/CML/74911 | NSE/CML/75102 | Cited |
| 2026-07-31 | Invesco | IVZINNIFTY | Rs 10 to Re 1 | NSE/CML/75236 | NSE/CML/75432 | Cited |
| 2026-08-28 | DSP | GOLDADD, SILVERADD | Rs 10 to Re 1 | NSE/CML/75678 | NSE/CML/75868 | Cited |

The "Change in ISIN" circulars were each read for the new ISIN. The
"Face Value Split" circulars are cited by their titles in NSE's circular
search; the ratios in the Split column are from the Change in ISIN
circulars' remarks and the `index=mf` subjects, of which a sample was read
against each other (NSE/CML/42888, 29412, 32526, 39383, 48595, 48964, 53470,
73115, 75102). The circulars for the clearing side (NSE/CMPT/29422,
32519, 39366, 39367; NCL/CMPT/42889, 45052, 45053, 47323, 47324, 47359 to
47361, 47685, 47686, 48597, 48962, 50088, 50377, 50556, 50930, 53198,
53447) say the same and were not read. AMC notices were not needed and not
searched.

### TIDEWATER and KARDA

Neither has any record in NSE's Corporate Action API under `index=equities`,
for any date (`symbol=TIDEWATER`, 2015 to 2017, and `symbol=KARDA`, 2019 to
2021, both return `[]`; the full lists for March 2016 and June to July 2020,
262 and 249 records, omit them too). Both are explained by NSE circulars.

- **TIDEWATER, 2016-03-17: a split from Rs 10 to Rs 5 and a 1:1 bonus, both
  ex 2016-03-16. Cited.** NSE/CML/31924 (2016-03-08) changes the face value
  from Rs 10 to Rs 5 from 2016-03-16. NSE/CML/31978 (2016-03-15, ref
  201/2016) changes the ISIN to INE484C01022 from 2016-03-17, the record
  date, "Sub-Division of Equity Shares from Rs. 10/- to Rs. 5/-", effective
  for trades from the Ex-Date, 2016-03-16. NSE/CMPT/31946 (2016-03-11) sets
  that Ex-Date for the split. NSE/CML/32044 (2016-03-22) admits 1,742,400
  "Equity shares of Rs. 5/- each issued under Bonus" (allotted 2016-03-18,
  distinctive numbers 1,742,401 to 3,484,800, under INE484C01022), one for
  each of the 1,742,400 shares after the split. The close falls from
  29,339.80 (2016-03-15) to 7,117.75 (2016-03-16), a factor of 4.12 against
  the 4 of split and bonus together. The bonus's own Ex-Date is not in a
  circular that was read; that it is 2016-03-16 is inferred from the price.
- **KARDA, 2020-07-03: a split from Rs 10 to Rs 2, ex 2020-07-02. Cited.**
  NSE/CML/44773 (2020-06-25, ref 0521/2020) changes the face value from Rs
  10 to Rs 2 from 2020-07-02. NSE/CML/44835 (2020-07-01, ref 0537/2020)
  changes the ISIN to INE278R01026 from 2020-07-03, "Sub-Division from Rs.
  10/- to Rs. 2/-", effective for trades from the Ex-Date, 2020-07-02.
  NCL/CMPT/44827 (2020-06-30) sets that Ex-Date in the trade-for-trade
  market (KARDA was in BE) and states the ratio: one old share for five new.
  The close falls from 275.65 to 57.90 on 2020-07-02, which is a fifth of
  the old close plus BE's 5 percent band.

A manual link would join either history without the factor, the reason
CCAVENUE has none (above); NSE's Corporate Action list supplies the factor
for neither.

### Outcome

Both lists are now ingested beside `index=equities`, each row recording its
list; the SME list's numbers are stored as no ISIN, and the fund-unit list's
records with no Ex-Date are not stored. TOUCHWOOD's "BONUS 1.5:1" reads as
3:2. The nine changes no list reaches (KOTAKGOLD, TIDEWATER, IGOLD,
ICICIGOLD, KARDA, ICICIBANKN, HDFCLIQUID, JSLL, GICL) are joined by manual
links citing their Change in ISIN circulars.

On the #29 database, on 2026-10-22: the backfill of 2015-01-01 to 2026-10-22
made 36 requests and stored 1,531 SME and 1,860 fund-unit rows (10,243
fund-unit records served with no Ex-Date, duplicates included). The rebuild
then made 509 corporate-action links (424 before) and 33 manual (24), and
reports 10 unexplained ISIN changes (104): all 94 of this section are
explained, and the 10 left (CCAVENUE, #31, among them) were reported before.
It finds 66 more Universe months stale, all 120 of `ded30b1014c4` now, since
the joins regroup SME and fund-unit rows in every window; recomputed, all
120 match the stored months (`tradeline universe --month 2016-11 --to
2026-10`), the six SUBEXLTD months reported as retired keys as in #35.

### Unresolved

- **No Adjustment Factor** for the eight manual links that are splits or
  bonuses (all but HDFCLIQUID): their Adjusted Series keep an unadjusted fall
  (#37). Settled for seven by #37 ("Splits and bonuses cited from NSE
  circulars (#37)" below); TIDEWATER's stays.
- **TIDEWATER's bonus Ex-Date** is inferred from the price; the circular
  read gives only the allotment date (2016-03-18).
- **120 stale months** that recompute unchanged stay recorded as stale;
  clearing them is the Operator's.

### Queries

The changes and their dates, with `psql` on the #29 database:

```sql
SELECT symbol, on_date, detail FROM instrument_anomalies
WHERE kind = 'unexplained_isin_change' ORDER BY on_date, symbol;
```

For each, the old ISIN's last row before `on_date` was taken from
`eod_bhavcopy` by Symbol and old ISIN. The NSE records were fetched with
`NseSession(LiveTransport()).get(url)` for the URLs above, and matched with
`adjust.interpret` and the builder's rule: an action of kind split or bonus,
filed under the Symbol or carrying the old or new ISIN, with an Ex-Date
within seven days of both the old ISIN's last row and the new one's first.
Circulars were listed per batch with `api/circulars` from 25 days before the
new ISIN's first day to 3 days after, filtered on their subject.

## Splits and bonuses cited from NSE circulars (#37)

Researched on 2026-10-22 against NSE's circulars, fetched as in #33 above
(`NseSession` over `LiveTransport`; the circular search `api/circulars` and the
PDFs at `nsearchives.nseindia.com/content/circulars/<dept><number>.pdf`), and
checked against the #29 database. Each row of `cited_corporate_actions.csv`
needs a circular that states both the ratio and the Ex-Date (ADR 0006, amended
by #37).

### Where the Ex-Date is stated

The Listing department's "Face Value Split" circulars (NSE/CML/29389, 32517,
39361, 44773, 53404, 68231, 70527, 31924) say only that the face value
"shall be changed w.e.f." a date, with the old and new values in a table:
they never say Ex-Date. The "Change in ISIN" circulars do, in one sentence,
"The new ISIN shall be effective for all trades done on and from the Ex-Date
i.e. <date>", with the ratio in the Remarks column. The clearing
corporation's early pay-in circulars (NSE/CMPT, later NCL/CMPT) state the
same Ex-Date and ratio, and the SME platform's market-lot circulars
(NSE/CMTR) state both as fields. For five of the seven, the date the
manual link carried was the record date, the new ISIN's first day; the
Ex-Date is one trading day earlier.

| Symbol | Ex-Date | Ratio, as the Remarks state it | Circulars stating both |
|---|---|---|---|
| KOTAKGOLD | 2015-04-13 | Face Value Split Rs.100/- to Rs.10/- | NSE/CML/29412, NSE/CMPT/29422 |
| IGOLD | 2016-06-09 | Sub-division of Equity Shares from Rs. 100/- to Rs. 10/- | NSE/CML/32526, NSE/CMPT/32519 |
| ICICIGOLD | 2018-11-15 | Sub-Division from Rs. 10/- to Re. 1/- | NSE/CML/39383, NCL/CMPT/39367 (39366 is ICICINXT50's) |
| KARDA | 2020-07-02 | Sub-Division from Rs. 10/- to Rs. 2/- | NSE/CML/44835 (NCL/CMPT/44827 gives the same Ex-Date but misprints "into Re. 2/- each") |
| ICICIBANKN | 2022-09-01 | Sub-Division from Rs. 10/- to Re. 1/- | NSE/CML/53470, NCL/CMPT/53447 |
| JSLL | 2025-06-12 | Sub-Division from Rs. 10/- to Rs. 2/- | NSE/CML/68416, NCL/CMPT/68489, NSE/CMTR/68251 ("Split Ratio 5:1") |
| GICL | 2025-10-15 | Sub-Division from Rs. 10/- to Rs. 5/-, and "Bonus Ratio 1:1" | NSE/CML/70656 for the split; NSE/CMTR/70599 alone for the bonus ("Final Adjustment Factor (A x B) = 4") |

GICL's bonus circular, NSE/CML/70874, gives the record date (2025-10-15)
and the deemed allotment (2025-10-16) but no ratio and no Ex-Date; only the
market-lot circular states them. Its row's subject is that circular's "Bonus
Ratio 1:1", which `interpret` now reads as it reads "Bonus 1:1"; no stored
subject says "Bonus Ratio", so no stored action is read differently.

### TIDEWATER's 2016 bonus

No NSE circular found states the bonus's Ex-Date or ratio. The circular
search for 2016-01-01 to 2016-04-30 returns 817 circulars, four of them
Tide Water Oil's: NSE/CML/31924 and NSE/CMPT/31946 (the split, ex
2016-03-16), NSE/CML/31978 (the Change in ISIN, which states the split's
Ex-Date and ratio only), and NSE/CML/32044, which admits "Equity shares of
Rs. 5/- each issued under Bonus" (1,742,400 shares allotted 2016-03-18) and
gives neither. None of February to April's circulars has "bonus" in its
subject. So TIDEWATER has no row: a row for the split alone (factor 1/2)
would be refused against the close's fall of 4.12-fold. It keeps its manual
link, and its Adjusted Series keeps the fall (#15).

### Two of the seven are in NSE's lists after all

NSE files KOTAKGOLD's split under GOLD1 (as #33 found) and KARDA's under
DHARAN, KARDA's Symbol since 2025 (`index=equities`, ex 2020-07-02, ISIN
INE278R01034, the Instrument's third). The builder cannot reach either,
since neither record carries the Symbol or an ISIN of the change it would
explain. The Adjusted Series can: it reads every action filed under any of
the Instrument's Symbols. With the manual links joining their histories
(#33), their series had no fall. Ingested, both rows are reported as
superseded by those records, with the same factors (1/10, 1/5), and are not
applied. They still join the ISIN changes in place of the manual links. The
other five were in no list, and their series fell 4 to 10-fold at the
Ex-Date until now.

### Outcome

On the #29 database, on 2026-10-22: `ingest cited-actions` stored all eight
rows, refused none (every factor within 20 percent of the close's move), and
reported KARDA's and KOTAKGOLD's as superseded, with no anomaly. With the
seven same-Symbol manual links removed, the rebuild made 516
corporate-action links (509) and 26 manual (33). The 10 unexplained ISIN
changes are the 10 reported before, and no Universe month became stale. Read
as of 2026-10-19, from ten days before each Ex-Date to five after, the
largest daily move in the seven Adjusted Series is 8.6 percent (JSLL, a
week before its Ex-Date). At the Ex-Dates the moves are 0.5 to 7.1
percent.

## Listing dates later than an Instrument's first row (#32)

Researched on 2026-10-21 against the #29 database (4,449 Instruments), NSE's
circulars (read through NSE's circular search and nsearchives), and seven
archived copies of NSE's equity master (`EQUITY_L.csv`) from the Internet
Archive, dated 2015-01-02, 2015-03-10, 2018-12-19, 2021-05-16, 2022-01-02,
2023-04-28 and 2025-07-26. 197 Instruments have a listing date after their
first Raw Series row, as #29 counted. No policy is decided here.

### The five ever-members

Each master date is the "with effect from" date of an NSE circular that
admitted the shares to dealings. In four of the five the earlier rows are
the same shares trading on NSE under the "permitted to trade" (PTT)
category, open to companies listed on another exchange but not on NSE. The
fifth is a readmission after a capital reduction.

| Instrument | Master date | Our rows | What the date is | Source |
|---|---|---|---|---|
| SUBEX@2020-11-05 | 2020-11-05 | SUBEX (INE754A01014) 2015-01-01 to 2020-10-21; nothing for 10 trading days; SUBEXLTD (INE754A01055) from 2020-11-05 | Readmission after a capital reduction. The shares allotted under the NCLT-approved reduction (face value Rs 10 to Rs 5) were admitted from 2020-11-05 as SUBEXLTD in BE under a new ISIN, then moved to EQ from 2020-11-19. The only one of the five where the Symbol, ISIN and series all change on the date. | NSE/CML/46245 (2020-11-03); NSE/CML/46277 (2020-11-05) |
| NESTLEIND@2023-08-01 | 2023-08-01 | EQ, INE239A01016, every trading day from 2015-01-01, with no break on the date | PTT to listed. The circular is titled "New Listing" and admits the shares from 2023-08-01. Nestlé's own disclosure says they were already traded at NSE as PTT in cash and F&O and were in the Nifty 50. The master now shows the date under INE239A01024, the ISIN from the split from Rs 10 to Re 1 (ex 2024-01-05 in NSE's Corporate Actions), so the date carried across the ISIN change. All six archived masters from 2015-01 to 2023-04 gave 08-JAN-2010. | NSE/CML/57743 (2023-07-28, ref 0946/2023); Nestlé India's Regulation 30 disclosure citing NSE letter NSE/LIST/123 (2023-07-28) |
| FORCEMOT@2024-02-14 | 2024-02-14 | EQ 2019-08-19 to 2023-10-25; nothing for 76 trading days; EQ from 2024-02-14 | PTT, withdrawn, then listed. Admitted to PTT dealings from 2019-08-19, withdrawn from PTT from 2023-10-26, newly listed from 2024-02-14. The archived masters of 2021-05, 2022-01 and 2023-04 gave 03-NOV-1994, the day NSE's capital market segment opened, while it traded as PTT. | NSE/CML/41893 (2019-08-16); NSE/CML/58560 (withdrawal from 2023-10-26); NSE/CML/60618 (2024-02-12, ref 0245/2024) |
| CAMS@2021-05-07 | 2021-05-07 | EQ, INE596I01012, from 2020-10-05, with no break on the date | PTT to listed. CAMS listed on BSE alone on 2020-10-01: NSE Investments had held shares before the IPO, which ruled out an NSE listing then (company press release). It was PTT on NSE from 2020-10-05 and listed from 2021-05-07 "on account of change in admission type from 'Permitted for Trade' category to 'Listed' category". The master now shows the date under INE596I01020, the ISIN from the 5-for-1 split (ex 2025-12-05). | NSE/CML/45904 (2020-10-01); NSE/CML/48190 (2021-05-05, ref 0477/2021); CAMS filing of 2021-05-07 |
| SHAKTIPUMP@2015-01-29 | 2015-01-29 | EQ, INE908D01010, from 2015-01-01, with no break on the date | PTT to listed. A "New Listing" from 2015-01-29. Before that it was PTT as a company listed on the Madhya Pradesh Stock Exchange (MPSE): NSE/CML/28574 withdrew PTT dealing in MSE- and MPSE-listed companies from 2015-01-30, Shakti Pumps among them. The archived master of 2015-01-02 gave 25-OCT-2011; that of 2015-03-10 gave 29-JAN-2015. | NSE/CML/28696 (2015-01-27, ref 90/2015); NSE/CML/28574 (2015-01-06) |

None of the five has a row before its master date that belongs to a
different security. Before the date the ISIN is the same in all but SUBEX,
whose earlier ISIN is the same company's shares before the reduction.

### What NSE's "DATE OF LISTING" means

No NSE definition of the field was found. NSE's "Securities Available for
Trading" page links the file but defines none of its columns. NSE's data
description (`Data_details_CM.pdf`) says only "Masters: Database masters,
listing out symbols, series, ISINs, etc." What NSE's own documents do show:

- **The date follows the latest admission to dealings, whatever its type.**
  Listing circulars and PTT circulars use the same wording ("further
  admitted to dealings on the National Stock Exchange (Capital Market
  segment) with effect from", under Regulation 3.1.1). The master date
  matches the latest such circular for each of the five. It also matches
  for all 105 Instruments keyed 2026-04-20, which is the date of the
  PTT admission in NSE/CML/73797, and for the eight of the 197 keyed
  2026-08-17, the PTT admission in NSE/CML/75748. So the master gives PTT
  admissions as "DATE OF LISTING" too.
- **The date is overwritten when the admission changes.** The archived
  masters show this directly. VALIANTORG was 05-OCT-2020 in May 2021 (its
  PTT admission, NSE/CML/45904) and 23-JUL-2021 from January 2022.
  WESTLIFE was 19-AUG-2019 (PTT, NSE/CML/41893) until April 2023 and
  05-JUN-2023 by July 2025. SHAKTIPUMP went from 25-OCT-2011 to
  29-JAN-2015, and FORCEMOT from 03-NOV-1994 to 14-FEB-2024.
- **The date survives a split or a rename.** It stays with the security
  while the Symbol or ISIN it is shown under changes. In 72 of the 197 the
  master shows its date under a (Symbol, ISIN) pair whose first row comes
  after that date (NESTLEIND and CAMS above, and renames such as
  CNL/CREATIVE and WORTHPERI/WORTH).
- **The file has no SME securities.** None of our 783 SM or 723 ST symbols
  has a master date, and the July 2025 archived master has no SM row. An SME
  company appears in the master only when it moves to the main board, with
  the migration date. NSE's circulars call that a listing on the main board
  "pursuant to migration from SME Emerge platform" (e.g. NSE/CML/41915,
  Krishana Phoschem, from 2019-08-22).

From these facts, the field reads as the date the security was last
admitted to dealings in NSE's capital market segment (main board), and not
as its first day of trading on NSE. That reading is inferred. NSE does not
state it.

What the older dates in the archived masters mean is not established:
NESTLEIND's 2010-01-08, SHAKTIPUMP's 2011-10-25, SUBEX's 2003-09-05 (the
2015 masters), and the 1994-11-03, 1994-12-28 and 2000-11-08 that
FORCEMOT, KENNAMET, GOODYEAR and NOVARTIND carried while trading as PTT.
FORCEMOT kept 1994-11-03 in 2021 although NSE/CML/41893 had admitted it
afresh in 2019 and we hold no FORCEMOT row from 2015 to 2019-08-16.

### The 197 by likely cause

Read from local data only, comparing each Instrument's last row before its
master date with its first row on or after it (rows through the
Instrument's intervals; EQ preferred on a day with two series):

| Cause, as the rows show it | Instruments |
|---|---|
| SME to main board: last row before the date in SM or ST, first on or after it in EQ or BE (150 SM to EQ, 2 SM to BE, 3 ST to BE) | 155 |
| No change on the date: same Symbol and ISIN, a break of at most one trading day (NESTLEIND, CAMS, SHAKTIPUMP, VALIANTORG, WESTLIFE, ...; BEARDSELL and AMRUTANJAN also move EQ to BE that week) | 18 |
| Same Symbol and ISIN, back after a break of 35 to 2,861 trading days | 23 |
| Symbol, ISIN and series all change on the date (SUBEX) | 1 |
| **Total** | **197** |

- **Series change** on the date: 159 (the 155 SME migrations, BEARDSELL,
  AMRUTANJAN, SAYAJIHOTL, SUBEX). **Symbol change**: 1. **ISIN change**: 1
  (both SUBEX). 17 of the 197 had more than one Symbol and 70 more than one
  ISIN over their history, but every such change falls away from the master
  date, except SUBEX's.
- **Breaks in rows before the date.** Of the 155 SME migrations, 129 have
  no missing trading day, 16 miss 1 to 5, and 10 miss 6 to 102 (ARVEE 102,
  PANSARI 84). Of the 42 main-board cases, 24 have a break of more than 5
  trading days. Nine stop trading on or before 2015-01-29, the last day
  before NSE/CML/28574 withdrew PTT dealing, and return in 2026 after 2,777
  to 2,861 trading days (AMARJOTHI, BIMETAL, GKB, IPRINGLTD, LAKSHMIMIL,
  MMWL, RAJPALAYAM, SAMBANDAM, SAYAJIHOTL, all named in that circular).
  Nine stop on 2023-10-25, the withdrawal date of NSE/CML/58560, and return
  later (FORCEMOT in 2024, the other eight in 2026). Six others: BESTAGRO
  (withdrawn by NSE/CML/57699 from 2023-07-27), VISHAL, SICAGEN, ARIHANT,
  DHANVARSHA, and SUBEX's 10 days.
- **Clusters by date.** The 42 main-board first rows bunch on PTT admission
  dates (2019-08-19: 7, 2020-10-05: 4, 2021-02-22, 2021-08-05 and
  2022-05-10: 2 each). 20 of the 42 master dates are the 2026 PTT
  readmissions (12 on 2026-04-20 and 8 on 2026-08-17). For those the
  master date is a fresh PTT admission, not a listing.
- **Archived masters.** For 35 of the 42 main-board cases an earlier
  archived master gives an earlier date for the same Symbol, in every case
  on or before the Instrument's first row. Three are in no earlier snapshot
  and four show the same date. Of the 155 SME migrations, 131 show the same
  date in every snapshot that has them and 24 are in none.

No single row pattern covers the 197. Two causes explain all but SUBEX,
both by NSE's own circulars. One is SME migration (155), where the earlier
rows are an NSE SME listing. The other is a change of PTT status (41),
where the earlier rows are NSE trading without an NSE listing, sometimes
with a gap of up to eleven years. No row of the 197 before its master date
was found to belong to a different security: the ISIN is the same either
side of the date in all but SUBEX. The per-Instrument PTT reading for the
main-board cases rests on the date patterns and the circulars named above.
Every one of the 42 was not checked against its own circular.

### Facts that bear on listing age

- **The rule reads today's master for every month.** `symbols.listing_date`
  holds one value per (Symbol, series), and each `ingest eod` overwrites it
  with the master of that day. A month before the master date therefore
  counts a listing age of zero (`count(*)` of calendar days on or after a
  later date). ADR 0005 asks for "the instrument master as of then" in a
  reconstructed Universe. Archived masters exist on the Internet Archive for
  some months, not all.
- **Months lost to it under `ded30b1014c4`.** Over the 120 stored months,
  one of the 197 was barred by listing age alone (at least 114 of the
  window's 126 days in EQ, last row in EQ, listed fewer than 126 trading
  days)
  in 836 Instrument-months, across 76 Instruments. 724 of those months come
  before the master date itself. Only 91 of them had window traded value at
  or above that month's 200th member. They are NESTLEIND, 87 months from
  2016-11 to 2024-02 (its window value was 3 to 12 times the 200th member's
  in 2020 and 2021), and CAMS, 4 months from 2021-08 to 2021-11. NESTLEIND
  is a member under both stored rule versions only from 2024-03, and CAMS
  only from 2021-12. #29's "none loses a month" compared the two versions
  with each other, and both read the same master date.
- **Listing age and history observed differ most in the PTT gaps.** The nine
  2015-withdrawn Instruments have about 20 rows from January 2015 and then
  none for eleven years. Their first row is 2,878 trading days before their
  2026 PTT readmission, but their presence in any window comes only from
  2026 rows. The SME 155 have their whole SME history before the date in
  series the Universe does not rank (SM, ST).
- **The equity master is not the only NSE source of a first date.** For
  every PTT case the admission circulars give dated evidence. The first PTT
  admission and the later listing are separate circulars (CAMS: NSE/CML/45904
  then NSE/CML/48190). Tradeline ingests no circulars today.

### Queries

Run with `psql` on the #29 database. The 197:

```sql
SELECT i.key, i.listed_on, min(ii.first_day) AS first_row
FROM instruments i JOIN instrument_intervals ii ON ii.instrument_key = i.key
GROUP BY i.key, i.listed_on HAVING i.listed_on > min(ii.first_day);
```

For each, the last row before `listed_on` and the first on or after it are
taken from `eod_bhavcopy` joined to the Instrument's intervals, and compared
for Symbol, ISIN and series. The break between them is counted in trading
days (distinct `trade_date`). The months lost were found by recomputing, for
each stored month under `ded30b1014c4`, each of the 197's EQ days, last
series, traded value and listing age over the 126-day window, and comparing
the value with the month's lowest stored member. The archived masters were
compared by Symbol, for every Symbol of the Instrument whose master date is
its key.

### Unresolved

- What the earlier master dates mean (NESTLEIND 2010-01-08, SHAKTIPUMP
  2011-10-25, SUBEX 2003-09-05, the 1994 and 2000 dates of FORCEMOT,
  KENNAMET, GOODYEAR, NOVARTIND). No circular was found for them.
- Whether `EQUITY_L.csv` lists a PTT security while it trades as PTT, in
  general. The archived masters list FORCEMOT, GOODYEAR, VALIANTORG and
  WESTLIFE during PTT periods, but NSE does not say so.
- The 41 PTT cases other than the four ever-members are classed by date
  patterns and the batch circulars named above (NSE/CML/28574, 41893, 45904,
  58560, 57699, 73797, 75748), not each against its own listing circular.
- NSE's own definition of "DATE OF LISTING": none found.

## MAZDOCK's Kite candles before its listing (#34)

Researched on 2026-10-22 against the #29 database, Kite's public instrument
dump (`https://api.kite.trade/instruments/NSE`, fetched that day), NSE's
archives (the legacy bhavcopy `cm04DEC2017bhav.csv.zip`, the UDiFF bhavcopies
of 2024-01-02 and 2026-10-19, and the security master
`nsearchives.nseindia.com/content/cm/NSE_CM_security_19102026.csv.gz`), NSE's
circulars (listed through `www.nseindia.com/api/circulars` with the repo's
`NseSession`, read at `nsearchives.nseindia.com/content/circulars/<dept><number>.pdf`),
and Kite Connect's documentation. No Kite login was used. No code, config or
stored data was changed.

**The 8 bars are Farmax India Limited's (FARMAXIND, INE890I01035) BZ-series
bars, served under the instrument token NSE reassigned to MAZDOCK, with
MAZDOCK's 2024 split applied to them.** The match to NSE's rows is cited
(each volume is exactly twice FARMAXIND BZ's traded quantity that day, on all
8 days and no others). That NSE token 509 was FARMAXIND BZ's in 2017 and 2018
is inferred: no public file from those years carries tokens.

### The bars

`candles_daily` stores no token. The bars were loaded on 2026-10-21 (ingest
run `84359aa1`, range 2016-10-21 to 2026-10-20) through MAZDOCK's token in
`symbols`, 130305, and are stored as Kite returned them. Beside NSE's rows for
FARMAXIND BZ on the same days:

| Day | Kite under 130305: O, H, L, C | Kite volume | NSE FARMAXIND BZ: O, H, L, C | NSE traded qty | Trades |
|---|---|---|---|---|---|
| 2017-12-04 | 0, 0, 0, 0 | 7,154,666 | 0.05 each (prev close 1) | 3,577,333 | 32 |
| 2018-01-01 | 0, 0, 0, 0 | 670,998 | 0.05 each | 335,499 | 4 |
| 2018-02-05 | 0, 0, 0, 0 | 2,430,000 | 0.05 each | 1,215,000 | 10 |
| 2018-03-05 | 0, 0, 0, 0 | 2,012 | 0.05 each | 1,006 | 2 |
| 2018-04-02 | 0, 0, 0, 0 | 100,000 | 0.05 each | 50,000 | 5 |
| 2018-05-07 | 0, 0, 0, 0 | 20,000 | 0.05 each | 10,000 | 1 |
| 2018-06-04 | 0, 0, 0, 0 | 840,852 | 0.05 each | 420,426 | 14 |
| 2018-07-02 | 0, 0, 0, 0 | 166,400 | 0.05 each | 83,200 | 7 |

- **Not MAZDOCK.** MAZDOCK's first bar, 2020-10-12, is O 107.45, H 108.5,
  L 86, C 86 on 79,858,408 in Kite, against NSE's 214.90, 216.95, 171.95,
  171.95 on 39,929,204 under INE249Z01012. No MAZDOCK or INE249Z ISIN row
  exists in `eod_bhavcopy` before 2020-10-12.
- **They are the only zero-price candles stored.** No other bar in
  `candles_daily` has a zero open, low or close.
- **Kite applied MAZDOCK's split to them.** MAZDOCK split Rs 10 to Rs 5, ex
  2024-12-27 (NSE's Corporate Action, factor 1/2). Kite doubles the volume of
  every bar before it: all 1,045 of MAZDOCK's own pre-split bars are exactly
  twice NSE's traded quantity, and so are the 8. Halved, FARMAXIND's 0.05 is
  0.025, a tie between 0 and 0.05; Kite rounds ties to the even 5 paisa tick
  ("Kite daily history depth and adjustment" above). On MAZDOCK's own
  pre-split bars, all 1,394 prices that halve to a tie land on the even tick.
  So the zeros are 0.05 adjusted and rounded, not missing prices (inferred
  from that rule; Kite does not document it).
- **The dates are FARMAXIND's trading days.** All 8 are the first Monday of
  the month. NSE/CML/36332 (2017-11-17) and NSE/CML/36374 (2017-11-24, ref
  1147/2017) moved FARMAXIND to BZ for non-compliance with SEBI (LODR)
  Regulations 27(2) and 31, the second effective 2017-12-04, the first BZ
  day. Both note that its shares were "not available for trading w.e.f.
  August 08, 2017 due to GSM framework under Stage VI. Trading in such
  securities shall be permitted once a month (i.e. First Monday of the
  month)." FARMAXIND had entered the Graded Surveillance Measure in NSE/SURV/34321
  (2017-03-03) and stayed in it in NSE/SURV/35167 (2017-06-21). Its BE bars
  of June to October 2017 (also first Mondays, 0.05 to 0.10) are not in
  Kite's answer for 130305, which fits a separate token per series
  (inferred).
- **FARMAXIND ended before MAZDOCK listed.** Its last row is 2018-07-02. It
  was compulsorily delisted from 2020-07-10 (NSE/CML/44686, 2020-06-18;
  NSE's delisted list as stored in `delistings`) and moved to NSE's
  Dissemination Board (NSE/CML/45009). MAZDOCK was announced for listing by
  NSE/CML/45978 (2020-10-08) and listed on 2020-10-12, 94 days after the
  delisting.

### Which security token 509 belonged to

- **Today: MAZDOCK EQ.** Kite's dump gives `130305,509,MAZDOCK,...,EQ,NSE,NSE`
  (instrument token, exchange token). NSE's UDiFF bhavcopies give
  `FinInstrmId` 509 for MAZDOCK EQ on 2024-01-02 (INE249Z01012) and on
  2026-10-19 (INE249Z01020), so the token survived the split's ISIN change.
  NSE's security master of 2026-10-19 lists 509 as MAZDOCK EQ with
  `ListgDt` 1286928000, which is 2020-10-12 counted in seconds from
  1980-01-01.
- **Kite's token is NSE's token times 256, plus 1.** All 10,235 NSE-segment
  rows of the dump satisfy `instrument_token = exchange_token * 256 + 1`
  (130305 = 509 * 256 + 1). The formula is observed in the dump, not stated
  in Kite's documentation.
- **In 2017 and 2018: not established from a primary file.** NSE's legacy
  bhavcopy has no token column (`cm04DEC2017bhav.csv` lists `FARMAXIND,BZ,
  0.05,...,3577333,...,INE890I01035`). The UDiFF bhavcopy starts in January
  2024 and the security master on the archives host in July 2024 (requests
  for 2018 to January 2024 dates return 404). The Internet Archive holds no
  Kite dump from before 2020-08-07, and that capture is cut off at 1 MB in
  the BCD segment.
- **What NSE's master shows about how 509 was assigned.** The tokens around
  it went to securities listed between 2017-11-17 and 2017-11-30
  (HDFCLIFE's series at 470 to 479, AISL 482 to 494, VERTOZ 500 to 506, PULZ
  507 to 515, ICICIB22 522, INFY BB 535, UTI fund units at 541 to 545),
  interleaved with securities listed on 2020-10-12 (MAZDOCK EQ, RL, BE, BL,
  IQ at 509, 511, 514, 521, 524; ICEMAKE at 489 to 504; UTIAMC at 527 and
  532; STINDIA BZ 481; KSERASERA BZ 484). FARMAXIND's BZ series was created
  for 2017-12-04, inside that late-November allocation, and its bars sit at
  509. That NSE gave FARMAXIND BZ token 509 in November 2017 and gave it to
  MAZDOCK EQ when it listed is inferred from this pattern and from the bar
  match. No NSE document read says so.
- **What the owners say about reuse.** Kite Connect's instrument
  documentation (`kite.trade/docs/connect/v3/market-quotes/`) says to key
  stored instruments on "a combination of exchange and tradingsymbol as the
  unique key, not the numeric instrument token. Exchanges may reuse
  instrument tokens for different derivative instruments after each
  expiry." Its historical documentation (`kite.trade/docs/connect/v3/historical/`)
  says only that exchanges flush futures and options tokens at every
  expiry. Neither mentions equity tokens. No NSE document on reusing
  capital-market tokens was found; Zerodha's support pages were not
  searched.

### Other Instruments with Kite candles before their first NSE row

Three, counted over every candle in `candles_daily` against its Instrument's
first Raw Series row (query below):

| Instrument | First NSE row | Kite candles before it | Span |
|---|---|---|---|
| RELIANCE@1995-11-29 | 2015-01-01 | 3,740 | 2000-01-03 to 2014-12-31 |
| INFY@1995-02-08 | 2015-01-01 | 2,949 | 2003-01-01 to 2014-12-31 |
| MAZDOCK@2020-10-12 | 2020-10-12 | 8 | 2017-12-04 to 2018-07-02 |

RELIANCE's and INFY's are the history-depth probe of 2026-10-21 (range from
1995-01-01, above). They come before the Raw Series starts (2015-01-01), not
before the listing, and are outside every Reconcile range so far. MAZDOCK is
the only Instrument with Kite candles before both its first row and its
listing, and the only one whose earlier candles are another security's.
Counted against `listed_on` instead, the four ever-members of #32 also have
earlier candles (NESTLEIND 1,677, FORCEMOT 1,038, SUBEX 886, CAMS 146); those
are the same shares' PTT or pre-reduction trading, with NSE rows on the same
days. Every candle in `candles_daily` maps to an Instrument; none is filed
under a bare Symbol.

### Facts that bear on reporting such candles

- **The token is the only thing joining the 8 bars to MAZDOCK.** They carry
  no Symbol, ISIN or series of their own: Kite files history by instrument
  token, and the repo looks the token up by MAZDOCK's name in today's dump
  (`ingest/candles.py`, "Which token"). ADR 0016 joins two stretches of NSE
  history only by a dated, sourced link, because a wrong join "silently
  mixes two companies' prices". Here Kite has made that join upstream, and
  MAZDOCK's Instrument has no NSE row to place the bars against.
- **NSE's rows already place them.** The same 8 days are FARMAXIND BZ rows
  in `eod_bhavcopy`, inside the Instrument FARMAXIND@2015-01-01 (BZ interval
  2017-12-04 to 2018-07-02). FARMAXIND has no candles, so Reconcile never
  compares it.
- **Listing Age does not read candles.** ADR 0017 counts from the earliest
  master date or the first Raw Series row, both NSE's, so MAZDOCK's Listing
  Age starts on 2020-10-12 whatever Kite returns. The Adjusted Series is
  built from NSE's rows, not Kite's, so it does not carry the 8 bars either.
- **Kite's values are not the earlier security's prices.** Kite adjusted
  them by a split of the later security, so they are neither FARMAXIND's
  prices (0.05) nor MAZDOCK's. A bar of a reused token can come back zero
  when the earlier security was a penny stock.
- **The class can recur.** NSE's master shows tokens refilled by later
  listings (MAZDOCK, ICEMAKE and UTIAMC on 2020-10-12 in the 470 to 545
  range). Any Instrument fetched through a reused token may have such bars,
  dated before its first row. Today only MAZDOCK does, of 464 Instruments
  with candles.

### Queries

Run with `psql` on the #29 database. The 8 bars and FARMAXIND's rows those
days:

```sql
SELECT k.trade_date, k.open, k.close, k.volume, e.symbol, e.series, e.isin,
       e.close, e.traded_qty, e.trades
FROM candles_daily k
LEFT JOIN eod_bhavcopy e ON e.trade_date = k.trade_date AND k.volume = 2 * e.traded_qty
WHERE k.symbol = 'MAZDOCK' AND k.trade_date < '2020-10-12' ORDER BY 1;
```

On 2018-01-01 and 2018-05-07 one other row also matches on quantity (GHCL
EQ, SKSTEXTILE SM); FARMAXIND BZ is the only match on all 8. Instruments with
candles before their first row:

```sql
WITH first AS (SELECT instrument_key, min(first_day) AS first_row
               FROM instrument_intervals GROUP BY 1)
SELECT c.instrument, f.first_row, count(*), min(c.trade_date), max(c.trade_date)
FROM candles_daily_instruments c JOIN first f ON f.instrument_key = c.instrument
WHERE c.trade_date < f.first_row GROUP BY 1, 2 ORDER BY 3 DESC;
```

The same against `instruments.listed_on` gives the second count. Kite's
rounding on MAZDOCK's pre-split bars: every pair of a Kite price and NSE's
price on the same day before 2024-12-27 whose NSE price is an odd 5 paisa
tick, tested for `round(kite / 0.05) % 2 = 0`. The token arithmetic was
checked over the dump with `instrument_token = exchange_token * 256 + 1`.
FARMAXIND's circulars were found by downloading every circular whose subject
mentions Z category, GSM, Regulation 33 or non-compliance (2017), or
delisting or suspension (2018 and 2020), and searching the text.

### Unresolved

- **Token 509 in 2017 and 2018** is inferred, not read. A Kite dump or an
  NSE security master from those years would settle it; none public was
  found.
- **Not checked with Kite (needs a Session Login).** With a fresh access
  token: whether `historical/130305/day` for 2017-01-01 to 2018-12-31 still
  returns the 8 bars; whether any FARMAXIND series returns bars under
  another token (delisted, so not in the dump); and whether a minute
  request for 2017-12-04 returns minute bars.
- **FARMAXIND's halt after 2018-07-02**: no suspension circular was found in
  the subjects searched (July to December 2018). Its delisting in 2020 is
  cited.
- **Zerodha's support pages** were not searched for equity token reuse.
- **How such candles are reported** is #34's second checkbox, for the
  Operator.

## The Trading Calendar (#57)

Researched on 2026-10-22 against NSE's circulars and checked against the #29
database (end-of-day files 2015-01-01 to 2026-10-19). The circular search
`nseindia.com/api/circulars` was read with `dept=CMTR` (which also returns
some NSE/MSD circulars) and with `dept=MSD`, for December 2014 to October
2026: 1,855 and 662 circulars. Every subject naming a holiday, muhurat, live
or Special Session, Saturday or Sunday, budget or election was read, and the
PDFs at `nsearchives.nseindia.com/content/circulars/<dept><number>.pdf` were
fetched and read for each row of `trading_calendar.csv`.

### Sources

- **Holidays.** The Capital Market segment's annual "Trading holidays for the
  calendar year" circular: NSE/CMTR/28337 (2015), 31297 (2016), 33746
  (2017), 36475 (2018), 39612 (2019), 42877 (2020), 46623 (2021), 50560
  (2022), 54757 (2023), 59722 (2024), 65587 (2025) and 71775 (2026). Only
  the weekday holidays are rows; each circular also lists those falling on a
  weekend.
- **Holidays added or moved later**: NSE/CMTR/40536 (2019-04-29,
  Parliamentary Elections), NSE/MSD/42319 (2019-10-21, Maharashtra Assembly
  Elections), NSE/CMTR/57285 (Bakri Id moved from 2023-06-28 to 2023-06-29),
  NSE/CMTR/60338 (2024-01-22, a public holiday under the Negotiable
  Instruments Act), NSE/CMTR/61518 (2024-05-20, Parliamentary Elections),
  NSE/CMTR/64960 (2024-11-20, Maharashtra Assembly Elections) and
  NSE/CMTR/72260 (2026-01-15, Maharashtra Municipal Corporation Elections).
- **Muhurat sessions**, each cited with the annual circular that names its
  day: NSE/CMTR/31047 (2015-11-11), 33424 (2016-10-30), 35993 (2017-10-19),
  39216 (2018-11-07), 42403 and its revised timings 42483 (2019-10-27; 42403
  is a scanned PDF with no text), 46230 (2020-11-14), 50050 (2021-11-04),
  54023 (2022-10-24), 59124 (2023-11-12), 64628 (2024-11-01), 70319
  (2025-10-21) and 76941 (2026-11-08).
- **Union Budget sessions**: NSE/CMTR/28939 (Saturday 2015-02-28), 43290
  (Saturday 2020-02-01), 65729 and NSE/MSD/66381 (Saturday 2025-02-01), and
  NSE/CMTR/72349 (Sunday 2026-02-01).
- **Saturday sessions of 2024**: NSE/MSD/59999 announced 2024-01-20 as a
  live session with an intraday switch to the DR site, and NSE/MSD/60340
  held it on the primary site at regular timings instead. NSE/MSD/60677
  (2024-03-02) and 61893 (2024-05-18) were held with the switch to DR.

The calendar has 170 holidays and 19 Special Sessions. A weekday muhurat
(2015, 2017, 2018, 2021, 2022, 2024, 2025) is a Special Session, not a
holiday, though the annual circular lists its day among the holidays as
"Diwali Laxmi Pujan*": NSE published an end-of-day file for each. NSE's
extended live sessions on weekdays (NSE/CMTR/32304 and the "Extended LIVE
TRADING session" circulars) and its weekday live sessions from the DR site are normal trading days, so they
are not rows. Mock trading sessions publish no file and are not rows.

### Circulars and files

Every one of the 166 weekdays without a file from 2015-01-01 to 2026-10-19
is a holiday a circular names, and every one of the 11 weekend files is a
Special Session a circular announced. #13's 167 weekdays ran to 2026-10-20,
Dussehra. No file is lost and none was re-ingested.

No circular and file disagree once the later circulars are read. Three
days would disagree with the annual circular alone:

- **2023-06-28 and 2023-06-29.** NSE/CMTR/54757 made 2023-06-28 the Bakri Id
  holiday; NSE/CMTR/57285 moved it to 2023-06-29 the day before, after
  Maharashtra's notification. The files agree with the move: 2023-06-28 has
  a file and 2023-06-29 has none.
- **2024-01-22** is in no annual circular. NSE/CMTR/60338 declared it on the
  Friday before (2024-01-19), when the Saturday 2024-01-20 session had
  already been called; the files have 2024-01-20 and not 2024-01-22.

The election holidays (2019-04-29, 2019-10-21, 2024-05-20, 2024-11-20,
2026-01-15) are likewise absent from their annual circulars and named in a
later one.

### Outcome

`tradeline integrity --from 2015-01-01 --to 2026-10-19` on the #29 database,
on 2026-10-22: PASS, 2,923 trading days checked, 0 day failures. The
calendar covers the rest of 2026 from NSE/CMTR/71775 and 76941: holidays
2026-10-20, 2026-11-10, 2026-11-24 and 2026-12-25, and the muhurat session
on Sunday 2026-11-08.

### Unresolved

- **Later changes to 2026.** A holiday NSE adds or moves after 2026-10-22,
  or a session it calls, is not in the calendar; the file check names such a
  day once its file is ingested (or found missing).

## Large Move records (#58)

Reviewed on 2026-10-22 against the #29 database (end-of-day files 2015-01-01
to 2026-10-19). `tradeline integrity --from 2015-01-01 --to 2026-10-19`
with a header-only `large_moves.csv` found 43 Large Moves among the 517
ever-members, all unexplained: no split or bonus falls on any of them, since
the Adjusted Series takes out every split and bonus it has. The spec's 82
were raw EQ closes; read as #56 measures them (the Adjusted Series, across
the listing series), a split's day and a series move no longer show, and 43
remain. A move on 2015-01-01, the range's first day, would not be seen.

For each, NSE's circulars from 30 days before to 3 days after the move
(`nseindia.com/api/circulars`, every department) were searched by company
name, and NSE's corporate announcements for the Symbol around the day
(`nseindia.com/api/corporate-announcements`). Each cited circular or filing
was fetched and read; the three image-only circulars (NSE/CMTR/39430,
NSE/SURV/40123, NSE/SURV/41288) were read as images.

### Counts by reason

| Reason | Moves |
|---|---|
| demerger | 26 |
| market | 14 |
| rights | 1 |
| dividend | 1 |
| resumed | 1 |
| unexplained | 0 |

- **demerger (26).** Each is the Ex-Date of a scheme in NSE's Corporate Action
  list ("Scheme Of Arrangement", "Composite Scheme Of Arrangement" or
  "Demerger"), and an NSE circular names the day. For an F&O stock, either
  NSE's F&O adjustment "on account of scheme of arrangement" states the
  ex-date (ADANIENT 2015, IDFC 2015, ARVIND 2018, PEL 2022) or NSE held a
  pre-open call auction for the scheme that day (ABIRLANUVO, CROMPGREAV,
  SINTEX, KPIT, CENTURYTEX, TATACHEM; from 2025 a Special Pre-Open Session:
  SIEMENS, ABFRL, TATAMOTORS, VEDL). Otherwise NSE moved the stock to
  trade-for-trade (BE) "Pursuant to Scheme of Arrangement" from that day
  (JSL, IIFL, TATACOMM, ANANTRAJ, EDELWEISS, NIITLTD, SANOFI, RAYMOND twice,
  STAR, SHANKARA, HEG). That is why 12 of the 26 moves are in BE.
- **rights (1).** IDEA 2019-03-29, its 87:38 rights issue at Rs 12.50. The
  theoretical ex-rights price, (38 x 29 + 87 x 12.50) / 125 = 17.50, is
  close to the 18.25 close. NCL/CMPT/40552 adjusts F&O for it.
- **dividend (1).** MAJESCO 2020-12-23, the interim dividend of Rs 974 per
  share paid from the sale of its US business. The board outcome was filed
  with NSE on 2020-12-15. The close fell from 985.65 to 12.20.
- **resumed (1).** BCG 2025-07-14 (the Instrument LYCOS named). It was
  suspended from 2024-06-14 and resumed in BE (NSE/CML/68967). The 241 gap
  days of LYCOS below are that suspension.
- **market (14).** None has a corporate action. Each cites the exchange's
  record of the day where one exists: NSE's news verification or
  price-movement query and the company's reply, an NSE circular, or a
  regulator's release.
  - UNITECH 2015-06-03: loan default rumours. NSE's press release of
    2015-06-04 records the clarification.
  - RCOM 2018-05-17: up on talk of a settlement with Ericsson. NSE's news
    verification records it.
  - DHFL 2018-09-21: default rumours. NSE's news verification records them,
    and DHFL's reply says it had not defaulted.
  - INFIBEAM 2018-09-28: Infibeam filed a voluntary clarification that day.
  - RCOM and RPOWER 2019-02-04: RCOM's board chose NCLT resolution
    (NSE/SURV/40123). Reliance Power's press release said the move did not
    affect it.
  - JETAIRWAYS 2019-06-18 and 2019-06-20: lenders took it to insolvency, and
    the NCLT admitted it two days later. NSE queried the news and the price
    move, and moved it to trade-for-trade (NSE/SURV/41288).
  - IBULHSGFIN 2019-09-30: the Delhi High Court PIL. The company's filing of
    2019-09-27 records it.
  - YESBANK 2020-03-06: the RBI moratorium (its press release of
    2020-03-05).
  - YESBANK 2020-03-17: the Reconstruction Scheme in force, and trading in
    the new ISIN (NSE/CML/43833).
  - IDEA 2020-03-18: the Supreme Court's AGR order that day. Vodafone Idea's
    filing of 2020-03-19 records it.
  - IBULHSGFIN 2020-03-19: no exchange record of the day was found. The
    reviewer's note cites the March 2020 market-wide fall and the company's
    filings on its Yes Bank loans.
  - POLICYBZR 2026-09-24: IRDAI's consultation paper "Recalibrating Economics
    of Insurance Distribution". PB Fintech's reply to NSE's price-movement
    query calls the fall market-driven.

No move turned out to be a split or bonus missing from NSE's lists, so no
Cited Action was added and none is unresolved.

### Outcome

`tradeline integrity --from 2015-01-01 --to 2026-10-19` with the 43 records,
on 2026-10-22, gave PASS:

- 2,923 trading days, 0 day failures;
- 517 ever-members and 43 Large Moves, all explained (26 demerger, 14 market,
  1 rights, 1 dividend, 1 resumed);
- 0 unexplained, 0 unresolved records and 0 unmatched records.

The 2,675 gaps fall across 21 Instruments. They are reported and never
failed: FCEL 449, HDIL 334, RCOM 295, LYCOS 241, JPASSOCIAT 227, FRETAIL 218,
RELCAPITAL 194, SINTEX 180, EROSMEDIA 108, SREINFRA 103, RELINFRA 79,
FORCEMOT 76, MEP 61, ABAN 50, PIPAVAVDOC 27, IBREALEST 12, SUBEX 10,
UPERGANGES 5, PRAKASH 3, BEPL 2 and GOLDINFRA 1.

### Unresolved

- **No Large Move is left unexplained.**
- **Weaker citations.** Two market records rest on a reviewer's note rather
  than an exchange record of the day. IBULHSGFIN 2020-03-19 has none at all.
  For RPOWER 2019-02-04 the only record is its own press release that RCOM's
  move did not affect it.
- **Later moves.** A Large Move after 2026-10-19, or a new ever-member's
  earlier one, fails the check until a record is added.

## Ten-year Integrity Check and Kite Parity (#59)

Run on 2026-10-22 on the #29 database, with #17's code (ADR 0018), the
reviewed Trading Calendar (#57) and the Large Move records (#58), against
live Kite responses. The range is #13's and #29's, 2016-10-21 to 2026-10-20.

| Step | Result | Runtime |
|---|---|---|
| `migrate` | 0025 (`candle_requests`), applied before the run. | under 1 s |
| `integrity` (it reads no candles, so it ran first) | **Pass.** Below. | 22 s |
| `ingest candles --interval day --from 2016-10-21 --to 2026-10-20`, naming each of the 464 Symbols already holding candles | 928 requests, each recorded: 464 (Symbol, series) pairs, merged to one range each. 1,007,665 bars seen, **1,435 written** (expected 0; BAFNAPH, below). 823 (Symbol, series) pairs without a token, skipped and not recorded. 0 anomalies. | 5 min |
| `parity` | **Fail: 2.09 percent** unexplained against 0.1. Below. | 2 min 19 s |

### The re-fetch wrote BAFNAPH's history

A named Symbol is fetched under every series it has a token for. BAFNAPH
had candles only for July and August 2024, which is why #13 and #29
counted 998 of its NSE rows as missing candles. Its BE
token returned 1,435 bars more, from 2020-03-02. The spec put fetching its
history out of scope, but the bars are kept: their requests are recorded,
so without them every one of those days would read as a bar Kite was asked
for and did not return. BAFNAPH is now compared like any other Instrument,
which is why "not fetched" is 0 below.

### Integrity

`tradeline integrity --from 2016-10-21 --to 2026-10-20`: PASS.

- 2,478 trading days, 0 day failures; the calendar covers the range.
- 517 ever-members; 37 Large Moves in the range, all explained by records
  (21 demerger, 13 market, 1 rights, 1 dividend, 1 resumed). 0 unexplained,
  0 unresolved, 0 unmatched and 0 duplicate records. #58's other six moves
  fall before 2016-10-21.
- 2,667 gaps across 18 Instruments, reported and never failed: FCEL 449,
  HDIL 334, RCOM 295, LYCOS 241, JPASSOCIAT 227, FRETAIL 218, RELCAPITAL
  194, SINTEX 180, EROSMEDIA 108, SREINFRA 103, RELINFRA 79, FORCEMOT 76,
  MEP 61, ABAN 50, PIPAVAVDOC 27, IBREALEST 12, SUBEX 10, PRAKASH 3.

### Parity beside #29

`tradeline parity --from 2016-10-21 --to 2026-10-20`: 1,008,709 rows
compared, 44,755 differences, 21,069 unexplained (**2.09 percent**): FAIL.

| Kind | Parity Reason | #29 (reconcile) | #59 |
|---|---|---|---|
| extra | series move | 13,596 | 14,033 |
| extra | unexplained | 8 (MAZDOCK) | 0 (MAZDOCK's 8 are Pre-listing Candles, #34) |
| missing | Special Session | 1,044 (muhurat) | 1,044 |
| missing | not fetched | 998 (BAFNAPH) | 0 (BAFNAPH fetched, above) |
| missing | unexplained | 8 | 8 |
| close | additive adjustment | 29,670 (both causes, not split out) | 5,238 |
| close | coarse rounding | (in the row above) | 3,371 |
| close | unexplained | (not split out) | 21,061 |
| | **total** | **45,324 (4.50 percent)** | **44,755; unexplained 21,069 (2.09 percent)** |

Series moves rose by 437 with BAFNAPH's BE history in the comparison.

- **Missing, unexplained (8).** The same single days as #29: CEBBCO
  2018-12-03 to 2018-12-05, DWARKESH 2019-01-08, SUBEX 2019-01-08, FORCEMOT
  2019-08-19, IIFLWAM 2019-10-04 and MCDOWELL-N 2019-11-08. NSE has a row,
  and Kite has no bar inside a requested range. Cause not found.
- **Close, unexplained (21,061),** in 274 stretches of 45 Instruments. Every
  stretch has at least five bars, so none fails the additive rule for being
  short. Each fails it on the half-tick limit:

| Max distance from the least-squares fit, per stretch | Unexplained closes |
|---|---|
| at most 3 paisa | 6,817 |
| over 3, at most 5 paisa | 4,109 |
| over 5, at most 10 paisa | 7,906 |
| over 10 paisa | 2,229 |

  - **Additive, then rounded (18,931).** Kite's close is a × NSE's close − b
    (ADR 0018's additive adjustment: a scale a and an offset b), and one a
    and b hold across consecutive stretches, often for years: GNFC a = 0.932,
    b = 43.33 (1,742 closes); BANKBARODA 0.936, 7.11 (1,668); CANBK 0.187,
    3.01 (1,641); ASHOKLEY 0.500, 2.48 (1,149). The same holds for HINDPETRO,
    VEDL, MPHASIS, M&MFIN, BPCL, TECHM, TATASTEEL, HEROMOTOCO, POWERGRID,
    NMDC, IOC and GAIL. Kite's closes there are on a 5 paisa tick
    (BANKBARODA, ASHOKLEY, GNFC, HINDPETRO), a 10 paisa one (MPHASIS,
    TECHM), or finer (CANBK, TATASTEEL, IOC, GAIL). A rounded price can be
    half its tick from the true line, and a least-squares line drawn through
    rounded prices is itself a little off it, so the largest gap lands just
    above 2.5 paisa: 2.7 to 3.1 paisa for CANBK, TATASTEEL, BANKBARODA and
    ASHOKLEY. On a 10 paisa tick it reaches about 10 paisa (MPHASIS,
    TECHM). GNFC, HINDPETRO, M&MFIN and BPCL reach 5.2 to 9.1 paisa on a
    5 paisa tick, more than rounding alone accounts for; why was not
    established. The rule as written (ADR 0018) cannot pass any of these.
  - **Additive with coarser rounding (2,076).** OFSS (1,531, up to
    51 paisa), BSE, NIITTECH, GODFRYPHLP, TATAINVEST, MCX and NAUKRI: fits
    off by 25 to 57 paisa, on a 10 paisa tick or coarser. These look like
    additive history rounded coarser than Kite's usual tick, to the rupee or
    the half-rupee. ADR 0018 agrees only rounding to ₹1, and only on its
    own, never after an additive fit.
  - **Isolated closes (54).** 16 Instruments with at most three unexplained
    closes each (34 closes: DMART, SBILIFE, PNBHOUSING, AUBANK, TEJASNET,
    DIXON, HINDZINC and others), and 20 SHANKARA closes in one stretch whose
    fit is about ₹6 off. Cause not found.

### Outcome

The Integrity Check passes: nothing in NSE's data as we hold it is lost or
unexplained. Kite Parity fails at 2.09 percent, and nearly all of that is
one cause, Kite's additive adjustment rounded to its own tick, which the
half-tick limit was set too tight to recognise. Even a limit of one 10 paisa
tick would explain 18,832 of the 21,061 and leave 2,229 close mismatches
and the 8 missing days unexplained (0.22 percent), still a fail.

### Unresolved

- **The additive rule's limit.** Half a tick from a least-squares fit does
  not allow for Kite's rounding of the adjusted price. A rule that does,
  for example a fit within one of Kite's ticks, or one a and b shared
  across an Instrument's consecutive stretches, needs ADR 0018 to agree
  it. It is not given here.
- **Additive history rounded coarser** (OFSS, BSE, MCX, TATAINVEST,
  GODFRYPHLP, NIITTECH, NAUKRI): a follow-up decides whether one reason may
  combine the additive fit with rupee rounding.
- **The 8 missing single days and the 54 isolated close mismatches**:
  causes not found.
- **Fetching every series of a named Symbol** filled BAFNAPH's BE history
  on a run meant to write nothing. A re-fetch meant only to record requests
  would need the (Symbol, series) pairs already held, which `ingest candles`
  cannot be asked for today.

## Kite's Adjustment as a chain of stages (#62)

These are read-only measurements on the #59 database (above). They cover the refit of the 274 unexplained stretches, the prototype on `prototype/kite-adjustment-chain` (8ba0bb5), and one measurement under the rules ADR 0018 now agrees. Nothing was written to the database, and neither Kite nor NSE was called. The findings were first posted on #62 (comments 6347194917 and 6347839034).

### Two corrections to #59

- **"A least-squares line drawn through rounded prices is itself a little off it, so the largest gap lands just above 2.5 paisa."** Wrong. The exact minimax line, the one with the smallest possible worst gap, is barely closer: CANBK 2.58 to 2.73 paisa against least squares' 2.69 to 2.94, BANKBARODA 2.70 to 2.85 against 2.88 to 3.06. Minimax within half a 5 paisa tick explains 2,693 of the 21,061 closes. Minimax within half the tick a stretch's own closes show explains 2,497 (parity 1.84 percent). The gap is Kite rounding more than once.
  - TATASTEEL: 99.8 percent of its closes are 0.97721 × v rounded to the paisa, where v lies on a 5 paisa grid and fits one line within 2.25 paisa.
  - INOXWIND and TRIDENT match the same test on 100 percent of their closes.
  - The prototype reproduces CANBK, IOC and GAIL exactly as chains of 4, 7 and 7 stages. GNFC, HINDPETRO and BPCL need 4 to 6 stages, mixing subtracted and divided dividends.
- **"Fits off by 25 to 57 paisa, on a 10 paisa tick or coarser" (OFSS, BSE, NIITTECH, GODFRYPHLP, TATAINVEST, MCX, NAUKRI).** Wrong for five of the seven. Their closes show a ₹0.50 tick (OFSS, BSE, NIITTECH, NAUKRI) or a ₹1 tick (GODFRYPHLP, TATAINVEST, MCX). The minimax line sits within half that tick for NIITTECH (25.00 paisa), NAUKRI (24.50 to 24.96), GODFRYPHLP, TATAINVEST and MCX (at most 50.00). It does not for OFSS (37.7 to 46.8 paisa against 25) or for 5 of BSE's 12 stretches. The replay reproduces OFSS's 1,531 closes and 716 of BSE's 717.
  - Several isolated misses in this group are exact half-tick ties of the median factor. One is NSE 1112.5 × 0.2 = 222.5, where Kite shows 222.

Also: the 274 stretches belong to 44 Instruments (46 Symbols), not 45.

### What a stage is

At each Ex-Date Kite adjusts for, it rewrites every older close as round(close / r − b) to that stage's tick and tie rule. Older closes pass through every later stage.

- **Splits and bonuses** divide by their own ratio, sometimes rounded to two decimals. A 1:3 bonus divides by 1.33 (ASTRAL twice, GAIL twice, OIL, WIPRO).
- **Dividends with an Ex-Date before July 2023** were never adjusted. MAJESCO's 98.8 percent payout looked like an exception: its divisor turned up as a fitted stage at a buyback six weeks earlier, which the amended rules refuse (below).
- **From July 2023 to September 2024**, a dividend was usually subtracted. That is the whole of #59's "additive adjustment": TATASTEEL ₹3.60, OFSS ₹240, GNFC ₹30 and then ₹16.50. ONGC (2023-11-21), PETRONET (2023-11-10), BPCL (2024-08-09) and SSLT (2024-09-10) were divided by the ratio instead. Forcing subtraction there fails them, and every older stage in their chains.
- **From October 2024**, a dividend divides by P / (P − D) rounded to six decimals. With the exact divisor, ITC is a paisa off on one close.
- **Which dividends Kite adjusts** has no sharp edge. Roughly those of 2 percent or more of P are adjusted, but some of 1.8 to 2 percent are and some of 2 to 4.5 percent are not.
- **Rights issues, demergers and other actions** have no rule for the ratio, so it is read from Kite's closes.
- **Ticks** come from the ladder 1p, 5p, 10p, ₹0.50, ₹1 and ₹5 (ABBOTINDIA). A later stage can be finer than an earlier one (TATASTEEL: 5p, then 1p).
- **Ties** follow one rule per stage. Of the prototype's 351 standing stages, 295 round half to even, 32 half down (TATASTEEL's 2022 split, GAIL's and HDFCBANK's 2019 actions) and 24 half up. Floating-point division with ties to even leaves 2,338 of 20,834 closes off on the split and bonus stages where ties show. Exact arithmetic with a tie rule per stage leaves 3 of 26,577.

### Measurement under ADR 0018's amended rules

| | |
|---|---|
| Dataset | The #59 development database: migrations to `0025_candle_requests`, 928 `candle_requests`, latest candle 2026-10-21 |
| Range | 2016-10-21 to 2026-10-20: 1,008,709 rows compared, 6,468 stretches, 464 Instruments |
| Code | `prototype/kite-adjustment-chain` at 599126f (`replay_run.py`'s `final_strict` rule set, `score.py`) |
| Unexplained | **841 (0.0834 percent)**: 833 closes and the 8 missing days |

The 745 unexplained (0.0739 percent) first posted on #62 came from the prototype's earlier strict run, before these rules were settled. That count included closes in a Kite Basis of any size. Requiring 20 bars raises it to 842 (the prototype's `report.py` against `score.py` on the same replay), and the amended rules give 841.

```sh
O=prototypes/kite_adjustment_chain/out; mkdir -p $O   # git-ignored: the pickles hold Kite's closes
uv run --env-file .env python prototypes/kite_adjustment_chain/extract_all.py $O/all.pkl   # read-only, about 40 s
uv run --with scipy python prototypes/kite_adjustment_chain/replay_run.py $O/all.pkl $O/baseline.pkl 0.05 strict
RULES=final uv run --with scipy python prototypes/kite_adjustment_chain/replay_run.py $O/all.pkl $O/final.pkl 0.05 strict
RULES=final_strict uv run --with scipy python prototypes/kite_adjustment_chain/replay_run.py $O/all.pkl $O/final_strict.pkl 0.05 strict   # about 6 min
uv run python prototypes/kite_adjustment_chain/score.py $O/final_strict.pkl $O/final.pkl
```

| #59's outcome | Explained by Kite's Adjustment |
|---|---|
| unexplained | 20,469 of 21,061 |
| additive adjustment | 5,237 of 5,238 |
| coarse rounding | 3,131 of 3,371 |
| all close mismatches | 28,837 of 29,670 |

- **Unexplained by cause:** 782 closes in the 112 stretches (of 6,468) that do not stand. 43 off the replay in standing stretches. 8 on a listing day. 8 missing days. None from a Kite Basis under 20 bars.
- **Unexplained by Instrument:** NMDC 546, BAJFINANCE 215, SHANKARA 20, M&MFIN 16, SSLT 6, VBL 4, and at most 3 each for the rest.
- **Computed stages:** 189 split or bonus at its exact ratio, 6 at a ratio rounded to two decimals, 23 subtracted dividends, 83 divided dividends, and no stage that only rounds again (3 of the 301 are in a Kite Basis under 20 bars).
- **Fitted stages:** 50: 28 at rights issues, 21 at demergers and schemes, and 1 at a general meeting (COALINDIA 2020-09-14, r = 1.008698, 0.5 percent from the nearest value a computed action gives).
- **Closes depending on a fitted stage:** 2,848 explained closes (0.28 percent of rows) go through at least one.

**Fitted stages that stood in for computed ones.** Under the first reading of the rules (`final`), a fitted stage could sit at any Ex-Date between two stretches and fold in a split or dividend from another Ex-Date there. Of `final`'s 56 fitted stages, 49 had a gap to themselves (every rights and demerger stage among them). The other 7 had a split, bonus or dividend in their gap. The table shows them, with every fit the amended rules refuse because it only rounds again:

| Instrument | Fitted at | r in `final` | What it did | Under the amended rules |
|---|---|---|---|---|
| IIFLWAM | 2022-09-22 AGM | 4.000221 | absorbed the 2023-03-02 1:1 bonus and 2-for-1 split (r = 4) | the split is its own computed stage; the AGM's remaining fit, r = 1.000000, is refused |
| MAJESCO | 2020-11-12 buyback | 84.597234 | absorbed the ₹974 dividend of 2020-12-23, P/(P − D) = 84.605150 | refused; 7 more stretches (1,004 bars) do not stand |
| MOTHERSUMI | 2022-01-14 demerger | 1.676522 | absorbed the 2018-10-30 1:2 bonus (1.5) | the bonus is its own stage; the demerger's fit is 1.117684 |
| COALINDIA | 2020-09-14 AGM | 1.008696 | nothing: 0.5 percent from the nearest dividend's value | kept, 1.008698 |
| COALINDIA | 2017-08-31, 2018-09-04 AGMs; 2019-02-14 buyback | 0.999949, 0.999983, 1.000023 | only rounded again | refused |
| TATACOMM | 2019-09-17 | 1.0005 | only rounded again, in a gap of its own | refused |

None of these changes a close Kite Parity flags: `final` and the amended rules both leave 841 unexplained. What changes is which matched closes the replay reproduces, and which stretches stand. A bounding run allowing no fitted stage at all where a computed action shares the gap (`final_nofit_in_mixed_gaps`: 49 fitted stages, 116 stretches not standing) also leaves 841.

The repository's tests pass on the prototype branch (508, `uv run pytest -q`; they use throwaway `tradeline_test_*` databases).

A fitted stage's ratio is chosen to reproduce the closes it explains. That those closes match is therefore not independent evidence for it, and the replay has not been checked against data it was not fitted on.

### The implementation's acceptance run

These are the results of `tradeline parity` itself, not the prototype. Its one reason for a close mismatch is Kite's Adjustment (`tradeline.kite_adjustment`). The run uses the same database and range as the measurement above.

| | |
|---|---|
| Command | `uv run tradeline parity --from 2016-10-21 --to 2026-10-20` (read-only) |
| Result | 1,008,709 rows, 44,755 differences, **841 unexplained (0.0834 percent): PASS** |
| Missing | 1,052: 1,044 Special Sessions, 8 unexplained |
| Extra | 14,033, all series moves |
| Close | 29,670: 28,837 Kite's Adjustment, 833 unexplained |
| Through a fitted stage | **2,848** of the explained closes (0.28 percent of rows) |
| Kite Bases | 111 in the JSON summary (the text lists 100): 106 with stages, and 5 of Kite's raw closes that leave a close unexplained |
| Where the 833 unexplained closes are | 783 in 45 stretches that do not stand (NMDC 546, BAJFINANCE 215, SHANKARA 20, HINDZINC 1, MAJESCO 1), 37 in Kite Bases with stages, 13 in Kite's raw closes: the first days after listing of DMART and HUDCO (3 each), SBILIFE and TEJASNET (2 each), and CROMPGREAV's 8 to 10 March 2017 (3), the CGPOWER closes noted above under that key |
| Ambiguous fits, bounded searches | 0 and 0 |
| Time | 10 min 53 s on the development machine (524 s of CPU, NMDC alone most of it) |

The count matches the prototype's, but that alone does not show each choice below is right.

**Where the implementation departs from `final_strict`.** Each change was measured with the same extract replayed both ways, and none moved a count.

- **Arithmetic and actions.** Arithmetic is exact throughout. An action filed under two Symbols or series is applied once. Dividends on one Ex-Date are summed before the guard against an amount at or above NSE's close; the prototype guarded each amount separately.
- **A dividend's P** is NSE's close on the last day before the Ex-Date that NSE has one, whether or not Kite has a candle that day. The prototype took the last day with both closes. This is an inferred rule: it reproduces Kite's closes exactly (ITC's 343 among them), but Kite publishes no reference-price convention.
- **A fitted stage is placed only where one Ex-Date of its gap could hold it.** The prototype put the ratio at the latest of several. Where several could hold it and one would let the stretch stand, nothing in Kite's closes says which action the ratio belongs to. So none is placed, the stretch's closes stay unexplained, and the report lists the case as an ambiguous fit. This run has none: every fitted stage behind the 2,848 closes sits in a gap with one fittable Ex-Date.
- **A gap with more than 64 layouts** is still searched one Ex-Date's choice at a time. The report now lists each such search and whether its stretch stood. This run has none: every mixed gap among the replayed Instruments has 64 layouts or fewer.
- **A fitted ratio is refused where it repeats any split, bonus, consolidation or dividend of its gap, staged or not.** The prototype compared it only with the gap's actions left without a stage, so a fitted ratio could apply a staged action a second time.
- **The report gives every unexplained close mismatch its place.** Each sits in a stretch that does not stand, in a Kite Basis with stages, or in a basis of Kite's raw closes, and each of the three is listed with its days.

**A known limit kept.** A group of stages found for one gap shares one tick among its computed stages, and one tie rule among all of them. ADR 0018 gives each stage its own. No group of more than one stage stands in any replayed Instrument of this run, so the choice changes no close here. A tick and tie rule per stage would multiply the search by 18 for each stage after the first. A test pins the limit: a bonus rounded to the rupee followed by a dividend subtracted to the paisa, in one gap, is not reproduced.

**A limit not measured.** A gap with no fittable action gets one computed stage at most, however many of its Ex-Dates hold a split or dividend. The prototype had the same rule. Whether any close would need two such stages was never measured.

### Unresolved

- **BAJFINANCE before mid-2025:** closes on ₹0.50 some days and ₹1 on others (893.5 and 942.5, but 899 for 898.7). No stage, tick or tie rule reproduces them.
- **NMDC before 2024-02-27:** Kite's close wanders 0.2 to 2 percent from any constant multiple of NSE's.
- **Listing-month closes** (SHANKARA, DMART, SBILIFE, AUBANK, PNBHOUSING and others): ₹1.5 to ₹6 off in either direction, while the rest of each stretch is reproduced.
- **Which dividends Kite adjusts,** and how it picks a tie rule.

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
- Listing dates (#32): NSE circulars NSE/CML/46245 and 46277 (SUBEX), 57743 (NESTLEIND), 41893, 58560 and 60618 (FORCEMOT), 45904 and 48190 (CAMS), 28696 and 28574 (SHAKTIPUMP), 57699, 73797, 75748 and 41915, at nsearchives.nseindia.com/content/circulars/CML<number>.pdf or .zip, found through nseindia.com/api/circulars; Nestlé India's Regulation 30 disclosure of the NSE listing (nestle.in, special announcements); CAMS's press release filing of 2021-05-07 (nsearchives.nseindia.com/corporate/CAMS_07052021180955_Pressrelease.pdf); NSE's "Securities Available for Trading" page (nseindia.com/static/products-services/equity-market-securities-available-for-trading); NSE's About Equities page for the capital market segment's opening on 1994-11-03 (nseindia.com/static/products-services/about-equity-market); NSE's data description (archives.nseindia.com/content/press/Data_details_CM.pdf); archived copies of NSE's EQUITY_L.csv at web.archive.org (snapshots 20150102124557, 20150310012511, 20181219143033, 20210516062344, 20220102154932, 20230428005156, 20250726163846)
- ISIN changes (#33): NSE's Corporate Action API (nseindia.com/api/corporates-corporateActions with `index=equities`, `sme` and `mf`); NSE circulars NSE/CML/29389, 29412, 31924, 31978, 32044, 32517, 32526, 35394, 35452, 39361, 39363, 39383, 39385, 42873, 42888, 44773, 44835, 45054, 45074, 47279, 47320, 47344, 47347, 47665, 47721, 48556, 48595, 48928, 48964, 50053, 50106, 50329, 50382, 50496, 50529, 50859, 50905, 53206, 53260, 53404, 53470, 58455, 58523, 58972, 59009, 60421, 60492, 60740, 60870, 61766, 61914, 66863, 68231, 68416, 70527, 70656, 70874, 72553, 72617, 72620, 72718, 72916, 72980, 73020, 73115, 73816, 73909, 74765, 74911, 74912, 75102, 75236, 75432, 75678, 75868, NSE/CMPT/31946 and NCL/CMPT/44827, at nsearchives.nseindia.com/content/circulars/, found through nseindia.com/api/circulars; Jeena Sikho Lifecare's and Globe International Carriers' filings at nsearchives.nseindia.com/corporate/, found through nseindia.com/api/corporate-announcements
- Trading Calendar (#57): NSE's circular search (nseindia.com/api/circulars, `dept=CMTR` and `dept=MSD`, December 2014 to October 2026); NSE circulars NSE/CMTR/28337, 28939, 31047, 31297, 33424, 33746, 35993, 36475, 39216, 39612, 40536, 42403, 42483, 42877, 43290, 46230, 46623, 50050, 50560, 54023, 54757, 57285, 59124, 59722, 60338, 61518, 64628, 64960, 65587, 65729, 70319, 71775, 72260, 72349, 76941 and NSE/MSD/42319, 59999, 60340, 60677, 61893, 66381, at nsearchives.nseindia.com/content/circulars/<dept><number>.pdf (57285 and 60338 are .zip)
- Large Move records (#58): NSE's circular search (nseindia.com/api/circulars, all departments, 30 days before to 3 days after each move) and corporate announcements (nseindia.com/api/corporate-announcements); NSE circulars NSE/CMPT/29829, 30815, NSE/CMTR/30850, 31553, 31832, 34910, 39430, 39976, 42322, 43646, 67392, 68037, 70614, 73856, NSE/SURV/31133, 40123, 41288, NSE/CML/41079, 42085, 43833, 45883, 56859, 56872, 62383, 62751, 65322, 67935, 68967, 70275, 76106, NCL/CMPT/39442, 40552 and 53443, at nsearchives.nseindia.com/content/circulars/<dept><number>.pdf; NSE's press release of 2015-06-04 (nsearchives.nseindia.com/content/press/04062015.htm); company filings at nsearchives.nseindia.com/corporate/ (DHFL, RCOM, RPOWER, IHFL/IBULHSGFIN, YESBANK, IDEA, MAJESCO, POLICYBZR, as cited in large_moves.csv) and Infibeam Avenues' voluntary clarification of 2018-09-28 (avenuesai.com); RBI press release of 2020-03-05 (rbi.org.in, prid=49476)
