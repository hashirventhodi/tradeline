---
status: accepted
date: 2026-10-22
---
# The Integrity Check and Kite Parity replace reconcile, and a broker never gates our data

`reconcile` compared Kite's daily candles with NSE's end-of-day rows and failed the run at 0.1 percent of discrepancies. That one number did two jobs: it was the Phase 1 check that our data is sound, and it measured how far Kite's chart agrees with it. After #16 it stood at 4.50 percent, and almost none of that was an error in our data. It was Kite's own conventions: its EQ token carrying on through series moves (13,596 days), its additive adjustment and coarse rounding (about 29,670 closes), missing muhurat sessions (1,044), and candles never fetched (BAFNAPH, 998). A gate that a broker's conventions fail says nothing about our data. A gate loose enough to pass them would hide a real error.

So the two jobs are split, and the word "reconcile" is retired: Phase 6's reconciliation is broker positions against the Ledger, and that keeps the word.

- **The Integrity Check** tests NSE's data as we hold it against NSE alone, never against Kite. Every day in the Trading Calendar must have its end-of-day file. The calendar is a reviewed, checked-in list of the weekdays NSE was closed and the Special Sessions it held, each citing NSE's circular, because the files cannot tell a holiday from a lost day. Every Large Move of an ever-member must be explained, by a split or bonus on that day or by a reviewed, checked-in record giving its reason and an official source, because an unexplained one may be a split or bonus the Adjusted Series lacks. A day an Instrument has no row in a file held whole is NSE's own record (2,665 such days across 20 ever-members, all suspensions or distress), so it is reported and never fails the check.
- **Kite Parity** compares Kite's candles with the Raw Series as reconcile did, by presence and close with one Kite factor per stretch between Ex-Dates. Every difference gets a Parity Reason or is unexplained. It fails only when unexplained differences reach 0.1 percent of the rows compared. Every Parity Reason is a rule over data we hold, never a list of dates: a series move, a Special Session in the Trading Calendar, a day outside every range candle ingest recorded asking Kite for, an additive adjustment (one fit of a × NSE − b within half a tick on every bar of a stretch of at least five), or rounding to ₹1. A new convention of Kite's is unexplained until a rule for it is agreed here.

## Consequences

- Phase 1's acceptance line in `docs/phases.md` now names both checks.
- Candle ingest records each (Symbol, series, range) it requests, and one re-run of the ten-year fetch fills that record in for the bars already held.
- `compare-adjusted` is unchanged. It remains the bar-by-bar check of our adjustment arithmetic against Kite.
- A Large Move record that matches no Large Move is reported, as a manual link that joins nothing is (ADR 0016).

## Considered options

- **Keep one command and filter Kite's conventions out of its count.** Rejected: our data's soundness would still be measured against a broker's chart.
- **Kite as a witness inside the Integrity Check** (a candle on a day NSE has no row suggests a lost row). Rejected: those days are exactly what Kite Parity explains, and the Trading Calendar finds a lost file without a broker.
- **Parity as a report that never gates.** Rejected: a difference with no reason is how a new Kite convention, or a wrong factor of ours, first shows.
- **Gate on Instrument gaps.** Rejected: without a source for suspensions, every suspension would fail the check, and a gap in a file held whole is not lost data.
