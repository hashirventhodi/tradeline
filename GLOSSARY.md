# Tradeline

A long-only swing trading platform for NSE cash equities. A deterministic quant core proposes and manages trades; an AI layer may only veto or shrink them; a code-only risk engine has the final word.

## Language

### Universe and selection

**Universe**:
The set of symbols eligible for trading on a given date, chosen by the Universe Rule.
_Avoid_: Watchlist, basket, index

**Universe Rule**:
The deterministic rule that defines the Universe at a point in time (currently: top 200 NSE stocks by trailing six-month traded value, recomputed monthly).

**Candidate**:
A symbol in the Universe that the Strategy's screen surfaced on a given Run.
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
An instruction sent to the Broker Adapter to buy or sell a quantity of a symbol under stated conditions.

**Fill**:
A confirmed execution of all or part of an Order at a price.
_Avoid_: Execution, trade

**Position**:
The net holding in one symbol, with its cost basis, stop, target, and the Plan it came from.
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
