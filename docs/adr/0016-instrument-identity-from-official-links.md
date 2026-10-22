---
status: accepted
date: 2026-10-21
amended: 2026-10-22
---
# An Instrument is joined only by official, dated links, and splits on doubt

Every table keys a stock by its Symbol of the day, so a rename cut its history in two. The #13 acceptance run showed what that costs. Kite filed 44,408 pre-rename candles under the new Symbol, and renamed large caps dropped out of the Universe for five to twelve months, because the Universe Rule saw the new Symbol as a fresh listing (ZOMATO to ETERNAL: May to September 2025 missing). We now follow an **Instrument** across its Symbols, series, and ISINs. Stored rows keep the Symbol they were published under, and the Instrument is a mapping over them, never a rewrite.

Neither Symbol nor ISIN is a stable identity on its own. Renames keep the ISIN (LTI, LTIM, and LTM are INE214T01019 throughout). Face-value splits issue a new one (HDFCBANK INE040A01026 to INE040A01034). Since 2016, 438 symbols carry more than one ISIN and 332 ISINs carry more than one symbol. So two stretches of history are joined only by an **Instrument Link** with a date and a source:

- same Symbol and same ISIN;
- same Symbol with a new ISIN on the Ex-Date of a split, bonus, or consolidation NSE lists;
- an NSE symbol-change record (stored as published, then links derived from it);
- a reviewed, checked-in manual link that cites an official source, for renames NSE's list omits (RUCHI to PATANJALI), or for an ISIN change under one Symbol that no Corporate Action explains (YESBANK in March 2020, when the depositories moved every share to a new ISIN to apply the Reconstruction Scheme's lock-in). Such a link is held to what a split is: it is dated on the new ISIN's first day, and the old ISIN's last row is within seven days before it, so it can never bridge a suspension or a relisting years later. A row that joins nothing is reported, never ignored. A longer gap would need a reviewed change to this rule, not a broader row.

Anything else splits the history and is reported as an anomaly: an unexplained ISIN change, a same-ISIN rename with no record, or a link that would give one Instrument two live Symbols on a day. Mergers never join. A wrongly split history is visible and can be fixed with a cited link. A wrong join silently mixes two companies' prices into the Adjusted Series and the Universe.

## Consequences

- The Universe Rule ranks Instruments, summing traded value and counting listing age across their Symbols. That is a new rule version (ADR 0005), and memberships under the old version stay as they were.
- An Instrument is keyed by its first Symbol and NSE's listing date (or its first Raw Series row where NSE gives none), so rebuilding the links reproduces the keys.
- A Symbol in a query resolves to the Instrument it named on the As-Of Date.
- A link that arrives late and would change months already in the Universe is reported as stale. A recompute is a deliberate act, never automatic.

## Considered options

- **Join on ISIN.** Rejected: it breaks every split that issues a new ISIN, and ISINs have carried more than one Symbol.
- **Join on Symbol continuity alone.** Rejected: a reused Symbol would join two companies.
- **Infer renames from an unchanged ISIN.** Rejected as a link. Such cases are offered as candidates for a cited manual link instead.

## Amendment (2026-10-21, #32): the key's date is not Listing Age

NSE's equity-master listing date is the security's last admission to dealings on the main board, and can come years after its first trading day on NSE (NESTLEIND's is 2023-08-01, the day it moved from "permitted to trade" to listed). The key keeps that date: it is an identity, reproduced by every rebuild, and re-keying would change stored memberships and manual links. Listing Age no longer counts from it (ADR 0017).

## Amendment (2026-10-22, #37): a circular-cited split or bonus explains an ISIN change

A split or bonus supplied by a cited NSE circular (ADR 0006, amended by #37) counts as one NSE lists: a new ISIN under the same Symbol on its Ex-Date is joined by the same rule. The same-Symbol manual links it makes redundant are removed, so one cited row both joins and adjusts. A manual link stays for an ISIN change no split or bonus explains (YESBANK, HDFCLIQUID), or one whose action does not qualify (TIDEWATER).
