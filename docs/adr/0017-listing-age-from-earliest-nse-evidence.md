---
status: accepted
date: 2026-10-21
---
# Listing Age counts from the earliest NSE evidence, not the equity master's date

The Universe Rule bars an Instrument until it has `min_listing_trading_days` of Listing Age. Until now that age counted from the Instrument's `listed_on`, which is NSE's equity-master "DATE OF LISTING". The #32 research found that this date is the security's *last* admission to dealings on NSE's main board, overwritten when its admission changes, not its first day of trading (NSE does not define the field; the reading rests on its circulars and seven archived masters). 197 of 4,449 Instruments have a master date after their first Raw Series row: 155 moved from SME to the main board, 41 changed their "permitted to trade" (PTT) status, and SUBEX was readmitted under a new ISIN after a capital reduction.

The rule also reads today's master for every past month, so any month before that date counted a Listing Age of zero. NESTLEIND, traded in EQ every day since 2015 and in the Nifty 50, was kept out of the Universe from 2016-11 to 2024-02 (87 months, its window traded value up to 12 times the 200th member's) because its master date is 2023-08-01, the day it moved from PTT to listed. CAMS was kept out from 2021-08 to 2021-11 the same way. #29 reported no month changed because it compared two rule versions that both read the same date.

The rule now has `listing_age_from = "earliest_evidence"`: Listing Age counts from the **earlier** of the earliest master date of any of the Instrument's (Symbol, ISIN) pairs (save the fallback-key case below) and its first Raw Series row in any listing series (EQ, BE, BZ, SM, ST). PTT rows and SME rows both count: the security was trading on NSE.

- A later master date never wins over a row, so a month reads nothing from after its own cutoff: the first row is before the month whenever the Instrument has a row in the window, and a master date earlier than every row is consistent with the rows.
- Listing Age is counted in trading days of the market's calendar held (every day with end-of-day rows, from 2015), so it never exceeds that calendar. A master date earlier than the Instrument's first row adds the calendar's days between the two, where the Instrument has no rows; before the calendar it adds nothing.
- Where an Instrument's master date already keys an earlier Instrument of the same Symbol (an unexplained ISIN change split the history, and NSE's master lists the original date under today's ISIN), that date is the earlier Instrument's evidence, not this one's. Such an Instrument is keyed from its first row and its Listing Age counts from that row.
- Today the only NSE listing evidence read is the equity master. Ingesting admission circulars or archived masters later would extend that evidence, not change the term.

## Consequences

- This is a new Universe Rule version (ADR 0005): the change alters membership, so it must alter the parameter hash. At its old value, `listing_date`, the parameter is left out of the hash, as `rank_by` is at `symbol`. Months stored under `ded30b1014c4` and the Symbol-ranked versions stay as they were. Their NESTLEIND and CAMS gap is a known error of those versions, not a difference to report. The new version is computed over the stored months as a deliberate recompute (ADR 0016), and `tradeline.toml` moves to it only once those months are stored, so the commands that read the configured rule (`collect`, `verify-universe`) never meet a version with no Universe.
- The Instrument key keeps its date (ADR 0016's amendment): the key is an identity and the date it uses is not Listing Age.
- The stale-month check of an Instrument rebuild compares eligibility by Listing Age, not by `listed_on`.
- The presence check (at least 90% of the 126-day window in EQ) still bars a new listing for about 114 trading days, so for SME migrants and PTT gaps Listing Age rarely binds. Its own job is the fresh listing between those days and `min_listing_trading_days`, and any future rule whose lookback is shorter than its listing minimum.

## Considered options

- **Drop Listing Age from the rule.** Presence already does nearly all of its work, and the rule would no longer read the master. Rejected: a shorter lookback would then admit fresh listings, and the two settings are kept independent.
- **Count from the first Raw Series row only.** Rejected: that is the history held, which this term is meant to stay distinct from, and it gives an Instrument listed long before its first row (rows lost, or a long suspension) no age for the calendar days in between.
- **Re-key the 197 by their earliest date.** Rejected: it changes keys on stored memberships and manual links, for a date that was never meant to be Listing Age.
- **Correct the stored months under the existing version.** Rejected: memberships are append-only per version, and ADR 0005 requires a version to reproduce.
