# Tradeline

A long-only swing trading platform for NSE cash equities. A deterministic quant core proposes and manages trades; an AI layer may only veto or shrink them; a code-only risk engine has the final word.

## Language

### Instruments

**Instrument**:
One security NSE lists in an equity listing series (fund units such as ETFs included), followed across its Symbols, series, and ISINs, from listing to delisting. Two Instruments are never joined because their companies merged.
_Avoid_: Company (one company can become several, and several one), stock, ticker, security

**Symbol**:
The name an Instrument trades under on NSE on a given day. An Instrument can have several over its life; a Symbol names at most one Instrument on any day.
_Avoid_: Ticker, trading symbol (Kite's term for its own name of an instrument)

**Instrument Link**:
The dated, sourced evidence that joins two stretches of history into one Instrument: an NSE symbol-change record, an ISIN change on the Ex-Date of a split, bonus, or consolidation, or a reviewed manual link citing an official source. Never inferred from a name or a price.
_Avoid_: Mapping, alias, merge

**Link Candidate**:
A new Symbol trading under an ISIN an earlier Symbol had, with no NSE record or manual link joining them. Never a link: the Operator reviews it and cites a manual link, or leaves the history split.
_Avoid_: Candidate (a Strategy's Candidate), suggested link

**End Reason**:
Why an Instrument's rows in a series stop: its series moved (the rows continue in another series), it merged or was delisted (only on an official record: a scheme of amalgamation NSE lists, or NSE's delisted list), or it ended with no reason stated. Never inferred, and never reported as missing data.
_Avoid_: Missing, dropped, dead

### Universe and selection

**Universe**:
The set of Instruments eligible for trading on a given date, chosen by the Universe Rule.
_Avoid_: Watchlist, basket, index

**Universe Rule**:
The deterministic rule that defines the Universe at a point in time (currently: top 200 NSE Instruments by trailing six-month traded value, recomputed monthly). An Instrument's traded value and Listing Age count across every Symbol it has had.

**Conformance Check**:
The check that every stored Universe month is exactly what the Universe Rule selects. Every member is eligible, members are ranked by their traded value, and every non-member with more traded value than the last member is ineligible for a reason the rule states. It tests the rule's own claims, not agreement with an index.
_Avoid_: Index check, overlap check, validation

**Listing Age**:
The trading days since an Instrument was first admitted to trading on NSE, measured from the earliest supported date established using NSE listing evidence and the Instrument's first Raw Series row. NSE's current master date may reflect a later admission to the main board rather than the original listing. Listing Age is distinct from the amount of historical data held, which may contain gaps.

**Candidate**:
An Instrument in the Universe that the Strategy's screen surfaced on a given Run.
_Avoid_: Signal, pick, idea, opportunity

**Strategy**:
A named, versioned rule set that turns market data into Candidates and Plans. A Strategy version is the Strategy's code revision run under one Protocol, and is immutable once it has been run against the Holdout.
_Avoid_: Algo, system, model, bot

**Strategy Family**:
The named rule shape (`pullback`, `lowvol`) that every version of a Strategy belongs to, recorded with each version so the lineage is visible. The Holdout is consumed once for the whole family.
_Avoid_: Variant, lineage (the record of a family's versions, not the family)

**Recorded Universe**:
A month's Universe computed under a Universe Rule version that was in force and recorded before the month's first trading day: what the system itself knew then.
_Avoid_: Live universe, original universe

**Reconstructed Universe**:
A month's Universe computed later, under a Universe Rule version chosen later, from inputs dated before the month began. Every backtest records which of the two each month it read was.
_Avoid_: Backfilled universe, historical universe (both kinds are historical)

**Rule Validity**:
The date from which the Operator declares a Universe Rule version the rule in force, distinct from when the version was recorded.
_Avoid_: Effective date, recorded_at

**Data Cutoff**:
The last end-of-day date a month's Universe was built from: the trading day before the month's first.
_Avoid_: As-of (the As-Of Date is a query's date), snapshot date

### Analysis

**Thesis**:
The written case for a Candidate, with the evidence it rests on.
_Avoid_: Analysis, rationale, narrative

**Critique**:
The Skeptic's structured challenge to a Thesis and Plan, ending in a recommendation to keep, shrink, or veto.
_Avoid_: Review, second opinion, risk assessment

**Skeptic**:
The AI role that produces Critiques. It sees the Thesis, the Plan, and historical base rates; it never sees other agents' confidence scores.
_Avoid_: Risk manager (that is the Risk Engine), devil's advocate

**Base Rate**:
The historical outcome statistics for a setup, computed by the engine from past Runs, given to the Skeptic as evidence.

### Decisions

**Plan**:
The complete trade specification for a Candidate: entry price, stop, target, size, and maximum holding period. A portfolio Strategy's Plan has an entry price and size only, and is held until it leaves at a Rebalance.
_Avoid_: Setup, trade idea, order plan

**Verdict**:
The final decision on a Candidate for a Run: BUY, SKIP, WAIT, or EXIT, with the reasons.
_Avoid_: Signal, decision, call, recommendation

**Run**:
One scheduled decision cycle over the whole Universe, producing Verdicts.
_Avoid_: Tick, cycle, iteration, scan

**Rebalance**:
The scheduled Run on which a portfolio Strategy re-ranks the Universe and replaces the holdings that no longer qualify. A portfolio Strategy enters and exits only on a Rebalance or its account's Initial Buy; between them its Runs place no Orders but those a Halt or a Corporate Action requires.
_Avoid_: Rotation, reconstitution (the index's word), review

**Initial Buy**:
The first trading day of an account that starts holding nothing (a Holdout, a Fresh Period, paper): entrants chosen as at a Rebalance from data through the previous close, and bought that day. Nothing is sold and no holding continues, so it is not a Rebalance and never counts as one.
_Avoid_: Initial Rebalance, seed, first Rebalance

**Engine Mode**:
Which clock and data source an engine run uses: backtest (a simulated clock over stored history), paper, or live. The Strategy, Risk Engine, sizing and cost model never differ by it.
_Avoid_: Environment, profile

**Backtest**:
A run of the engine in backtest mode, each Run reading only what was known on its date.
_Avoid_: Simulation, replay, test run

**Equity**:
Cash plus open Positions marked at the day's close; what sizing and the Limits are measured against.
_Avoid_: Capital, account value, NAV

**Shadow Mode**:
The state in which the AI layer records what it would have done but has no effect on Verdicts.

### Risk and control

**Risk Engine**:
The code-only component that checks every Plan against the Limits and issues a Pass or a Block. Nothing reaches the Executor without a Pass.
_Avoid_: Risk manager, risk agent, guardrails

**Limits**:
The configured constraints the Risk Engine enforces: risk per trade, position count, position size, sector exposure, gross exposure, participation, daily loss, and drawdown.
_Avoid_: Rules, guardrails, parameters

**Participation Cap**:
The Limit on an Order's value as a fraction of the Instrument's recent average daily traded value, so that a fill a backtest assumes is one the market could have given.
_Avoid_: Liquidity limit, ADV cap, volume limit

**Unclassified**:
The sector of an Instrument with no recorded sector classification. The sector Limit applies to it like any other; in paper and live it means the sector map is stale.
_Avoid_: Unknown, other, no sector

**Pass**:
The Risk Engine's signed approval of one specific Plan. A Pass is bound to that Plan and expires with the Run.

**Block**:
The Risk Engine's refusal of a Plan, with the Limit that was breached.
_Avoid_: Reject, fail, veto (veto is the Skeptic's word)

**Halt**:
The state in which no new entries are placed, pending orders are cancelled, and open Positions continue to be managed by their exits (for a portfolio Strategy, holdings leaving at a Rebalance are still sold).
_Avoid_: Kill switch (the mechanism), stop, pause, freeze

**Relative Drawdown**:
The fall of a Strategy's value divided by the Benchmark's value from its running peak, both before tax and with dividends counted. The performance Halt of a portfolio Strategy watches it, since a fall the Benchmark shares is the risk that was chosen.
_Avoid_: Excess drawdown, tracking drawdown, underperformance

**Flatten**:
The explicit, confirmed command to close every open Position at market. Distinct from Halt.

### Execution

**Order**:
An instruction sent to the Broker Adapter to buy or sell a quantity of an Instrument, under the Symbol it trades under that day, on stated conditions.

**Fill**:
A confirmed execution of all or part of an Order at a price.
_Avoid_: Execution, trade

**Position**:
The net holding in one Instrument, kept as one Position across a change of Symbol, with its cost basis, its stop and target when its Plan has them, and the Plan it came from.
_Avoid_: Holding, trade

**Broker Adapter**:
The component that translates Orders into a broker's API calls and broker events back into Fills. The Paper Broker and the Kite Broker are both Broker Adapters.

**Paper Broker**:
The internal Broker Adapter that simulates Fills against real quotes with modelled slippage and the full Indian cost stack. In a Backtest its quotes are the historical opens.
_Avoid_: Simulator, sandbox, demo account

**Slippage**:
The modelled cost beyond the fill price: a fixed safety margin on the Order's value (a limit Order filled in the pre-open auction crosses no spread) plus an impact term from the Order's value relative to the Instrument's average daily traded value.
_Avoid_: Spread (there is none to cross), friction

**Circuit Lock**:
A day an Instrument's bar is pinned at its price band, open, high, low and close equal at the band, on which no Order fills.
_Avoid_: Limit-down, limit-up, freeze

**Gapped Stop**:
A stop whose exit filled below it because the next open gapped past the exit's protective band, or the day was a Circuit Lock, so the trade lost more than its Plan risked.
_Avoid_: Slippage (a modelled cost), overshoot, blown stop

**Boundary Exit**:
The forced close of every open Position at the last out-of-sample day's close, where the Stitched Curve ends and the Holdout begins. An exit with its own reason, never a stop or target.
_Avoid_: Liquidation, end-of-period close

**Approval**:
The operator's explicit yes to one Order in live mode. Required for every live Order until the Autonomy Gate passes.

### Validation

**Gate**:
A measurable, configured rule that must pass before a Strategy or the AI layer advances a stage: Backtest Gate, Paper Gate, Live Gate, Autonomy Gate, AI Promotion Gate.
_Avoid_: Milestone, checkpoint, criteria

**Holdout**:
A fixed, locked two-year period that a Strategy Family runs exactly once, by its first version. A later version's run on it is Not Independent and cannot pass the Backtest Gate alone.
_Avoid_: Test set, out-of-sample (ambiguous: walk-forward windows are also out of sample)

**Not Independent**:
A Holdout run by a later version of a family whose Holdout is already consumed, or by a first version whose family was chosen after reading published results that cover the Holdout's years. Recorded as such, with its reason, and passes the Backtest Gate only together with a Fresh Period.
_Avoid_: Invalid, tainted, contaminated

**Fresh Period**:
The trading days after both the Holdout's end and a Not Independent version's Protocol freeze: data that version's design could not have used. It must be at least twelve months long to carry a Backtest Gate, and for a portfolio Strategy must also hold at least two Rebalances after its Initial Buy.
_Avoid_: New holdout, extended holdout, forward test

**Drift Replay**:
A rerun of a frozen Protocol's Out-of-Sample Windows and any Fresh Period days at a later commit, compared with the runs at the frozen commit to show the later engine behaves the same. Never a run of the Holdout, never counted as a Window run, and never read by a Gate.
_Avoid_: Regression run, rerun, drift test

**Machinery Correction**:
A fix that changes money but could not have been informed by results: the Strategy version is unchanged, and the bug was shown first by a failing test against a record outside the system's own results. It is frozen as a new Protocol recording the correction and its evidence, and the Fresh Period continues. A change that is not one resets the Fresh Period as a new, Not Independent version.
_Avoid_: Bug fix (any fix), hotfix, engine patch

**Walk-Forward**:
The validation protocol in which parameters are chosen on a rolling window and evaluated on the window that follows it.

**Window**:
One stretch of Walk-Forward: an In-Sample Window, on which parameters are chosen, and the Out-of-Sample Window after it, on which they are evaluated.
_Avoid_: Fold, period, split

**Stitched Curve**:
The one Equity curve over every Out-of-Sample Window in order, as one continuous account with Positions carried across Window boundaries, that the Backtest Gate scores.
_Avoid_: OOS curve, combined results, concatenated equity

**Protocol**:
The frozen statement of how a Strategy version is validated: its parameter grid, objective and tie-break, Window boundaries, cost and Slippage parameters, Gate thresholds, and the data it reads. Frozen before any run and never changed; a different Protocol is a new Strategy version.
_Avoid_: Config, settings, backtest parameters, methodology

**Benchmark**:
The passive alternative a Strategy must beat to be worth running: a real index fund tracking the NIFTY 500, valued at its published NAV (so after its expenses and tracking error), bought at a period's start with the costs a buyer of its units pays, held to its end, and taxed once on sale.
_Avoid_: Index (the NIFTY 500 itself earns no fee and pays no tax), baseline, buy-and-hold

**Liquidation Value**:
What an account would hold in cash if it sold everything on a day and paid every tax its sales up to then owe. The Backtest Gate compares a Strategy's with the Benchmark's.
_Avoid_: Equity (Equity is before tax and before selling), NAV, after-tax return

**Expectancy**:
Average profit per trade after all costs, in rupees or as a fraction of equity.

### Price data

**Raw Series**:
An Instrument's daily prices and volumes in its equity listing series exactly as NSE's official end-of-day files state them, each day under the Symbol it was published under, never adjusted and never modified. Rows NSE lists under the same Symbol in another series (a block deal, the issuer's bonds, preference or partly paid shares) are not the Instrument's.
_Avoid_: Unadjusted prices, raw candles (the broker's candles are the broker's own adjusted view)

**Corporate Action**:
An event NSE lists for a Symbol, or states in a circular a reviewed row cites, that takes effect on an Ex-Date, and so for the Instrument that Symbol named that day: a split, bonus, rights issue, dividend, demerger, scheme of arrangement or amalgamation, capital reduction, buyback, or meeting. Splits and bonuses change the Adjusted Series. Rights issues, dividends, demergers, schemes and capital reductions are flagged on it, since each moves the price without being a split or bonus, and no official factor for it applies to cash equities. Buybacks, meetings and the rest are recorded only.
_Avoid_: CA, event, announcement (an announcement is a filing, which may or may not describe a Corporate Action)

**Demerger Credit**:
The cash a holder at the close before a demerger's Ex-Date is credited on the child's first listing day: the entitled shares times the child's first close in the Raw Series. It is taken from a reviewed record citing the scheme's ratio and the announced split of cost basis, and is taxed as a sale of the child's share of that basis.
_Avoid_: Demerger adjustment (nothing is adjusted), spin-off value

**Cited Action**:
A split or bonus taken from a reviewed record citing NSE's circulars, for one NSE's lists do not have. It is superseded, and not applied, when NSE's list has a split or bonus for the same Instrument within seven days of its Ex-Date; if that listed action's Adjustment Factor differs, the difference is reported for review.
_Avoid_: Cited row, manual action, manual adjustment

**Flagged Action**:
A Corporate Action that moves the price but is left in the Adjusted Series, and is carried on every bar before its Ex-Date so that a reader of those bars knows the series is not continuous across it: a rights issue, dividend, demerger, scheme, or capital reduction.
_Avoid_: Unadjusted action (a buyback or meeting is unadjusted too, but not flagged), discontinuity

**Ex-Date**:
The first trading day on which an Instrument trades without the entitlement of a Corporate Action, so the day the market price reflects it.
_Avoid_: Effective date, action date, record date (the record date is the later date on which holders are fixed)

**Adjustment Factor**:
The exact ratio by which the prices of every bar before an Ex-Date are multiplied, and its volume divided, so that the series is continuous across a split or bonus.
_Avoid_: Multiplier, ratio, adjustment

**Adjusted Series**:
The Raw Series with every Corporate Action known on an As-Of Date applied, whichever of the Instrument's Symbols it was filed under, computed whenever it is read and never stored. The only price series the engine, the backtester, and the tests read.
_Avoid_: Adjusted candles, continuous prices, clean prices

**Pre-listing Candle**:
A broker candle dated before the start of its Instrument's Listing Age. It cannot be the Instrument's own trading (a broker's token can carry an earlier security's history), so it is reported, never compared, and never attributed to another Instrument.
_Avoid_: Pre-listing history, extra (an extra is a candle on a day the Instrument was trading and NSE has no row)

**As-Of Date**:
The date on which an Adjusted Series is asked for. Only Corporate Actions with an Ex-Date on or before it are applied, and no bar after it is returned, so a Run on that date sees only what the market had already priced.
_Avoid_: Reference date, valuation date, cutoff

### Data checks

**Integrity Check**:
The check of NSE's data as we hold it against NSE itself, never against a broker: every day NSE was open has its end-of-day file, held whole, and every large move in the Adjusted Series is explained. A day an Instrument has no row in a file held whole is NSE's own record, reported but never a failure.
_Avoid_: Reconcile, reconciliation (Reconciliation is the broker's positions against the Ledger), validation

**Kite Parity**:
The comparison of Kite's daily candles with the Raw Series, in presence and close, after taking out Kite's own adjustment. Every difference either has a Parity Reason or is unexplained, and only unexplained differences count against it.
_Avoid_: Reconcile, reconciliation, chart check

**Parity Reason**:
A stated, checkable cause of a difference Kite Parity finds: a series move (NSE lists the Instrument in another series that day), a Special Session Kite has no candle for, a day outside every range Kite was asked for, or Kite's Adjustment. A difference without one is unexplained.
_Avoid_: Excuse, known issue, exception

**Kite's Adjustment**:
Kite's rewriting of an Instrument's history for its corporate actions. At each action Kite adjusts for, it divides, or subtracts from, the history it has already rounded, then rounds again to a tick Kite's closes show. As a Parity Reason it explains a close that Kite's Adjustment reproduces from NSE's raw close. A close it does not reproduce stays unexplained, however close it comes.
_Avoid_: Additive adjustment, coarse rounding (both are cases of it), Kite factor

**Kite Basis**:
A span of an Instrument's consecutive stretches between Ex-Dates over which Kite applied the same adjustment. It ends only where Kite's own closes show that Kite adjusted again.
_Avoid_: Run (a command's run), stretch (one span between two Ex-Dates), segment

**Factor Agreement**:
The check that our Adjustment Factor at each split and bonus of an ever-member is the one Kite applied: Kite's Adjustment, replayed from NSE's raw closes, reproduces Kite's closes with a stage of that ratio at that Ex-Date. An Ex-Date where Kite's closes show a different factor, or none, disagrees and fails it. One Kite's Adjustment cannot reproduce on either side is not judged, and is reported but never a failure.
_Avoid_: Chart match, chart check, adjusted chart comparison

**Large Move**:
A close-to-close change of more than 1.5 times, either way, in an Instrument's Adjusted Series across its listing series. Unless a split or bonus on that day explains it, it must be explained by a reviewed record with its reason and source, because an unexplained one may be a split or bonus the Adjusted Series lacks.
_Avoid_: Jump, spike, outlier

**Ever-member**:
An Instrument that has been in the Universe in any stored month, under any version of the Universe Rule. The Integrity Check reads the Large Moves and gaps of every ever-member, not only of today's Universe, since a backtest reads the Instruments of every past month.
_Avoid_: Constituent, current member, universe history

**Trading Calendar**:
The days NSE was open for equity trading: every weekday but the holidays NSE announced, and every Special Session.

**Special Session**:
A session NSE holds outside its normal days, such as the Diwali muhurat session.
_Avoid_: Weekend session (some are on weekdays)

### Operations

**Operator**:
The single human who approves Orders, answers the daily broker login, and can trigger Halt or Flatten.
_Avoid_: User, admin, trader

**Session Login**:
The daily interactive login that refreshes the broker access token. A missed Session Login puts the day in Data-Only Mode.

**Data-Only Mode**:
The state in which the system ingests and analyses but places no Orders because no valid broker session exists.

**Ledger**:
The append-only record of every Run, Thesis, Critique, Verdict, Pass, Block, Order, Fill, Approval, Halt, and configuration change.
_Avoid_: Log, history, audit trail (the Ledger is the audit trail)
