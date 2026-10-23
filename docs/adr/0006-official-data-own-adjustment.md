---
status: accepted
date: 2026-10-21
amended: 2026-10-23
---
# Price history comes from Kite and NSE only, with our own corporate-action adjustment

Kite Connect candles are stored exactly as returned. Splits and bonuses are pulled from NSE's official lists and the Adjusted Series is computed by us, deterministically and auditably. Yahoo Finance's adjusted .NS series was rejected despite being faster to start with: it is unofficial, its adjustment conventions differ from the broker's, and two sources with different conventions make backtest and paper disagree in ways that are hard to trace. Paid vendor history was rejected on budget.

## Amendment (2026-10-21, #5): which series is raw, and which the backtester reads

The original text called Kite's candles "raw and unadjusted". They are not: Kite's daily history is adjusted by Kite's own rules as of the day it is fetched (splits and bonuses in price and volume, and at least one large dividend in price alone; see `docs/research.md`). So:

- **The Raw Series is NSE's end-of-day file rows** (`eod_bhavcopy`): official, unadjusted, inserted once, never modified. They are the only input to the Adjusted Series.
- **Kite's candles** (`candles_daily`) stay stored exactly as returned, as the record of what the broker showed on the day they were fetched. They are the cross-check for our arithmetic (the RELIANCE bonus comparison is a test), and the source of minute bars later, never the input to adjustment.
- **The backtester, the engine, and the tests read the Adjusted Series through `queries.adjusted_daily_candles`** with an As-Of Date, and nothing else. It is computed on every read from the Raw Series and the Adjustment Factors in `corporate_actions`, is never written back, and applies only Corporate Actions whose Ex-Date is on or before the As-Of Date. Bars after the As-Of Date are refused.
- Splits (either direction) and bonuses are adjusted. Rights issues and dividends are recorded and flagged, not adjusted. Demergers and other schemes are recorded only (follow-up issue #15).
- Adjusted prices are rounded to the 5 paisa tick with ties to the even tick, the rule that reproduces Kite's bars exactly above Rs 250. Below Rs 250, where NSE's tick has been 1 paisa since June 2024, the broker's rounding is unverified and the same rule is applied until evidence says otherwise.

Alternatives rejected: adjusting Kite's bars a second time (double-counts every action Kite applied); storing the adjusted series (an audit cannot then tell raw from derived, and a new action would require rewriting history).

## Amendment (2026-10-22, #37): a split or bonus NSE's lists omit may come from a cited NSE circular

Some splits and bonuses are in none of NSE's Corporate Action lists, though NSE announced each by circular: KARDA's 2020 split, JSLL's and GICL's 2025 SME splits, three ICICI ETF unit splits, and KOTAKGOLD's 2015 split, which the fund-unit list files under today's Symbol GOLD1, a Symbol that did not name the Instrument then. Joined by a manual link alone (ADR 0016), each left an unadjusted 4x to 10x fall in its Adjusted Series. So:

- **A reviewed, checked-in row citing NSE circulars may supply a split or bonus** that no NSE list has. The circulars must state both the ratio and the Ex-Date. An Ex-Date inferred from prices does not qualify (TIDEWATER's 2016 bonus, whose Ex-Date no circular read states, stays unadjusted and is flagged with #15's kinds). Only the exchange's own circulars count: a company filing gives the record date, not the Ex-Date.
- **The row states NSE's subject wording, never a factor.** The factor is derived from it by the same reading as a listed action, and the row is stored in `corporate_actions` with its source and citation, append-only like the rest.
- **It must agree with the price.** The Ex-Date close over the previous close, read across the Instrument's series, must be within 20 percent of the row's factor (the factor that multiplies earlier prices, below one for a split or bonus): |(Ex-Date close / previous close) / factor - 1| <= 0.2. A row outside that, or without both closes, is refused. KARDA's split from Rs 10 to Rs 2 has factor 1/5, and its close went from 275.65 to 57.90, a ratio of 0.210: 5 percent from 0.2, so it is accepted. A 1:1 bonus (factor 1/2) on the same prices would be 58 percent off, and refused. A wrong ratio or Ex-Date is out by a factor of two or more; a price band or an odd print (TIDEWATER fell 4.12-fold against a factor of 4, 3 percent off) is not. Rows of one Symbol on one Ex-Date (a split and a bonus) are checked by their factors together.
- **NSE's list wins.** If an NSE list has an adjusting action for the same Instrument with an Ex-Date within seven days of the row's, the row is not applied and is reported as superseded, so a split NSE files as two actions and a row files as one is never counted twice. A different combined factor on that date is reported as an anomaly. Every Instruments rebuild reports both over all stored rows (#45), since NSE may list the action later, or a rebuild may change which Instrument a Cited Action reaches.

Alternatives rejected: leaving the series discontinuous and flagged (the official source exists and the arithmetic is the same); widening the Symbol window so KOTAKGOLD reaches GOLD1's record (a reused Symbol would then attach another security's action); a factor column on the manual links (it mixes identity with adjustment, and a bonus with no ISIN change has no link).

## Amendment (2026-10-23, #15): demergers and the other price-moving actions are flagged, and no dividend is adjusted

The #5 amendment left demergers, schemes and capital reductions recorded only, under the kind `other`. Each moves the price without being a split or bonus: RELIANCE opened lower on 2023-07-20 by the value of Jio Financial, and 26 of the 43 Large Moves #58 reviewed are demergers or schemes. A Strategy reading across one sees a fall no holder suffered. So:

- **Demergers, schemes and capital reductions are Flagged Actions**, like rights issues and dividends. Each has its own kind (`demerger`, `scheme`, `capital_reduction`). They are carried on every bar before their Ex-Date and change no price. `scheme` covers schemes of arrangement, amalgamation and demerger, amalgamations, mergers and reconstructions. Buybacks get their own kind (`buyback`) and are recorded only, since a tender buyback does not reprice the stock on its Ex-Date. Meetings and the rest stay `other`.
- **A subject naming several actions takes the kind that matters most**: split, bonus, demerger, scheme, capital reduction, rights, dividend, buyback, other. A factor is never lost, and a subject naming a flagged action is flagged unless it also names a split or bonus, which it is then adjusted for (no stored subject names both).
- **Demergers get no Adjustment Factor.** For cash equities the only factor available is the Ex-Date open over the previous close, which is derived from prices, not stated by NSE. NSE's F&O adjustment factors are official but exist only for F&O stocks, which would give one kind two behaviours.
- **No dividend is adjusted.** The hypothesis that Kite adjusts dividends of 2 percent or more of the price (two data points, #5) does not hold up across ten years (docs/research.md, #62): Kite adjusted no dividend with an Ex-Date before 2023-07-01, and after that adjusted roughly those of 2 percent or more, but some of 1.8 to 2 percent and not some of 2 to 4.5 percent. No threshold of ours reproduces that, and Kite Parity already replays Kite's dividend stages on its own (ADR 0018, amended by #62).
- **Rounding stays at 5 paisa with ties to even, below Rs 250 too, unverified there.** Its first reason was to reproduce Kite's bars. ADR 0018 has since moved agreement with Kite's chart to Kite Parity, and #62 found Kite rounds stage by stage to ticks from 1 paisa to Rs 5, so no single rule of ours would reproduce it. The rule is kept because it is the one the Adjusted Series has used, not because it matches the broker below Rs 250.
- **Stored rows take their new kinds by a migration that reads their subjects again**, as 0019 did for the "Fv Splt" subjects. The subjects, sources and citations are unchanged. The same reading fixes 37 dividends whose subjects NSE misspelled ("Divdend", "Divided", "Dividned", "Divivend", and "dividend" run into the word before it) and which were stored as `other`.

The #37 amendment says TIDEWATER's 2016 bonus "is flagged with #15's kinds". It is not: a flag comes only from an action in NSE's lists, and NSE lists none for it. TIDEWATER has never been in the Universe. If it becomes an ever-member, the Integrity Check fails on its 4.12-fold Large Move and calls for a reviewed record then.

ADR 0018 is unchanged: a Flagged Action on the day of a Large Move does not explain it. A flag says the price moved, not by how much, and a demerger or capital reduction can come with a consolidation the Adjusted Series lacks.

Alternatives rejected: a price-derived factor for demergers (not official data); NSE's F&O factor where one exists (covers F&O stocks only); adjusting dividends of 2 percent or more (matches neither Kite nor any official convention); one kind for every scheme-like action (loses the distinction NSE's subject makes); flagging buybacks (about 340 actions that do not reprice the stock).
