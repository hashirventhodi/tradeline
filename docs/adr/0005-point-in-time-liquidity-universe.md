---
status: accepted
date: 2026-10-21
amended: 2026-10-21
---
# The Universe is a point-in-time liquidity rule, not an index constituent list

Backtesting on today's NIFTY 200 is survivorship-biased: every member survived and grew. The Universe Rule is instead the top 200 NSE stocks by trailing six-month traded value, recomputed monthly from NSE's free official end-of-day files, which is reproducible at any historical date. The live Universe is sanity-checked against the current NIFTY 200 and large disagreements are flagged, but the index list is never the source of truth. Reconstructing historical index membership from NSE announcements was rejected as fiddly and gap-prone.

## Amendment (2026-10-21, #1 review): an unversioned query never sees a rule recorded later

`queries.universe_membership` called without a `rule_version` returns the membership stored under the latest Universe Rule version whose `universe_rule_versions.recorded_at` is on or before the As-Of Date. If no such version exists it returns an empty list. It never falls back to the current rule. `tests/test_universe.py` pins this: membership for August 2024 computed today is invisible to an unversioned query as of 1 August 2024.

What follows from this:

- **Every version recorded today is invisible to any past As-Of Date.** Every Universe computed today for a historical month is stored under a version recorded today, so an unversioned historical query returns nothing. That is deliberate: a rule chosen in 2026 was not knowable in 2024, and silently treating it as if it were is the lookahead this ADR exists to prevent.
- **A caller that wants a past Universe must name the rule version.** A versioned query reads the stored membership for that month and version, after the month-start As-Of check, and does not consult `recorded_at`. Nothing calls the query without a version today, so Phase 1 needs no further change.

### Deferred to Phase 2: historical Universe reconstruction

Backtests need the Universe for past months under a rule chosen later. Phase 2 designs this explicitly, keeping four things distinct that are currently conflated or implicit:

1. **When the version was computed** (`recorded_at` today; call it `computed_at` if renamed).
2. **The rule's validity period**: the dates from which a version is the rule in force, as opposed to when it was recorded.
3. **The data cutoff**: the last end-of-day date used to build a month's membership. This is already "strictly before the month's first trading day", but it is not stored per membership.
4. **The mode**: a *recorded* Universe (a version that was in force and recorded at the time) versus a *reconstructed* historical Universe (a later version applied to past months).

A reconstructed Universe must use only inputs available at the historical date (the data cutoff above, end-of-day files as first loaded, the instrument master as of then), and it must be reproducible byte for byte. Every backtest stores the rule version, the data provenance (end-of-day ingest runs and their files), and the mode it used. Where no valid version exists for a query, the result is an explicit empty result or a domain-level outcome, never a silent substitution of today's Universe.
