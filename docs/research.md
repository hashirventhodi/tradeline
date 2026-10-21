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
| Link Candidates | 11 | All end on 2015-01-29: BSE-listed companies that traded on NSE as "permitted to trade" until NSE withdrew the facility from 30 January 2015 (NSE/CML/28574), then listed on NSE directly. NSE dates them from that listing (AVANTIFEED 2015-04-15), so they are new listings, not renames. Left split. |

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
  history.
- **Explained by inference, not citation** (#33): the 21 SME and 71 fund ISIN
  changes (as classes), and TIDEWATER's and KARDA's four-fold changes (not
  explained). None touches an ever-member or an EQ Universe month.
- **MAZDOCK's 8 candles from 2017 and 2018** (#34), before its 2020 listing: an
  earlier instrument under a reused Kite token is likely but unconfirmed.
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
