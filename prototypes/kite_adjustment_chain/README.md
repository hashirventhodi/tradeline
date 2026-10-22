# PROTOTYPE: Kite's Adjustment as a chain (#62)

Throwaway. It lives on the `prototype/kite-adjustment-chain` branch only. Nothing here
writes to the database or calls Kite or NSE.

## The question

Does replaying Kite's adjustment one stage at a time reproduce Kite's daily closes
**exactly** from NSE's raw closes? Each stage rescales or subtracts from the history Kite
has already rounded, then rounds again.

## The answer

Yes, for every case #62 named except the listing-day closes, which were expected to stay
unexplained. Over the ten-year #59 run (1,008,709 rows), the strict replay reproduces
28,933 of the 29,670 closes Kite Parity flags. That leaves **745 unexplained, including
the 8 missing days: 0.0739 %**, against the gate of 0.1 %. No tolerance is involved
anywhere: a close equals the replay or it does not.

| Today's outcome | Reproduced exactly |
|---|---|
| unexplained | 20,476 / 21,061 |
| additive adjustment | 5,237 / 5,238 |
| coarse rounding | 3,220 / 3,371 |
| matched (within tolerance) | 959,141 / 963,954 |

### What a stage is

For Ex-Date E, Kite rewrites every close before E as `round_tick(close / r - b)`. Older
closes pass through every later stage, earliest first. The stages the data shows:

- **Split or bonus:** `r` is the action's own ratio, except that some ratios are rounded
  to 2 decimals: a 1:3 bonus divides by **1.33**, not 4/3. That happens in 6 stages
  (ASTRAL ×2, GAIL ×2, OIL, WIPRO).
- **Dividend, from October 2024:** `r = round(P / (P - D), 6)`, where P is NSE's close the
  day before E. The 6-decimal rounding matters: ITC's exact divisor leaves a close a paisa
  off, and the rounded one reproduces all 343.
- **Dividend, July 2023 to September 2024:** subtracted outright, `r = 1, b = D`. This is
  the whole of today's "additive adjustment" (TATASTEEL Rs 3.60, OFSS Rs 240, GNFC Rs 30,
  then Rs 16.50).
