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

**Listing Age**:
The trading days since an Instrument was first admitted to trading on NSE, measured from the earliest supported date established using NSE listing evidence and the Instrument's first Raw Series row. NSE's current master date may reflect a later admission to the main board rather than the original listing. Listing Age is distinct from the amount of historical data held, which may contain gaps.

**Candidate**:
An Instrument in the Universe that the Strategy's screen surfaced on a given Run.
_Avoid_: Signal, pick, idea, opportunity

**Strategy**:
A named, versioned rule set that turns market data into Candidates and Plans. A Strategy version is immutable once it has been run against the Holdout.
_Avoid_: Algo, system, model, bot

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
The complete trade specification for a Candidate: entry price, stop, target, size, and maximum holding period.
_Avoid_: Setup, trade idea, order plan

**Verdict**:
The final decision on a Candidate for a Run: BUY, SKIP, WAIT, or EXIT, with the reasons.
_Avoid_: Signal, decision, call, recommendation

**Run**:
One scheduled decision cycle over the whole Universe, producing Verdicts.
_Avoid_: Tick, cycle, iteration, scan

**Shadow Mode**:
The state in which the AI layer records what it would have done but has no effect on Verdicts.

### Risk and control

**Risk Engine**:
The code-only component that checks every Plan against the Limits and issues a Pass or a Block. Nothing reaches the Executor without a Pass.
_Avoid_: Risk manager, risk agent, guardrails

**Limits**:
The configured constraints the Risk Engine enforces: risk per trade, position count, position size, sector exposure, gross exposure, daily loss, and drawdown.
_Avoid_: Rules, guardrails, parameters

**Pass**:
The Risk Engine's signed approval of one specific Plan. A Pass is bound to that Plan and expires with the Run.

**Block**:
The Risk Engine's refusal of a Plan, with the Limit that was breached.
_Avoid_: Reject, fail, veto (veto is the Skeptic's word)

**Halt**:
The state in which no new entries are placed, pending orders are cancelled, and open Positions continue to be managed by their exits.
_Avoid_: Kill switch (the mechanism), stop, pause, freeze

**Flatten**:
The explicit, confirmed command to close every open Position at market. Distinct from Halt.

### Execution

**Order**:
An instruction sent to the Broker Adapter to buy or sell a quantity of an Instrument, under the Symbol it trades under that day, on stated conditions.

**Fill**:
A confirmed execution of all or part of an Order at a price.
_Avoid_: Execution, trade

**Position**:
The net holding in one Instrument, kept as one Position across a change of Symbol, with its cost basis, stop, target, and the Plan it came from.
_Avoid_: Holding, trade

**Broker Adapter**:
The component that translates Orders into a broker's API calls and broker events back into Fills. The Paper Broker and the Kite Broker are both Broker Adapters.

**Paper Broker**:
The internal Broker Adapter that simulates Fills against real quotes with modelled slippage and the full Indian cost stack.
_Avoid_: Simulator, sandbox, demo account

**Approval**:
The operator's explicit yes to one Order in live mode. Required for every live Order until the Autonomy Gate passes.

### Validation

**Gate**:
A measurable, configured rule that must pass before a Strategy or the AI layer advances a stage: Backtest Gate, Paper Gate, Live Gate, Autonomy Gate, AI Promotion Gate.
_Avoid_: Milestone, checkpoint, criteria

**Holdout**:
The most recent two years of data, locked and run exactly once per Strategy version.
_Avoid_: Test set, out-of-sample (ambiguous: walk-forward windows are also out of sample)

**Walk-Forward**:
The validation protocol in which parameters are chosen on a rolling window and evaluated on the window that follows it.

**Expectancy**:
Average profit per trade after all costs, in rupees or as a fraction of equity.

### Price data

**Raw Series**:
An Instrument's daily prices and volumes exactly as NSE's official end-of-day files state them, each day under the Symbol it was published under, never adjusted and never modified.
_Avoid_: Unadjusted prices, raw candles (the broker's candles are the broker's own adjusted view)

**Corporate Action**:
An event NSE lists for a Symbol, or states in a circular a reviewed row cites, that takes effect on an Ex-Date, and so for the Instrument that Symbol named that day: a split, bonus, rights issue, dividend, demerger, or meeting. Splits and bonuses change the Adjusted Series; rights issues and dividends are flagged on it; the rest are recorded only.
_Avoid_: CA, event, announcement (an announcement is a filing, which may or may not describe a Corporate Action)

**Cited Action**:
A split or bonus taken from a reviewed record citing NSE's circulars, for one NSE's lists do not have. It is superseded, and not applied, when NSE's list has a split or bonus for the same Instrument within seven days of its Ex-Date; if that listed action's Adjustment Factor differs, the difference is reported for review.
_Avoid_: Cited row, manual action, manual adjustment

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
A stated, checkable cause of a difference Kite Parity finds: a series move (NSE lists the Instrument in another series that day), a Special Session Kite has no candle for, a day outside every range Kite was asked for, Kite's additive adjustment, or Kite's coarser rounding. A difference without one is unexplained.
_Avoid_: Excuse, known issue, exception

**Large Move**:
A close-to-close change of more than 1.5 times, either way, in an Instrument's Adjusted Series across its listing series. Unless a split or bonus on that day explains it, it must be explained by a reviewed record with its reason and source, because an unexplained one may be a split or bonus the Adjusted Series lacks.
_Avoid_: Jump, spike, outlier

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
