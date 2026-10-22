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
