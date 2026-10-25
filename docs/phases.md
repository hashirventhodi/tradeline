# Phases and acceptance criteria

Each phase ships tested and deployable, and nothing in a phase is throwaway. Criteria are checkable; "works" is not a criterion.

## Phase 0: Foundations (this document set)

Done when: repo exists with GLOSSARY.md, ADRs 0001 to 0015, architecture, phases, risk controls, research. No code.

## Phase 1: Data layer and Universe

Build: Kite candle ingestion (daily and minute), NSE end-of-day file ingestion, NSE corporate actions and our adjustment, NSE and BSE announcements, RSS ingestion, sector map, Universe Rule, Postgres schema and migrations, append-only enforcement, Docker Compose for local development.

Accept when:
- Ten years of NSE end-of-day files pass the Integrity Check: a file for every day in the Trading Calendar, and every Large Move of an ever-member explained (ADR 0018).
- Ten years of daily candles are loaded for every ever-member Kite still serves. Every ever-member without candles has an End Reason in `tradeline instruments` (series moved, merged, delisted, or ended, reason not stated), and none is still trading. Kite Parity against NSE's end-of-day files has unexplained differences under 0.1% (ADR 0018; #13, below).
- Factor Agreement holds for every ever-member over the ten years: no split or bonus Ex-Date disagrees, run by `tradeline compare-adjusted` (ADR 0018; #68, below).
- The Universe Rule, run for every month in the last ten years, is reproducible: two runs produce byte-identical membership tables.
- The Conformance Check passes for every stored month under the configured rule, run by `tradeline check-universe`; the current Universe is compared with the current NIFTY 200 as a report, not a gate (#18, below).
- Announcements and RSS ingestion run for five consecutive trading days without a missed window, with duplicates removed.
- Test suite: unit tests for adjustment math and the Universe Rule; integration tests against recorded Kite and NSE fixtures; no network in tests.

### Revisions

**#18, 2026-10-23, adopted with PR #64.** The original line was:

> The current Universe overlaps the current NIFTY 200 by at least 85%; disagreements are listed with reasons.

#13 measured 77 percent (154 of 200) and failed it. The Universe Rule ranks by six-month traded value and NSE builds the NIFTY 200 by free-float market capitalisation, so their overlap measures the market as much as the rule, and every one of the 92 differences has a reason the rule gives (`docs/research.md`, "Universe acceptance criteria revisited (#18)"). The line is retired as a gate. Its replacement is:

- The Conformance Check passes for every stored month under the configured rule, run by `tradeline check-universe` over stored data with no network. A month passes only when:
  - every member is eligible under the rule;
  - members are ranked 1 to 200 (or every eligible Instrument, if fewer) by descending traded value, recomputed independently of the rule's ranking query, with ties broken by Symbol;
  - each member's stored traded value equals the recomputed one exactly;
  - every non-member with more traded value than the last member is ineligible for a reason the rule states (series, fund unit, Listing Age or presence). One with no reason fails the month.

  The check reports the excluded Instruments counted by reason, and any tie at the cut. It runs in under five minutes for the 120 months. Under the ADR 0017 version (`f4997f66b273`), stored and configured by #18, all 120 months conform.
- The current Universe is compared with the current NIFTY 200 as a report, not a gate: `verify-universe` gives the overlap and both difference lists, each name with its rank or the reason the rule excludes it.

Liquidity coverage and monthly turnover were measured and not proposed (`docs/research.md`). Reproducibility stays as written above.

**#68, 2026-10-23, adopted with PR #69.** The original line was:

> Our adjusted series matches the broker's displayed adjusted chart on a sample of 20 symbols with known splits or bonuses, to the paisa.

#13 measured 26,310 of 48,699 bars equal and failed it. The line cannot pass by design. ADR 0006 adjusts splits and bonuses only, and Kite also adjusts dividends from 2023-07-01 and demergers. Kite rounds each stage to its own tick (#62), and our rounding is kept by convention, not to match Kite. Agreement with Kite's closes is already gated by Kite Parity (ADR 0018). What our adjustment claims, and Kite can check, is the factor at each split and bonus. The line is retired. Its replacement is:

- Factor Agreement holds for every ever-member over the ten years, run by `tradeline compare-adjusted` over stored data with no network. Each split (either direction) and bonus Ex-Date of an ever-member, followed across renames, gets one outcome from Kite's Adjustment:
  - **agrees:** Kite's Adjustment stands with that Ex-Date's computed stage, our ratio exact or rounded to two decimals (ADR 0018);
  - **disagrees:** Kite's Adjustment stands across that Ex-Date only without that stage, with no stage or a fitted ratio in its place;
  - **not judged:** the replay leaves no standing stretch to judge it by, Kite has no closes on one side, the Ex-Date is after the candles were fetched, our ratio stands only when read from Kite's closes on a tick of its own, or a split or bonus there has no factor.

  The line passes when no Ex-Date disagrees. Ex-Dates not judged are listed, each with its reason, and counted, and never fail it. The bar-by-bar comparison to the paisa stays in `compare-adjusted` as detail for a named Symbol and gates nothing.

**#13, 2026-10-23, adopted with PR #71 (no build).** The original line was:

> Ten years of daily candles for every Instrument that was ever in the Universe are loaded, and Kite Parity against NSE's end-of-day files has unexplained differences under 0.1% (ADR 0018).

The coverage half cannot be met as written: 54 of the 517 ever-members have no Kite candles (`tradeline instruments`, 2026-10-23). 45 ended, reason not stated, 8 moved to BE or BZ, and 1 was delisted. Kite has no candles under a token that no longer exists, which covers all of them except INFIBEAM (below). NSE's end-of-day rows cover every ever-member for every month it was a member (#13), and the Integrity Check and the Adjusted Series read those rows. Kite's candles are only the cross-check. The coverage half is replaced by:

- Ten years of candles are loaded for every ever-member Kite still serves. An ever-member without candles is accepted only when `tradeline instruments` gives it an End Reason, showing it no longer trades in the series it was ranked in: series moved, merged, delisted, or ended, reason not stated. One the report lists as still trading fails the line, since that is a fetch that was missed.
- The 8 ever-members that moved to BE or BZ are accepted as listed: their rows continue in that series. Whether Kite would return their EQ years under any token was not measured. Fetching was considered and not done.
- INFIBEAM is listed as ended only because CCAVENUE's 2026 consolidation is not yet in NSE's Corporate Action list. Kite serves its history under CCAVENUE's token, and #31 tracks fetching it.

The Kite Parity half is unchanged. Parity compares only Instruments with candles stored, so its result covers only the ever-members that have them. #13's re-score states how many.

## Phase 2: Engine, Strategy, backtester, Gates

Build: event-driven engine with a simulated clock, Strategy interface and `pullback_v1`, cost model for Indian delivery, Paper Broker fill model against historical opens, Walk-Forward and Holdout protocol, Backtest Gate evaluation, backtest report.

Accept when:
- The same Strategy object runs in backtest mode and in a paper dry-run with no code branches on mode.
- Cost model reproduces a real Zerodha contract note for a delivery round trip to the rupee on three examples.
- Walk-Forward over the available history runs end to end and writes every window's parameters and results to the Ledger.
- The Holdout lock is enforced: a second run of the same Strategy version on the Holdout is refused by the engine and recorded as an attempt.
- The Backtest Gate is evaluated and reported honestly, pass or fail. A failing result is an acceptable phase outcome and is documented as such.
- Backtest trades carry the same schema as live trades.
- Historical Universe reconstruction is designed and built per the ADR 0005 amendment: recorded versus reconstructed mode, rule validity period, and data cutoff are distinct and stored; every backtest records the rule version, data provenance, and mode it used; an unversioned query for a date with no valid version never falls back to today's Universe.

### Revisions

**#72, 2026-10-24, grilled before the build.** The Build line gains the Risk Engine, and the acceptance criteria are restated so each is checkable. Build: event-driven engine with a simulated clock, one set of Ledger tables tagged by Engine Mode (ADR 0019), Strategy interface and `pullback_v1`, Risk Engine with Limits, sizing, Pass and Block, cost model for Indian delivery with a dated rate schedule, Paper Broker fill model against historical opens (ADR 0003 amendment), Universe reconstruction (ADR 0005 amendment), the Protocol, Walk-Forward and Holdout (ADR 0007 amendment), Backtest Gate evaluation, backtest report.

Accept when:

- The same Strategy object runs in backtest mode and in a paper dry-run (`tradeline run --mode paper`, one Run over the data `collect-daily` loaded, filled next day against the end-of-day open) with no code branches on mode: the same day run in both modes yields identical Candidates, Plans, Passes, Blocks and Verdicts.
- A Run reads the Adjusted Series and the Universe only as of its own date: a test adds a bar and a Corporate Action dated after a Run's day and the Run is unchanged.
- The Risk Engine never issues a Pass for a Plan that breaches any single Limit, shown by a property-based test over generated portfolios and Plans; five Plans that pass alone but breach gross exposure together produce exactly the right Blocks; a Pass bound to a Plan is rejected by the Paper Broker if any Plan field changes. (Moved from Phase 3.)
- The cost model reproduces hand-computed round trips from the dated rate schedule to the rupee, with trades before and after 2024-10, as its stated check. No real Zerodha contract note is available to the build; when the Operator supplies one, it is added as a test against the rates in force on its trade date, and the acceptance run says whether any was.
- A Protocol is frozen in the Ledger before any run, and the engine refuses a backtest naming none. Its Window boundaries are generated from the Trading Calendar and printed for independent checking before the freeze.
- Walk-Forward over the five Windows runs end to end and writes every Window's chosen parameters, in-sample trade count and out-of-sample result to the Ledger. One two-year backtest of one grid cell finishes in under 3 minutes and the whole Protocol in under 4 hours, measured alone on the laptop with the Compose Postgres, resumable per cell.
- The Holdout lock is enforced: a second run of the same Strategy version on the Holdout is refused and recorded as an attempt. A later version of the family is recorded as Not Independent and cannot pass the Backtest Gate without a Fresh Period of at least twelve months.
- The Backtest Gate is evaluated and reported honestly, pass or fail: Sharpe above 1.0 over the Protocol's risk-free rate, drawdown under 15 percent and profit factor above 1.3 on both the Stitched Curve and the Holdout; at least 150 trades on the Stitched Curve, the Holdout's count reported. Either failing fails. A failing result is an acceptable phase outcome and is documented as such.
- The report lists every Gapped Stop with its realised loss, every day a daily-loss or drawdown Halt would have fired (near the top, beside the Gate outcome), results with the impact coefficient at zero and at twice its value, the mode of every Universe month read, and the family lineage.
- Backtest trades carry the same schema as live trades: there is one set of tables, and the mode is a column (ADR 0019).
- Historical Universe reconstruction is built per the ADR 0005 amendment: Rule Validity is declared by the Operator, mode is derived, each month build stores its Data Cutoff, ingest run ids and content hash, and a backtest verifies the hashes of the months it reads before running.

The grilling's other decisions (the Strategy's exact rule, the grid, the fill model, Equity, metric definitions, sectors in a backtest) are in #72 and the ADR amendments it names.

**#85, 2026-10-24, the acceptance run.** The Backtest Gate **fails**, an outcome this phase accepts. `pullback_v1` under Protocol `41e47fd39af3` (commit `c5588e7`, tag `protocol/41e47fd39af3`) misses all six gated thresholds: the Stitched Curve's Sharpe is 0.073, its drawdown 33.5 percent and its profit factor 1.12 over 691 trades; the Holdout's are -1.39, 31.2 percent and 0.77 over 277 trades. The family's Holdout is consumed. The full report is on #72 and the run in `docs/research.md`, "Phase 2 acceptance run (#85)".

- **Contract notes: none supplied.** `contract_notes.csv` holds none, so the cost model's check stays the hand-computed round trips, as the line above allows.
- **Window boundaries:** checked before the freeze against the Trading Calendar file and, separately, against the trading days of the stored end-of-day files, by a script that imports nothing from the package. All 12 dates agree, and the two day sets (2,416 days) are equal.
- **Runtime: passes, after two fixes the run found.** First measured, the eight In-Sample cells recorded in 49 minutes took up to 9.5 minutes each, about 24 hours for the Protocol. Two queries were planned against Ledger tables whose statistics read empty, and walked every account's rows and the dead rows each rolled-back cell leaves (commits `3a6af7b`, `89a7531`, `b297dc5`). After them, measured alone on the laptop with the Compose Postgres: one two-year cell 155.8 s from start-up; Walk-Forward 3,592.8 s, its cells 13.7 to 119.5 s (median 15.6 s); the Holdout 147.8 s; the report with the c sensitivity 948.6 s; with the freeze (70.5 s), 79 minutes in all.
- **Three changes the run made, each in its own commit before the final freeze:** an Instrument's Raw Series is its rows in the equity listing series (`58e208f`; block deals and bonds under its Symbol were read as its bars); the two query-plan fixes, shown to change no result by eight In-Sample cells reproduced exactly under Protocol `f9285f6631ca`, which differs from the stopped `c0527f5740ff` in its commit alone; and the target priced at the Plan's own multiple of the rounded risk, rounded up (`c5588e7`), found from trade counts (98 percent of Plans at r = 1.5 Blocked for the minimum reward to risk) before any result was read.

## Phase 2b: The low-volatility family

Grilled on 2026-10-25, after #85's Gate failure; decisions in ADR 0020 and the 2026-10-25 amendments to ADR 0007 and ADR 0015. It comes before Phase 3 so that the Risk Engine, scheduler and Halts are not hardened around the swing-trade shape. Phase 2's acceptance run stays as recorded.

Build: `lowvol_v1`, family `lowvol`; Limits declared per family in the Protocol; the Relative Drawdown Halt and its threshold; dividend crediting with its parser; the demerger table and Demerger Credits; the tax ledger and Liquidation Value; Benchmark ingestion from AMFI NAV history and the factor-fund comparison; the Benchmark Gate and the report's additions; the Drift Replay; the pre-Holdout checks; the weekly post-freeze Integrity Check; the acceptance run.

The rule, frozen before any backtest:

- **Eligible:** a member of the Universe of the month containing the decision day, with 252 daily log returns over its last 253 trading days in the Adjusted Series as of the decision day, none missing. A name suspended inside that year is ineligible.
- **Ranked:** by the sample standard deviation of those log returns, lowest first, ties by Symbol.
- **Held:** 30 names at equal weight. At a Rebalance a held name leaves if it ranks below 60th (NSE's buffer of twice the count, kept literally although our eligible set is larger than NSE's, as the Protocol notes), has become ineligible, or has left the Universe; empty slots are filled with the best-ranked names not held, in rank order, until 30 are held. A name that leaves the Universe between Rebalances is held until the next one.
- **Rebalances:** decided after the close of the last trading day of March and of September. Leavers are sold at the next open; entrants are bought the trading day after, splitting the cash then held, each at most 15 percent of Equity. Continuing names are never trimmed. An account starts with an Initial Buy on its first trading day, which is not a Rebalance.
- **Grid:** one cell. The Stitched Curve is one continuous backtest over 2019-10 to 2024-09 (10 Rebalances); the Holdout 2024-10-01 to 2026-09-30 holds 4.

Accept when:

- A Protocol that leaves any Limit unstated is refused, and `pullback_v1`'s frozen Protocol still verifies unchanged.
- The rule above is shown on a synthetic fixture: eligibility with a missing day, the buffer at ranks 30, 31, 60 and 61, a held name leaving the Universe between Rebalances, ties by Symbol, sales on the Rebalance day and buys the next, cash split with the 15 percent cap, and an Initial Buy.
- Dividends: per-share, percent-of-face-value and several-in-one subjects are credited on the Ex-Date to a holder at the prior close. Over every Universe member eligible at any decision date from 2019-10 to 2026-09, every dividend subject parses or is listed with a reason, and the count is reported.
- Demergers: the table holds ITC Hotels (1 for 10, Ex-Date 2025-01-06, listed 2025-01-29, basis 86.49 / 13.51) and Jio Financial (1 for 1, 2023-07-20, listed 2023-08-21, basis 95.32 / 4.68), each citing its source, and has been checked against NSE's Corporate Action list for every eligible member over 2019-10 to 2026-09. A credit is cash at the child's first close in the Raw Series and is taxed as a sale against its basis.
- The tax ledger passes synthetic tests for: the twelve-month cut on the day before, the day of and the day after the anniversary, citing the provision; set-off of each kind of loss; eight-year carry-forward; the debit on the first trading day after March 31; cess; settlement at liquidation with unused losses dropped and reported; dividends at the configured rate; a Demerger Credit taxed against its basis; the Benchmark's own holding rule, a twelve-month period taxing it as short-term; and a later-dated schedule row taking effect on its date.
- The Benchmark is the direct-plan, growth-option code of the Motilal Oswal Nifty 500 Index Fund, from AMFI's NAV history from 2019-09-11, and the build fails if its scheme name in AMFI's master lacks "Direct" or "Growth". Its stamp duty, STT and exit load come from the dated schedule. The ICICI Prudential Nifty 100 Low Vol 30 ETF's NAV is fetched for the comparison.
- The Relative Drawdown threshold is computed from NIFTY100 Low Volatility 30 TRI and NIFTY 500 TRI history from 2005-04 to 2019-09 (NSE's archive host or niftyindices.com), or, only if neither serves it, from NSE's 2018 white paper, and the Protocol records which and that the index history before launch is backfilled.
- The Drift Replay at a commit other than the Protocol's reproduces the Stitched Curve's holdings, Fills, costs, dividends and tax ledger exactly, is recorded apart, is refused for the Holdout, and is never read by the Gate.
- The engine refuses to start the Holdout until the pre-Holdout checks pass: the contract-note test passed or explicitly accepted as unmet, the dividend parse and demerger checks above, and the tax tests.
- The acceptance run freezes `lowvol_v1` as Not Independent, with its reason, runs the Stitched Curve and the Holdout once, and reports honestly, pass or fail: annualised excess of Liquidation Value over the Benchmark above zero and drawdown no worse than the Benchmark's plus 2 points on both curves, at least 8 Rebalances on the Stitched Curve; the information ratio prominently; Sharpe, profit factor and results before tax; the factor-fund comparison; sector weights and their peaks; the largest name weight; cash at every Rebalance; every dividend, Demerger Credit and rights issue held through with its effect; the days the Relative Drawdown Halt would have fired near the top; the rate the Benchmark was taxed at; and the surcharge caveat. It cannot pass before a Fresh Period of twelve months and two Rebalances after its Initial Buy, run as a backtest.
- The weekly Integrity Check over post-freeze data alerts on a gap.

## Phase 3: Risk Engine, Paper Broker on live data, Ledger, scheduler

**Phase 2b (2026-10-25).** Phase 3 must not touch the `strategy` package: a paper Run refuses a Strategy version hash other than the frozen one, and a commit without a passing Drift Replay. Paper starts when Phase 3 is done, with its own Initial Buy, and its Gate is fidelity (ADR 0015 amendment). The Halts it builds are the operational ones and `lowvol`'s Relative Drawdown Halt.

Build: Limits and Risk Engine with Pass and Block, Halt and Flatten, Paper Broker against live Kite quotes, scheduled daily Run, Session Login flow with Data-Only Mode, Ledger completeness.

Accept when:
- A property-based test shows the Risk Engine never issues a Pass for a Plan that breaches any single Limit, across generated portfolios and plans.
- Orders are checked sequentially so that five Plans that pass alone but breach gross exposure together produce exactly the right Blocks.
- A Pass bound to a Plan is rejected by the Broker Adapter if any Plan field changes.
- Halt cancels pending Orders and blocks entries while exits still execute, demonstrated in an integration test over a simulated week.
- Twenty consecutive trading days of paper Runs complete on schedule with zero missed Runs and zero unexplained gaps in the Ledger.
- A missed Session Login produces a Data-Only day with no Orders and an alert.

### Revisions

**#72, 2026-10-24.** The Risk Engine's Limits, sizing, Pass and Block moved to Phase 2, since a backtest without them measures a portfolio live would never hold. The first three acceptance lines above (the property-based test, the sequential Blocks, the Pass bound to its Plan) are Phase 2's now. Phase 3 keeps Halt and Flatten, the daily-loss and drawdown Halts (a backtest reports the days they would have fired and never simulates them, since every Halt needs the Operator to clear it), the Paper Broker against live Kite quotes, the scheduled Run, Session Login and Data-Only Mode, and Ledger completeness. A member of the Universe in the Unclassified sector raises a stale-sector-map alert in paper and live.

## Phase 4: AI layer in Shadow Mode

**Deferred (2026-10-25, Phase 2b).** `lowvol` adds perhaps 5 to 10 names a Rebalance, so the AI Promotion Gate's 200 shadow decisions would take until about 2029. Phase 4 waits until a family trades often enough to be evaluated. ADR 0001 is unchanged.

Build: news filter, thesis, skeptic, base rates, structured output schemas, prompt versioning, shadow log, AI Promotion Gate evaluation, cost tracking.

Accept when:
- Every AI call is reproducible from the Ledger: model, prompt version, inputs, structured output, tokens, latency.
- The skeptic has no access path to the thesis writer's confidence; verified by schema and by test.
- Shadow Mode is proven inert: a Run with the AI layer on and off produces identical Verdicts.
- AI Promotion Gate evaluation runs nightly and reports sample size, Expectancy of would-be-vetoed versus kept trades, p-value, and calibration.
- Monthly AI spend stays under the configured cap; the cap is enforced by refusing further calls, not by an alert alone.

## Phase 5: Dashboard and Telegram

Build: FastAPI JSON endpoints, passkey or TOTP login, React dashboard (Positions, Verdicts with reasons, Blocks with reasons, Gate status, Ledger browser, Halt button, Flatten with confirmation), Telegram bot (alerts, digest, login link, approve or reject, Halt).

Accept when:
- Every Verdict and Block on the dashboard shows its reasons without leaving the page.
- Halt from the dashboard and from Telegram both produce a Halt row in the Ledger within five seconds and are confirmed back to the Operator.
- Flatten cannot be triggered without the typed confirmation phrase; tested.
- Dashboard renders usably at phone width.
- Unauthenticated requests to every endpoint are refused; tested.

## Phase 6: Kite Broker and production deployment

Build: Kite Broker behind the live flag, Approval flow, after-market order placement, fill reconciliation against broker positions, Terraform for AWS Mumbai, SSM secrets, GitHub Actions deploy, S3 backups and a restore drill.

Accept when:
- With the live flag off, no code path can reach Kite order placement; verified by test and by a grep-based guard in CI.
- With the live flag on in a test account, one share of a liquid stock is bought and sold with Pass and Approval, and the Ledger, the broker's order book, and the dashboard agree.
- Reconciliation detects an injected mismatch between broker positions and the Ledger and raises Halt.
- `terraform apply` from an empty account produces a working instance; `terraform destroy` removes it; documented.
- A backup is restored to a fresh Postgres and the dashboard shows the same data.
- Deployment instructions are followed by a second person without help.

## Ongoing: Gates

Backtest Gate (phase 2; for `lowvol`, phase 2b, against the Benchmark after tax, passing only with a Fresh Period), Paper Gate (90 trading days after phase 3; for `lowvol`, fidelity through two Rebalances), Live Gate (90 days after phase 6 with Approvals), AI Promotion Gate (200 shadow decisions after phase 4). Each is evaluated by the system, shown on the dashboard, and recorded in the Ledger. Failing a Gate is an expected outcome of this project and is reported as such.
