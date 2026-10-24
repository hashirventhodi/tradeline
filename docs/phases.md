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

## Phase 3: Risk Engine, Paper Broker on live data, Ledger, scheduler

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

Backtest Gate (phase 2), Paper Gate (90 trading days after phase 3), Live Gate (90 days after phase 6 with Approvals), AI Promotion Gate (200 shadow decisions after phase 4). Each is evaluated by the system, shown on the dashboard, and recorded in the Ledger. Failing a Gate is an expected outcome of this project and is reported as such.