- **Dividends before July 2023 were never adjusted** (MAJESCO's 98.8 % payout aside).
  Since then Kite has adjusted roughly those of 2 % or more of P. The edge is not sharp:
  1.8–2 % ones were sometimes adjusted, and some 2–4.5 % ones were not.
- **Rights, demergers and other actions:** no rule computes `r`, so it is read from Kite's
  closes. This covers 49 standing stages (rights 29, demergers and other 28 in all).
- **Tick:** each stage has its own, from the ladder 1p, 5p, 10p, Rs 0.50, Rs 1, plus
  **Rs 5** (ABBOTINDIA's adjusted closes sit on it). A later stage can be finer than an
  earlier one (TATASTEEL: 5p, then 1p, then 1p).
- **Ties:** each stage has its own tie rule too. Of the 351 standing stages, 295 round
  half to even, 32 half down (TATASTEEL's 2022 split, GAIL's 2019 and HDFCBANK's 2019
  splits and bonuses) and 24 half up. On the 26 split and bonus stages where this shows,
  exact arithmetic with a per-stage tie rule leaves 3 of 26,577 closes off. Floating-point
  division with ties to even leaves 2,338 of 20,834 off, so floating point alone does not
  explain the ties.
- **A Kite Basis boundary** falls only where the chain stops reproducing the older
  stretch. Most Ex-Dates (all small dividends) carry no stage.

### The cases

| Case | Today's flagged closes reproduced | Stages |
|---|---|---|
| TATASTEEL, INOXWIND, TRIDENT | 1,427/1,427, 1/1, 1/1 | 3, 2, 3 |
| CANBK, IOC, GAIL (70–85 % before) | 1,656/1,656, 1,623/1,623, 1,293/1,293 | 4, 7, 7 |
| GNFC, HINDPETRO, BPCL | 1,744/1,744, 1,723/1,723, 1,271/1,271 | 4, 6, 6 |
| M&MFIN | 1,327/1,344 | 5 (two fitted: a 2020 rights issue and 2025-05-14) |
| OFSS, BSE (Rs 0.50) | 1,531/1,531, 716/717 | 3, 2 |
| ASHOKLEY, HEROMOTOCO, NIITTECH, NAUKRI, GODFRYPHLP, TATAINVEST, MCX | all | 1–2 each |
| Listing-day closes (DMART, SBILIFE, SHANKARA and others) | 0/49 | none: expected |

GNFC, HINDPETRO, M&MFIN and BPCL were neither coarse rounding nor two roundings. Each is a
chain of 4 to 6 stages that mixes subtraction-era and ratio-era dividends.

### What the chain cannot reproduce

- **NMDC before 2024-02-27** (516 closes): Kite's close wanders 0.2 to 2 % from any
  constant multiple of NSE's. Kite holds different closes there, which is not a
  different adjustment.
- **BAJFINANCE** (123 closes, which today's coarse-rounding rule explains): before
  mid-2025, Kite's closes fall on Rs 0.50 on some days and Rs 1 on others (893.5, 942.5,
  but 899 for 898.7). No single stage, tick or tie rule reproduces them. **These are the
  152 closes explained today that the replay would lose** (BAJFINANCE 123, NMDC 25,
  others 4).
- **Listing months** (49 closes): SHANKARA, DMART, SBILIFE and the others, as expected.
- **M&MFIN** (16), **SSLT** (6), and single closes of HINDZINC, PTC and MAJESCO.

### Strict against loose

Strict mode (`strict`) never fits `r` at an Ex-Date a rule computes, and never falls back
to a free line. Loose mode reaches 685 (0.0679 %), but needs 10 free `f x - b` stages
(COALINDIA ×5, KARURVYSYA ×2, BEL, SSLT ×2) and near-identity fitted stages that nudge
closes by a paisa. That is a fit, not a model, so the numbers above are strict.

### Fitted stages in mixed gaps (`RULES=final_strict`)

A fitted stage covers a **gap**: every Ex-Date between the older stretch and the newer one it
joins. When several Ex-Dates fall in one gap (the stretches between them do not stand), the
gap is **mixed** if it holds both a fittable action (rights, demerger, scheme, AGM, buyback, ...)
and a computed-kind one (split, bonus, consolidation, or a dividend of any era). Under
`RULES=final` a mixed gap gets one stage, and a fitted one can fold a computed action into its r.

Of `final`'s 56 fitted stages, 49 are clean (a gap of their own Ex-Date alone: every rights and
demerger stage) and 7 sit in mixed gaps, all at an AGM or buyback (or with one in the gap):

| Instrument | Fitted at | r (final) | Gap's computed-kind actions | Class |
|---|---|---|---|---|
| IIFLWAM | 2022-09-22 AGM | 4.000221 | 1:1 bonus + 2-for-1 split 2023-03-02, 2 dividends | absorbing: the split, r = 4 (off 5.5e-05) |
| MAJESCO | 2020-11-12 buyback | 84.597234 | Rs 974 dividend 2020-12-23 (pre-July-2023) | absorbing: P/(P-D) = 84.605150 (off 9.4e-05) |
| MOTHERSUMI | 2022-01-14 demerger | 1.676522 | 1:2 bonus 2018-10-30, 5 dividends, an AGM | absorbing + residual: 1.5 x 1.117681 |
| COALINDIA | 2020-09-14 AGM | 1.008696 (116/115, off the 6-decimal grid) | 10 dividends 2020-11..2023-11 | separate: nearest computed value 1.013834, 0.5 % away |
| COALINDIA | 2019-02-14 buyback | 1.000023 | dividend 2019-03-22 | near-identity |
| COALINDIA | 2018-09-04 AGM | 0.999983 | dividend 2018-12-28 | near-identity |
| COALINDIA | 2017-08-31 AGM | 0.999949 | dividend 2018-03-16 | near-identity |

No join crosses a split: within every Kite Basis, the Ex-Dates between member stretches hold only
dividends and fittable actions, so a basis's one r cannot fold in a split that way.

`RULES=final_strict` is `final` except in a mixed gap, where a fitted stage may not absorb,
replace or distort a computed action:

- **(a)** each computed-kind Ex-Date gets its own computed stage, at its own Ex-Date, with its
  computed value (every form `final` offers), or no stage where the rules allow none: any
  dividend may carry none, and one before 2023-07-01 always does. A split, bonus or
  consolidation always has its stage.
- **(b)** a fitted stage sits at a fittable action's own Ex-Date, in Ex-Date order among the
  gap's computed stages (earlier ones inside it, later ones outside). Its r is fitted with them
  in place, so it carries only what they do not. Every fittable Ex-Date is tried; equal results
  go to the latest, as in `final`.
- **(c)** a fitted r within 0.1 % of a value a computed-kind action of the gap without its own
  stage would give (split ratio, 2-decimal ratio, P/(P-D) of a dividend of any era, or a product
  of these) is that action absorbed, and is refused. Closes that need it stay unexplained.
- **(d)** in any gap, mixed or not, a fitted r within 0.1 % of 1 is refused. It adjusts nothing,
  so it is a reround, and a reround stands only at a dividend Ex-Date from 2023-07-01, as a
  computed stage.

Interpretation choices: a mixed gap's stages are chosen together, fewest closes off, then
without a fitted stage, then fewer stages, then the coarsest ticks. The computed stages share a
tick, the fitted stage has its own, and all share a tie rule. Each join re-chooses those ticks,
the tie rule and the fitted r over the whole basis, keeping the layout. Gaps with more than 64
layouts (they reach back over many non-standing stretches, as NMDC's do) are searched by
improving one Ex-Date's choice at a time. `RULES=final_nofit_in_mixed_gaps` bounds this: no fitted stage
at all in a mixed gap.

What `final_strict` changes, against `final`:

| Instrument | final | final_strict |
|---|---|---|
| IIFLWAM | fitted 4.000221 at the AGM | split 4.0 at 2023-03-02 (own stage); the AGM's fitted r = 1.000000 (a reround) refused by (d) |
| MOTHERSUMI | fitted 1.676522 at the demerger | bonus 1.5 at 2018-10-30 (own stage), plus a fitted r = 1.117684 at the demerger |
| MAJESCO | fitted 84.597234 at the buyback | refused (absorbs the Rs 974 dividend); 7 more stretches (1,004 bars) do not stand |
| COALINDIA | fitted 1.008696 at the 2020-09-14 AGM | fitted 1.008698: no computed value in r (nearest 1.013834) |
| COALINDIA ×3 | fitted 0.999949, 0.999983, 1.000023 at 2017–2019 AGMs and a buyback | refused by (d) |
| TATACOMM | fitted 1.0005 at 2019-09-17, a clean gap | refused by (d) |

The gap's dividends keep no stage in both. None of these closes is flagged by Kite Parity today,
apart from one MAJESCO close that was already unexplained. The unexplained count does not move.

| Run (same 1,008,709-row extract) | Unexplained | % | Gate 0.1 % | Fitted stages | Explained closes via a fitted stage | Non-standing stretches |
|---|---|---|---|---|---|---|
| baseline (no `RULES`, `report.py` count) | 745 | 0.0739 % | pass | 56 | 2,847 | 94 |
| baseline (`score.py` count) | 842 | 0.0835 % | pass | 56 | 2,847 | 94 |
| `final` | 841 | 0.0834 % | pass | 56 | 2,848 | 93 |
| `final_strict` | 841 | 0.0834 % | pass | 50 | 2,848 | 112 |
| `final_nofit_in_mixed_gaps` (bound) | 841 | 0.0834 % | pass | 49 | 2,848 | 116 |

`score.py` counts a close as explained only in a Kite Basis of 20 bars or more, so it counts the
baseline 97 higher than `report.py`. "Fitted stages" counts every fitted stage entering a chain,
including those in bases under 20 bars (the baseline has 7 of those). Of 6,468 stretches in all.

### Parked questions, with what the prototype saw

- **Tick per stage:** yes, and the tie rule per stage too.
- **Sample size:** a tick read off 11 bars was wrong for BAJFINANCE (Rs 0.50 against Rs 1:
  1 close off against 2). The prototype re-chooses a stage's tick and tie rule over its
  whole Kite Basis whenever a new stretch would not join.
- **Closes off before a basis stops standing:** the prototype allows ceil(5 % of each
  stretch's bars). 121 of 6,468 stretches do not stand.
- **How a stage's factor is found:** computed from the action for splits, bonuses and
  dividends, and read from Kite's closes only for rights, demergers and other actions.

## Files

- `chain.py`: the logic, pure and exact (`Fraction`). A stage, `replay`, `preimage`, and
  `stage_window` (a new inner stage's window under the chain so far).
- `extract_all.py`: the read-only dump of every stretch, reusing `parity`'s own pipeline.
- `replay_run.py`: the replay, working back from today.
- `report.py`: the summary and the #62 cases.
- `score.py`: the count under the settled rules (`RULES=final`, `final_strict`, ...): only
  standing stretches in a Kite Basis of 20 bars or more explain a close. It prints unexplained
  closes by cause and Instrument, pass or fail against the 0.1 % gate, the fitted-stage count,
  the explained closes that depend on a fitted stage, and the non-standing stretches. It also
  classifies every fitted stage's gap (clean, or conflicting: absorbing, absorbing + residual,
  near-identity, separate). Given a second result, it shows how each one handled every
  computed-kind action in a mixed gap. Its inputs and outputs go in `out/`, which is
  git-ignored (they embed Kite closes).
- `build_demo.py` and `demo_template.html`: build `kite-adjustment-chain.html`, a single
  file to open in a browser. Its walkthroughs cover ITC, TATASTEEL, GAIL, IOC, HDFCBANK,
  OFSS, NMDC and SHANKARA, with a free-play stage editor. The built page embeds real
  Kite closes, so it is git-ignored. Build it locally:

```sh
uv run python prototypes/kite_adjustment_chain/extract_all.py /tmp/all.pkl   # ~40 s, read-only
uv run --with scipy python prototypes/kite_adjustment_chain/replay_run.py /tmp/all.pkl /tmp/v.pkl 0.05 strict
uv run --with scipy python prototypes/kite_adjustment_chain/report.py /tmp/v.pkl
uv run python prototypes/kite_adjustment_chain/build_demo.py /tmp/all.pkl prototypes/kite_adjustment_chain/kite-adjustment-chain.html
```

The rule-set runs and their scores (each replay needs `--with scipy`; `final_strict` takes about
6 minutes on 10 cores, the others 0.5 to 1.5):

```sh
P=prototypes/kite_adjustment_chain; O=$P/out
uv run python $P/extract_all.py $O/all.pkl                     # read-only; 1,008,709 rows
uv run --with scipy python $P/replay_run.py $O/all.pkl $O/baseline.pkl 0.05 strict
for v in final final_strict final_nofit_in_mixed_gaps; do
  RULES=$v uv run --with scipy python $P/replay_run.py $O/all.pkl $O/$v.pkl 0.05 strict
done
uv run --with scipy python $P/report.py $O/baseline.pkl         # 745
uv run python $P/score.py $O/final.pkl $O/baseline.pkl           # 841
uv run python $P/score.py $O/final_strict.pkl $O/final.pkl       # 841, and the handling diff
uv run python $P/score.py $O/final_nofit_in_mixed_gaps.pkl $O/final.pkl
```
