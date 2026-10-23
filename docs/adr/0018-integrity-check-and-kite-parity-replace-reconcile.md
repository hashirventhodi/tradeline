---
status: accepted
date: 2026-10-22
amended: 2026-10-23
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

## Amendment (2026-10-23, #62): Kite's Adjustment replaces the additive and rupee-rounding reasons

The #59 run failed Kite Parity at 2.09 percent, almost all of it closes the additive reason could not pass. The fit was not the problem: a minimax line does barely better than least squares. Kite adjusts its history one stage at a time. At each action it adjusts for, it divides the history it has already rounded, or subtracts from it, and then rounds again, each stage to its own tick. One line rounded once cannot describe that (docs/research.md, #62).

So the additive adjustment and rounding to ₹1 are retired as Parity Reasons. Both are replaced by one, **Kite's Adjustment**. A close mismatch is explained when replaying Kite's stages from NSE's raw close gives exactly Kite's close. No tolerance is widened, and nothing is fitted to a line.

- **Unchanged.** A close is a mismatch exactly as before: further than `close_tolerance` (plus half a 5 paisa tick) from NSE's close times the stretch's median factor. Kite's Adjustment only gives reasons to those mismatches; a matched close is never replayed or re-judged. `compare-adjusted` is unchanged, and so is the 0.1 percent gate.
- **Stages.** One stage can fall on each Ex-Date. It is applied to every older close, earliest stage first. A stage's ratio or amount is **computed** from the action wherever a rule gives it:
  - a split, bonus or consolidation divides by its exact ratio, or by that ratio rounded to two decimals (Kite divides a 1:3 bonus by 1.33);
  - a dividend with an Ex-Date before 2023-07-01 is never a stage;
  - one from 2023-07-01 to 2024-09-30 is subtracted, or divides by P / (P − D) rounded to six decimals (P being NSE's close the day before), whichever reproduces Kite's closes, since Kite used both then;
  - one from 2024-10-01 divides by that ratio only.
  
  An action is a dividend when its stored kind is: a subject naming a dividend and a rights issue is a rights issue, by ADR 0006's precedence, and its stage is fitted (#66). From 2023-07-01, a dividend's Ex-Date may also carry a stage that only rounds the history again, to a new tick. Whether a given dividend is a stage is read from Kite's closes; no threshold is agreed.
- **Fitted stages.** For any other kind of action (a rights issue, a demerger, and the rest), no rule gives Kite's ratio. It is read from Kite's closes and reported as **fitted**. A fitted ratio is chosen so that the closes it explains are reproduced. Reproducing them is therefore not independent evidence that the stage is right, only that one number can account for them. The report keeps fitted stages, and the closes that depend on them, apart from computed ones. A fitted stage may never stand in for a computed one:
  - It sits at its own action's Ex-Date. Where several Ex-Dates fall between two stretches, every split, bonus, consolidation and dividend among them has its own computed stage, or none where the rules give none, and the fitted ratio carries only what those leave.
  - A fitted ratio within 0.1 percent of what one of those actions would give, or a product of them, absorbs that action and is refused. The ratio a dividend before 2023-07-01 would give counts too, since such a dividend is never a stage.
  - A fitted ratio within 0.1 percent of 1 adjusts nothing and is refused, wherever it falls: a stage that only rounds again stands only at a dividend's Ex-Date.
- **Tick and ties.** The stages found together for one gap form a group, and the group rounds as one. Its computed stages share one tick from the ladder 1 paisa, 5 paisa, 10 paisa, ₹0.50, ₹1, ₹5, its fitted stage has a tick of its own from that ladder, and one tie rule (to even, up or down) serves them all. A stage alone in its gap therefore has its own tick and tie rule. Both are chosen over the whole Kite Basis, never one stretch. A tick and tie rule for each stage of a group would multiply the search by 18 for every stage after the first; docs/research.md (#63) gives the evidence this rests on. A new tick or tie rule is a new convention of Kite's, unexplained until agreed here.
- **Kite Basis.** Consecutive stretches join, in date order, while the same stages reproduce them. A boundary falls only on an Ex-Date where Kite's closes force one. A Kite Basis of fewer than 20 compared bars explains nothing.
- **Closes off the replay.** A stretch stands when at most 5 percent of its compared closes (rounded up) differ from the replay. Its closes off the replay stay unexplained and are listed. A standing stretch is not one Kite's Adjustment reproduces in full. A stretch that does not stand explains none of its closes and adds no stage.
- **Report.** Each Kite Basis is reported with its span and its stages. For each stage: computed or fitted, ratio or amount, tick, and tie rule. Then the closes it leaves unexplained.

The 0.1 percent gate is a property of each run, never of this rule. On the #59 database, the prototype measured 841 unexplained of 1,008,709 rows compared (0.0834 percent) under exactly these rules. It allowed 50 fitted stages, and 2,848 of the closes it explains depend on at least one of them. Those closes alone are 0.28 percent of the rows, so that run passes only because fitted stages are allowed. The dataset, code revision and command are in docs/research.md (#62). The implementation's own acceptance run is the result that counts.

### Consequences of the amendment

- Closes today's rules explain and the replay does not become unexplained: BAJFINANCE's history, which lands on ₹0.50 some days and ₹1 on others, and NMDC's before 2024-02-27, where Kite holds different closes. Closes in an Instrument's first weeks after listing (DMART, SBILIFE, SHANKARA and others) stay unexplained.
- `additive_min_bars` gives way to the 20-bar minimum and the 5 percent limit.
- A gap with more than 64 layouts (each split, bonus, consolidation and dividend with its stage or none, and the fitted stage) is searched one Ex-Date's choice at a time rather than in full. Every layout it tries keeps these rules, but it can miss the one that would let the stretch stand. Each such search is reported with whether its stretch stood, including the search at one Ex-Date for an ambiguous fit.

### Considered options (#62)

- **Widen the half-tick limit** (one 10 paisa tick, or a bound derived from the number of roundings). Rejected: a wider limit passes closes without saying why, and #59 showed one 10 paisa tick still fails.
- **A minimax line within half the tick Kite's closes show.** Rejected: it explains about 2,500 of 21,061 closes. The rest were rounded more than once.
- **Kite's dividend conventions as strict eras** (subtraction only until 2024-09-30). Rejected: ONGC, PETRONET, BPCL and SSLT were divided by the ratio in that window, and forcing subtraction fails parity at 0.36 percent.
- **Free lines where the replay misses** (the prototype's loose mode). Rejected: it improves the count only by fitting, which is not a model of Kite.
- **Keep per-close rupee rounding as a fallback** for BAJFINANCE. Rejected: it explained those closes one at a time, by coincidence against a median factor, not by anything Kite did.
- **No fitted stage between two stretches that also holds a split, bonus, consolidation or dividend.** Rejected: it drops fitted stages for rights issues and demergers that conflict with nothing. MOTHERSUMI's demerger is one: it stands with its bonus as a separate computed stage. Measured, it changes no flagged close.
- **Fitted ratios only for rights and demergers.** Rejected: no rule computes Kite's ratio for any action outside splits, bonuses, consolidations and dividends, so all of them are treated alike and reported as fitted.

## Amendment (2026-10-23, #68): `compare-adjusted` judges our factors by Kite's Adjustment

This ADR twice says `compare-adjusted` is unchanged, the bar-by-bar check of our adjustment arithmetic against Kite. Phase 1 gated it at "to the paisa" on 20 symbols, and #13 failed it at 26,310 of 48,699 bars. That line cannot pass by design: ADR 0006 adjusts splits and bonuses only, Kite also adjusts dividends from 2023-07-01 and demergers, and Kite rounds each stage to its own tick. Agreement with Kite's closes is this ADR's Kite Parity. What `compare-adjusted` can still check, and nothing else does, is our factor at each split and bonus. An Integrity Check Large Move catches only a factor wrong by more than 1.5 times, so a 1:2 bonus read as 1:1 would pass it. So:

- **`compare-adjusted` gives Factor Agreement as its verdict.** It covers every split (either direction) and bonus Ex-Date of every ever-member, following Instruments across renames, and judges each by the replay Kite Parity uses. No tolerance is added.
  - **Agrees:** Kite's Adjustment stands with that Ex-Date's computed stage, our ratio exact or rounded to two decimals.
  - **Disagrees:** Kite's Adjustment stands across that Ex-Date only without that stage: no stage, or a fitted ratio in its place.
  - **Not judged:** no standing stretch to judge it by (BAJFINANCE before mid-2025, NMDC before 2024-02-27, listing months, a Kite Basis under 20 bars), no Kite closes on one side, an Ex-Date after the candles were fetched, our ratio standing only when read from Kite's closes on a tick of its own, or a split or bonus with no factor. Each is listed with its reason and counted.
- **Only a disagreement fails it.** An Ex-Date not judged is Kite's convention we cannot replay, not evidence against our factor, just as a gap is reported and never fails the Integrity Check.
- **An ever-member no Instrument answers for is listed, not judged, and fails nothing here.** The Integrity Check fails on it, and both checks are Phase 1 acceptance lines, so that gate stays the Integrity Check's alone rather than being duplicated. Not judged is never reported as verified.
- **The bar-by-bar comparison stays as detail** for a named Symbol, and gates nothing.

### Consequences of the amendment

- Phase 1's adjusted-chart line in `docs/phases.md` is replaced by Factor Agreement, proposed until the pull request that builds it is approved.
- Our rounding, 5 paisa with ties to even, is no longer checked against Kite by anything. ADR 0006 already keeps it by convention.
- A computed stage's ratio is read from the same stored action as our factor. Agreement shows that Kite's closes are reproduced with that ratio, so a misread subject shows as a disagreement or as a stretch that does not stand. It does not show that NSE's subject is right.

### Considered options (#68)

- **Keep the line and record it failed and explained.** Rejected: a gate that fails by design checks nothing, and our factors would go unchecked.
- **Retire it, relying on the Integrity Check and Kite Parity.** Rejected: a factor wrong by less than 1.5 times leaves no Large Move, and Kite Parity compares Kite with the Raw Series, never with our Adjusted Series.
- **Pass when every bar that differs from Kite is explained by Kite's Adjustment.** Rejected: it rebuilds Kite Parity's replay on top of our series to check, mostly, the actions we deliberately do not adjust.
- **Kite's factor as the steady ratio of Kite's close to NSE's either side of the Ex-Date** (#13's method). Rejected: it needs a tolerance, and a dividend or other stage near the Ex-Date moves it.
- **The 20 named symbols.** Rejected: a hand-picked sample, and all ever-members cost no more.
- **Fail on Ex-Dates not judged, or above a share of them.** Rejected: BAJFINANCE's 2016 split would fail it for Kite's rounding, which says nothing about our factor.
