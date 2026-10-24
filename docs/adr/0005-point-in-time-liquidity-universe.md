---
status: accepted
date: 2026-10-21
amended: 2026-10-24
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

## Amendment (2026-10-24, #72): how a reconstructed Universe is recorded

The deferred design above is settled as follows.

- **Rule Validity is declared, not inferred.** An append-only `universe_rule_validity` table holds (rule version, in force from, declared at), written by an Operator command. `recorded_at` stays what it is: when the version was first computed.
- **The mode is derived.** A month's Universe is a Recorded Universe when its rule version was in force and recorded before the month's first trading day; otherwise it is a Reconstructed Universe. Every one of the 120 stored months is reconstructed today, and says so.
- **Every month build is recorded with its inputs.** An append-only `universe_month_builds` row per (month, rule version) stores the Data Cutoff (the last end-of-day date used), the ids of the end-of-day ingest runs and the Instruments rebuild it read, and a SHA-256 of the end-of-day rows in its window and of the Instrument links. Run ids say which ingests a build read; the hash says what the rows contained, and does not rely on append-only enforcement never having been bypassed. Months stored before this table existed get their cutoff from `ingest_runs` history or are marked cutoff-inferred. Each month hashes only its own window, about six months of rows, in seconds.
- **A backtest names its rule version and verifies its inputs.** The Protocol's data snapshot lists the build ids of every month the backtest reads and their modes; before running, the engine recomputes the hashes and refuses to run if any differs, so Phase 1's byte-identical membership stays true after re-ingests.
- **An unversioned query stays empty** for a month with no valid version, as the first amendment says. Nothing substitutes today's Universe.

Built in #75. The Operator commands are `tradeline declare-rule-validity --in-force-from DATE [--rule-version V]` and `tradeline backfill-universe-builds`; `queries.universe_month_mode`, `queries.universe_month_builds` and `queries.universe_month_hash` are the reads. "Before the month began" is read as before the start of its first calendar day in India, never later than its first trading day, so this reading can only call a month reconstructed that a trading-day reading would call recorded, never the reverse. The Instrument links hash covers the Instrument intervals overlapping the window with their Instruments' keys and listing dates, which is what a rule ranking Instruments reads; a rule ranking Symbols reads listing dates from the equity master, which no hash covers. Hashing one month's window took 0.51 to 0.55 s (325,000 to 382,500 rows; three windows, in 2016, 2024 and 2026) on a throwaway Compose Postgres database holding 7.7 million synthetic end-of-day rows (2,500 symbols on every weekday from 2015-01 to 2026-10, more than the real table holds), measured on the laptop with nothing else running, 2026-10-24. The real ten-year data was not touched; #85's acceptance run backfills its 120 months and states that run's time.

Alternatives rejected: a hash alone (cannot say which ingest to re-run); run ids alone (trusts that the rows those runs wrote are the rows there now); marking every month reconstructed with no cutoff or provenance (the ADR's four distinctions would be documented, not stored).
