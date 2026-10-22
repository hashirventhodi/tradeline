"""PROTOTYPE (#62): replay Kite's Adjustment stage by stage over every stretch extract_all.py
dumped, and count the closes it reproduces exactly. Throwaway.

A stage divides the history before its Ex-Date by a divisor r and rounds to a tick,
half to even (chain.py). Its r is, in order of preference:
  - computed: a split or bonus's own ratio; a dividend's round(P / (P - D), 6), P being
    NSE's close on the last day before the Ex-Date (ITC: the only form that reproduces
    all 343 closes of its two 2026 dividend stages);
  - fitted: r on the 6-decimal grid that reproduces the most closes (demergers, and
    anything the computed r misses);
  - a free line f x raw - b: the last resort, flagged.

Works back from today, per Instrument and series. The newest stretch is first tried as
Kite's raw close. Each older stretch joins the current Kite Basis when the chain so far
reproduces it (a fitted stage is refitted over the joined basis); otherwise the
Ex-Dates between it and the basis are tried as a new innermost stage, every option on
every tick of the ladder, the fewest closes off winning. A stretch stands when at most
ceil(MAX_OFF x bars) of its closes sit off the replay (parked question: how many?).

    uv run --with scipy python prototypes/kite_adjustment_chain/replay_run.py <all.pkl> <out.pkl> [max_off] [strict|loose] [SYMBOLS]

RULES=final in the environment replays the settled rule set instead (see FINAL below), and
score.py counts its result.
"""

from __future__ import annotations

import math
import pickle
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from fractions import Fraction as F

import numpy as np
from scipy.optimize import linprog

sys.path.insert(0, sys.argv[0].rsplit("/", 1)[0])
from chain import LADDER, Stage, replay, stage_window  # noqa: E402

MAX_OFF = float(sys.argv[3]) if len(sys.argv) > 3 else 0.05
# strict: a stage's r is fitted only at an Ex-Date no rule computes (no dividend, split or
# bonus there), and never as a free line
STRICT = len(sys.argv) > 4 and sys.argv[4] == "strict"
TICKS = (*LADDER, F(5))  # ₹5: ABBOTINDIA's adjusted closes sit on it
SIX = 10**6
TIES = ("half_even", "half_down", "half_up")  # Kite's ties vary by stage (HDFCBANK even, TATASTEEL down)
AMOUNT = re.compile(r"R[se]\.?\s*-?\s*(\d+(?:\.\d+)?)")


def dividend(subject: str) -> F | None:
    found = [F(a.rstrip(".")) for a in AMOUNT.findall(subject)]
    if not found:
        return None
    return found[0] if "includ" in subject.lower() else sum(found, F(0))


def computed(actions, ex_date, prev_close):
    """The stages Kite's rule gives an Ex-Date's actions, as (form, f, b): a split or bonus
    rescales by its ratio; a dividend D either divides by round(P / (P - D), 6) or is
    subtracted outright (TATASTEEL 2024: f = 1, b = 3.60, its dividend)."""
    ratio, sub, ok = F(1), F(0), False
    for ed, kind, subject, factor in actions:
        if ed != ex_date:
            continue
        if kind in ("split", "bonus") and factor:
            ratio *= factor
            ok = True
        elif kind == "dividend" and prev_close:
            d = dividend(subject)
            if d is not None and d < prev_close:
                sub += d
    found = [("reround", F(1), F(0))]  # a fresh rounding of the history, no rescale (BAJFINANCE)
    if sub and ok:
        found.append(("ratio", ratio, F(0)))  # the split or bonus alone, its dividend not adjusted (RITES)
    if sub:
        r = F(round(prev_close / (prev_close - sub) * SIX), SIX)
        found.append(("ratio", ratio / r, F(0)))
        found.append(("subtract", ratio, sub * ratio))
    elif ok:
        found.append(("ratio", ratio, F(0)))
        # Kite's divisor for a 1:3 bonus is 1.33, not 4/3 (ASTRAL, GAIL)
        r2 = F(round(1 / ratio * 100), 100)
        if r2 != 1 / ratio:
            found.append(("ratio_2dp", 1 / r2, F(0)))
    return found


# REMEASURE: the settled rules, switched on by the RULES environment variable (comma-separated):
#   r1  dividend eras: none before 2023-07-01, subtract to 2024-09-30, ratio from 2024-10-01;
#       no reround at a dividend Ex-Date
#   r2  split/bonus: exact or 2-decimal ratio only (also when a dividend shares the Ex-Date);
#       no reround at a split/bonus Ex-Date
#   r3  r fitted only where an Ex-Date in the gap carries a rights/other action
#   r4  every join re-chooses the innermost stage's tick and tie rule (and a fitted r) over the
#       whole Kite Basis, fewest closes off, every stretch standing
import os  # noqa: E402
from datetime import date as _date  # noqa: E402

RULES = set(filter(None, os.environ.get("RULES", "").split(",")))
ERA_SUB, ERA_RATIO = _date(2023, 7, 1), _date(2024, 10, 1)
FITTABLE = ("rights", "other")


def computed_rules(actions, ex_date, prev_close):
    ratio, sub, ok, div, other = F(1), F(0), False, False, False
    for ed, kind, subject, factor in actions:
        if ed != ex_date:
            continue
        if kind in ("split", "bonus") and factor:
            ratio *= factor
            ok = True
        elif kind == "dividend":
            div = True
            if prev_close:
                d = dividend(subject)
                if d is not None and d < prev_close:
                    sub += d
        elif kind in FITTABLE:
            other = True
    if "r1pre" in RULES and "r1" not in RULES:
        # only the pre-July-2023 half of rule 1: such a dividend carries no stage, no reround
        if div and ex_date < ERA_SUB:
            return [o for o in computed(actions, ex_date, None) if o[0] != "reround"]
        return computed(actions, ex_date, prev_close)
    if "r1" not in RULES or "r2" not in RULES:
        old = computed(actions, ex_date, prev_close)
    found = []
    # reround: only where nothing rule-computed sits (a fitted r of 1 is allowed at rights/other)
    if not ((("r1" in RULES) and div) or (("r2" in RULES) and ok)):
        found.append(("reround", F(1), F(0)))
    splits = []
    if ok:
        splits.append(("ratio", ratio))
        r2 = F(round(1 / ratio * 100), 100)
        if r2 != 1 / ratio and ("r2" in RULES or not sub):
            splits.append(("ratio_2dp", 1 / r2))
    if "r1" in RULES:
        found += [(form, f, F(0)) for form, f in splits]
        if sub and ex_date >= ERA_SUB:
            for form, f in (splits or [("ratio", F(1))]):
                if ex_date >= ERA_RATIO:
                    r = F(round(prev_close / (prev_close - sub) * SIX), SIX)
                    found.append(("ratio", f / r, F(0)))
                else:
                    found.append(("subtract", f, sub * f))
    else:
        found += [o for o in old if o[0] != "reround" and not (o[0] == "ratio_2dp" and "r2" in RULES)]
        found += [(form, f, F(0)) for form, f in splits if form == "ratio_2dp" and "r2" in RULES]
    return list(dict.fromkeys(found))


def fittable(actions, eds):
    return any(ed == e and kind in FITTABLE for ed, kind, _, _ in actions for e in eds)


# FINAL (RULES=final): the settled rule set, on its own and not combined with r1..r4.
#   - a split, bonus or consolidation stage divides by its exact ratio or that ratio rounded to
#     2 decimals, never fitted (the 2-decimal option is offered when a dividend shares the Ex-Date);
#   - dividends: no stage before 2023-07-01; subtraction or ratio to 2024-09-30; ratio from
#     2024-10-01. Any action whose purpose names a dividend, misspelt or not, is a dividend;
#     an amount that cannot be read gives no stage;
#   - a reround (r = 1, new tick) only at a dividend Ex-Date from 2023-07-01 with no split or bonus;
#   - r is fitted only at the Ex-Date of an action that is none of these (rights, demergers, others);
#   - each join re-chooses the innermost stage's tick and tie rule (and a fitted r) over the whole
#     Kite Basis: fewest closes off, then the coarsest tick;
#   - a stage enters the chain only when its stretch stands.
FINAL = "final" in RULES
DIVIDEND_WORD = re.compile(r"div(?:idend|dend|ided|idned)", re.I)


def action_class(kind, subject):
    if kind in ("split", "bonus", "consolidation"):
        return "split"
    if kind == "dividend" or DIVIDEND_WORD.search(subject or ""):
        return "dividend"
    return "fittable"


def computed_final(actions, ex_date, prev_close):
    ratio, sub, has_split, has_div = F(1), F(0), False, False
    for ed, kind, subject, factor in actions:
        if ed != ex_date:
            continue
        cls = action_class(kind, subject)
        if cls == "split":
            has_split = True
            if factor:
                ratio *= factor
        elif cls == "dividend":
            has_div = True
            d = dividend(subject) if prev_close else None
            if d is not None and d < prev_close:
                sub += d
    found = []
    splits = []
    if has_split and ratio != 1:
        splits.append(("split", ratio))
        r2 = F(round(1 / ratio * 100), 100)
        if r2 != 1 / ratio:
            splits.append(("split_2dp", 1 / r2))
    found += [(form, f, F(0)) for form, f in splits]
    if sub and ex_date >= ERA_SUB:
        r = F(round(prev_close / (prev_close - sub) * SIX), SIX)
        for form, f in (splits or [(None, F(1))]):
            pre = f"{form}+" if form else ""
            found.append((pre + "div_ratio", f / r, F(0)))
            if ex_date < ERA_RATIO:
                found.append((pre + "div_subtract", f, sub * f))
    if has_div and not has_split and ex_date >= ERA_SUB:
        found.append(("reround", F(1), F(0)))
    return list(dict.fromkeys(found))


def fit_kinds(actions, ed):
    """The fittable actions at an Ex-Date, named by what they are."""
    out = []
    for e, kind, subject, _ in actions:
        if e != ed or action_class(kind, subject) != "fittable":
            continue
        s = (subject or "").lower()
        if kind == "rights" or "right" in s:
            out.append("rights")
        elif "merger" in s or "arrange" in s or "scheme" in s:
            out.append("demerger/scheme")
        elif "buy" in s:
            out.append("buyback")
        elif "interest" in s:
            out.append("interest")
        elif "general meet" in s or "general meeting" in s or "agm" in s:
            out.append("general meeting")
        else:
            out.append("other")
    return out


def options_final(outer, pts, eds, actions, prev_close, allow_fit=True):
    found = []
    comp = [(ed, form, f, b) for ed in eds for form, f, b in computed_final(actions, ed, prev_close.get(ed))]
    fit_eds = [e for e in eds if fit_kinds(actions, e)] if allow_fit else []
    for tick in TICKS:
        r_fit = fit_r(outer, pts, tick) if fit_eds else None
        for ties in TIES:
            for ed, form, f, b in comp:
                found.append(dict(how="computed", form=form, ex_date=ed, r=1 / f, stage=Stage(ed, f, b, tick, ties)))
            if r_fit is not None:
                found.append(dict(how="fitted", form="fitted", ex_date=fit_eds[-1], r=r_fit,
                                  fit_kinds=sorted({k for e in fit_eds for k in fit_kinds(actions, e)}),
                                  stage=Stage(fit_eds[-1], 1 / r_fit, F(0), tick, ties)))
    for o in found:
        o["off"] = off_of([o["stage"], *outer], pts)
    rank = {"computed": 0, "fitted": 1}
    found.sort(key=lambda o: (len(o["off"]), rank[o["how"]], -o["stage"].tick, TIES.index(o["stage"].ties)))
    return found


def rechoose_final(chain, basis_how, members, pts_of):
    """The innermost stage's tick and tie rule (and, if fitted, r) re-chosen over the whole
    Kite Basis ``members``: every stretch standing, fewest closes off, then the coarsest tick.
    Returns (stage, closes off per member) or None."""
    own = [p for i in members for p in pts_of[i]]
    best = None
    for tick in TICKS:
        if basis_how == "fitted":
            r = fit_r(chain[1:], own, tick)
            if r is None:
                continue
            base_stage = replace(chain[0], f=1 / r, tick=tick)
        else:
            base_stage = replace(chain[0], tick=tick)
        for ties in TIES:
            trial = replace(base_stage, ties=ties)
            tc = [trial, *chain[1:]]
            per = {i: off_of(tc, pts_of[i]) for i in members}
            if all(len(per[i]) <= cap_of(len(pts_of[i])) for i in per):
                k = (sum(len(v) for v in per.values()), -tick, TIES.index(ties))
                if best is None or k < best[0]:
                    best = (k, trial, per)
    return None if best is None else best[1:]


# FINAL_STRICT (RULES=final_strict): FINAL, except in a mixed gap, one whose Ex-Dates hold both a
# computed-kind action (split, bonus, consolidation, dividend of any era) and a fittable one. There
# a fitted stage may not absorb, replace or distort a computed action:
#   (a) each computed-kind Ex-Date in the gap gets its own computed stage at its own Ex-Date, with
#       its computed value, or no stage where the rules allow none (any dividend may carry none, a
#       dividend before 2023-07-01 always does); a split, bonus or consolidation always has one;
#   (b) a fitted stage sits at a fittable action's own Ex-Date, in Ex-Date order among the gap's
#       computed stages (earlier ones inside it, later ones outside), and its r is fitted with
#       them in place, so it carries only what they do not;
#   (c) a fitted r within ABSORB_TOL of a value a computed-kind action of the gap would give
#       (split ratio, 2-decimal ratio, P / (P - D) of a dividend of any era, or a product of
#       these over Ex-Dates carrying no stage of their own) is that action absorbed, and is
#       refused: the closes that need it stay unexplained.
#   (d) in any gap, a fitted r within ABSORB_TOL of 1 is refused: it adjusts nothing, so it is a
#       reround, which only a dividend Ex-Date from 2023-07-01 may carry, as a computed stage.
# Each join re-chooses the group's ticks and tie rule (and its fitted r) over the whole basis.
# RULES=final_nofit_in_mixed_gaps bounds it: no fitted stage at all in a mixed gap.
FINAL = FINAL or any(r.startswith("final") for r in RULES)
STRICT_FIT = "final_strict" in RULES
NOFIT_MIXED = "final_nofit_in_mixed_gaps" in RULES
ABSORB_TOL = F(1, 1000)


def gap_info(actions, eds, prev_close):
    """Every action at the gap's Ex-Dates, its class, and the r values a computed-kind one would
    give (split ratio, 2-decimal ratio, P / (P - D) for a dividend of any era)."""
    out = []
    for ed in eds:
        P = prev_close.get(ed)
        acts = [(k, s, f) for e, k, s, f in actions if e == ed]
        ratio, sub = F(1), F(0)
        for k, s, f in acts:
            c = action_class(k, s)
            if c == "split" and f:
                ratio *= f
            elif c == "dividend" and P:
                d = dividend(s)
                if d is not None and d < P:
                    sub += d
        values = []
        if ratio != 1:
            values.append(("split", 1 / ratio))
            r2 = F(round(1 / ratio * 100), 100)
            if r2 != 1 / ratio:
                values.append(("split_2dp", r2))
        if sub:
            dr = P / (P - sub)
            values.append(("div", dr))
            if ratio != 1:
                values.append(("split*div", dr / ratio))
        out.append(dict(ex_date=ed, prev_close=None if P is None else float(P),
                        actions=[(k, action_class(k, s), s, None if f is None else float(f)) for k, s, f in acts],
                        values=[(lbl, float(v)) for lbl, v in values]))
    return out


def is_mixed(actions, eds):
    cls = {action_class(k, s) for e, k, s, _ in actions if e in eds}
    return "fittable" in cls and bool(cls & {"split", "dividend"})


def absorbs(r, unstaged_values):
    """The computed-kind value (or product of them) a fitted r reproduces within ABSORB_TOL, if any."""
    prods = [(F(1), ())]
    for vals in unstaged_values:
        prods = prods + [(p * v, lbl + (name,)) for p, lbl in prods for name, v in vals]
    best = None
    for p, lbl in prods[1:]:
        d = abs(r / p - 1)
        if d <= ABSORB_TOL and (best is None or d < best[0]):
            best = (d, lbl, p)
    return best


def _group_eval(layout, outer, pts, unstaged, tc, tf, ties):
    """Stages for a layout (Ex-Date order, innermost first) on ticks tc (computed) and tf (fitted):
    (stages, fitted r or None) or None when the fitted r cannot be found or absorbs a computed action."""
    stages, fit_at = [], None
    for k, ent in enumerate(layout):
        if ent[0] == "c":
            _, ed, form, f, b = ent
            stages.append(Stage(ed, f, b, tc, ties))
        else:
            fit_at = k
            stages.append(None)
    if fit_at is None:
        return stages, None
    inner, outer_g = stages[:fit_at], [*stages[fit_at + 1:], *outer]
    own = [(replay(inner, x), y) for x, y in pts] if inner else pts
    r = fit_r(outer_g, own, tf)
    if r is None or absorbs(r, unstaged):
        return None
    stages[fit_at] = Stage(layout[fit_at][1], 1 / r, F(0), tf, ties)
    return stages, r


def _ticks(layout):
    has_c = any(e[0] == "c" for e in layout)
    has_f = any(e[0] == "fit" for e in layout)
    return [(tc, tf) for tc in (TICKS if has_c else (F(1),)) for tf in (TICKS if has_f else (F(1),))]


def _layout_key(layout):
    return [(e[1], e[2] if e[0] == "c" else "fitted") for e in layout]


EXHAUSTIVE = 64  # layouts tried one by one up to this many; beyond it, coordinate descent


def off_within(chain, pts, cap):
    """The closes off the replay, or None as soon as more than ``cap`` are."""
    off = []
    for i, (x, y) in enumerate(pts):
        if replay(chain, x) != y:
            off.append(i)
            if len(off) > cap:
                return None
    return off


def options_strict(outer, pts, eds, actions, prev_close, cap):
    """The layouts of a mixed gap: one choice per computed-kind Ex-Date (its computed forms, or no
    stage where allowed) and at most one fitted stage at a fittable Ex-Date, scored as a group.
    Up to EXHAUSTIVE layouts per fitted choice are all tried; beyond that (a gap reaching back over
    many non-standing stretches), each Ex-Date's choice is improved in turn, two passes, on a
    shared tick and ties to even, and only the layout reached gets the full tick and tie search.
    Only layouts leaving at most ``cap`` closes off are returned: no other can enter the chain.
    Fitted Ex-Dates with the same computed-kind Ex-Dates either side are one choice (the latest)."""
    comp_eds = [e for e in eds if any(action_class(k, s) in ("split", "dividend") for d, k, s, _ in actions if d == e)]
    fit_eds = [e for e in eds if fit_kinds(actions, e)]
    # one fitted choice per run of fittable Ex-Dates between the same computed-kind ones: the latest
    side = lambda e: sum(1 for c in comp_eds if c < e or (c == e))
    fit_eds = [e for e in fit_eds if not any(side(o) == side(e) and o > e for o in fit_eds)]
    info = {g["ex_date"]: g for g in gap_info(actions, eds, prev_close)}
    choices = []
    for e in comp_eds:
        forms = computed_final(actions, e, prev_close.get(e))
        must = any(form.startswith("split") for form, _, _ in forms)
        choices.append([(e, c) for c in (forms if must else [None, *forms])])

    def layout_of(combo, fe):
        layout = [("c", e, *c) for e, c in combo if c is not None]
        if fe is not None:
            layout.append(("fit", fe))
        layout.sort(key=lambda ent: (ent[1], ent[0] == "fit"))
        return layout

    def unstaged_of(combo):
        return [[(lbl, F(v)) for lbl, v in info[e]["values"]] for e, c in combo if c is None and info[e]["values"]]

    def score(combo, fe, configs):
        layout = layout_of(combo, fe)
        if not layout:
            return []
        unstaged = unstaged_of(combo)
        out = []
        for tc, tf, ties in configs(layout):
            got = _group_eval(layout, outer, pts, unstaged, tc, tf, ties)
            if got is None:
                continue
            stages, r = got
            off = off_within([*stages, *outer], pts, cap)
            if off is None:
                continue
            out.append(dict(how="fitted" if fe else "computed", layout=layout, unstaged=unstaged,
                            stages=stages, off=off, r_fit=r, ex_date=fe or layout[-1][1],
                            form="+".join(f for _, f in _layout_key(layout)),
                            fit_kinds=fit_kinds(actions, fe) if fe else None,
                            key=(len(off), fe is not None, len(layout), -tc, -tf, TIES.index(ties))))
        return out

    full = lambda layout: [(tc, tf, ties) for tc, tf in _ticks(layout) for ties in TIES]
    quick = lambda layout: [(t if any(e[0] == "c" for e in layout) else F(1), t if any(e[0] == "fit" for e in layout) else F(1), "half_even") for t in TICKS]
    n_combos = math.prod(len(ch) for ch in choices)
    found = []
    for fe in [None, *reversed(fit_eds)]:  # ties go to the latest fittable Ex-Date, as in FINAL
        if n_combos <= EXHAUSTIVE:
            combos = [[]]
            for ch in choices:
                combos = [c + [x] for c in combos for x in ch]
            for combo in combos:
                found += score(combo, fe, full)
            continue
        combo = [ch[0] for ch in choices]  # every optional dividend without a stage, splits exact
        best = min(score(combo, fe, quick), key=lambda o: o["key"], default=None)
        for _ in range(2):
            for j, ch in enumerate(choices):
                for x in ch:
                    trial = [*combo[:j], x, *combo[j + 1:]]
                    got = min(score(trial, fe, quick), key=lambda o: o["key"], default=None)
                    if got is not None and (best is None or got["key"] < best["key"]):
                        best, combo = got, trial
        found += score(combo, fe, full)
    found.sort(key=lambda o: o["key"])
    return found


def rechoose_group(chain, basis, members, pts_of):
    """A group's ticks and tie rule (and fitted r) re-chosen over the whole Kite Basis, its layout
    kept: every stretch standing, fewest closes off, then the coarsest ticks."""
    n, layout, unstaged = basis["n"], basis["layout"], basis["unstaged"]
    outer = chain[n:]
    own = [p for i in members for p in pts_of[i]]
    best = None
    for tc, tf in _ticks(layout):
        for ties in TIES:
            got = _group_eval(layout, outer, own, unstaged, tc, tf, ties)
            if got is None:
                continue
            tcn = [*got[0], *outer]
            per = {i: off_of(tcn, pts_of[i]) for i in members}
            if all(len(per[i]) <= cap_of(len(pts_of[i])) for i in per):
                k = (sum(len(v) for v in per.values()), -tc, -tf, TIES.index(ties))
                if best is None or k < best[0]:
                    best = (k, got[0], per)
    return None if best is None else best[1:]


def off_of(chain, pts):
    return [i for i, (x, y) in enumerate(pts) if replay(chain, x) != y]


def cap_of(n):
    return math.ceil(MAX_OFF * n)


def fit_r(outer, pts, tick):
    """``_fit_r``, except that under FINAL_STRICT a fitted divisor within ABSORB_TOL of 1 is
    refused, in any gap: it adjusts nothing and only rounds again, which is a reround, and a
    reround stands only at a dividend Ex-Date from 2023-07-01, as a computed stage."""
    r = _fit_r(outer, pts, tick)
    if STRICT_FIT and r is not None and abs(r - 1) <= ABSORB_TOL:
        return None
    return r


def _fit_r(outer, pts, tick):
    """The divisor on the 6-decimal grid that the most closes allow: each close allows an
    interval of r; the point covered by the most intervals wins."""
    events = []
    for x, y in pts:
        w = stage_window(outer, tick, y)
        if w is None or w[0] <= 0:
            continue
        events.append((x / w[1], 0))  # opens (r small = u large)
        events.append((x / w[0], 1))  # closes
    if not events:
        return None
    events.sort()
    best, cur, at = -1, 0, None
    for k, (v, kind) in enumerate(events):
        if kind == 0:
            cur += 1
            if cur > best:
                best, at = cur, k
        else:
            cur -= 1
    lo = events[at][0]
    hi = next(v for v, kind in events[at + 1:] if kind == 1)
    for r in (F(math.ceil(lo * SIX), SIX), F(math.floor(hi * SIX), SIX), F(round((lo + hi) / 2 * SIX), SIX)):
        if lo <= r <= hi:
            return r
    return (lo + hi) / 2  # no 6-decimal r fits: noted as such


def fit_line(outer, pts, tick):
    """A free (f, b) by the largest-margin linear program, dropping the worst closes."""
    win = [stage_window(outer, tick, y) for _, y in pts]
    live = [i for i, w in enumerate(win) if w is not None]
    for _ in range(20):
        if len({pts[i][0] for i in live}) < 2:
            return None
        xs = np.array([float(pts[i][0]) for i in live]); m = xs.mean(); xc = xs - m
        lo = np.array([float(win[i][0]) for i in live]); hi = np.array([float(win[i][1]) for i in live])
        n = len(live)
        A = np.vstack([np.column_stack([-xc, -np.ones(n), -np.ones(n)]), np.column_stack([xc, np.ones(n), -np.ones(n)])])
        res = linprog([0, 0, 1], A_ub=A, b_ub=np.concatenate([-lo, hi]),
                      bounds=[(1e-6, 1e3), (None, None), (-1e3, None)], method="highs")
        if res.status != 0:
            return None
        f, c, z = res.x
        if z <= 1e-7:
            return F(f), F(f * m - c)
        viol = np.maximum(lo - (f * xc + c), (f * xc + c) - hi)
        live = [live[k] for k in range(n) if viol[k] < 0.9 * viol.max()]
    return None


def make(how, r, tick, ex_date, b=F(0)):
    return dict(how=how, ex_date=ex_date, r=r, stage=Stage(ex_date, 1 / r, b, tick))


def options(outer, pts, eds, actions, prev_close):
    """Every candidate new innermost stage for an Ex-Date boundary, with its closes off."""
    found = []
    orig = [(ed, form, f, b) for ed in eds for form, f, b in computed(actions, ed, prev_close.get(ed))]
    computable = any(form != "reround" for _, form, _, _ in orig)
    comp = ([(ed, form, f, b) for ed in eds for form, f, b in computed_rules(actions, ed, prev_close.get(ed))]
            if RULES & {"r1", "r2", "r1pre"} else orig)
    if "r3" in RULES:
        fit_eds = [e for e in eds if fittable(actions, [e])]
        may_fit, fit_ed = bool(fit_eds), (fit_eds[-1] if fit_eds else None)
    else:
        may_fit, fit_ed = not (STRICT and computable), eds[-1]
    for tick in TICKS:
        r_fit = fit_r(outer, pts, tick) if may_fit else None
        for ties in TIES:
            for ed, form, f, b in comp:
                found.append(dict(how="computed", form=form, ex_date=ed, r=1 / f, stage=Stage(ed, f, b, tick, ties)))
            if r_fit is not None and may_fit:
                found.append(dict(how="fitted", ex_date=fit_ed, r=r_fit, stage=Stage(fit_ed, 1 / r_fit, F(0), tick, ties)))
    scored = []
    for o in found:
        o["off"] = off_of([o["stage"], *outer], pts)
        scored.append(o)
    rank = {"computed": 0, "fitted": 1, "line": 2}
    key = lambda o: (len(o["off"]), rank[o["how"]], TIES.index(o["stage"].ties), -o["stage"].tick)
    scored.sort(key=key)
    if not STRICT and (not scored or scored[0]["off"]):
        # the last resort: a free line, on the tick of the best so far
        for tick in TICKS:
            got = fit_line(outer, pts, tick)
            if got:
                f, b = got
                o = dict(how="line", ex_date=eds[-1], r=1 / f, stage=Stage(eds[-1], f, b, tick))
                o["off"] = off_of([o["stage"], *outer], pts)
                scored.append(o)
        scored.sort(key=key)
    return scored


def run(item):
    (inst, series), stretches, ex, actions = item
    pts_of = {idx: [(F(e), F(k)) for _, _, e, k, _ in rows] for idx, rows in stretches}
    # NSE's close on the last day before each Ex-Date, from this series's rows
    days = sorted((r[0], r[2]) for _, rows in stretches for r in rows)
    prev_close = {}
    for ed in ex:
        k = next((j for j in range(len(days) - 1, -1, -1) if days[j][0] < ed), None) if days else None
        prev_close[ed] = F(days[k][1]) if k is not None else None
    chain: list[Stage] = []  # innermost first
    bases: list[dict] = []
    placed_in: dict[int, int | None] = {}
    depth: dict[int, int] = {}  # how many stages, from the outermost, a stretch passes through
    prev_idx = None
    for idx, rows in stretches:
        pts = pts_of[idx]
        cap = cap_of(len(pts))
        joined = False
        if bases:
            basis = bases[-1]
            off = off_of(chain, pts)
            if FINAL:
                if basis["how"] == "raw":
                    joined = len(off) <= cap
                elif basis.get("layout") is not None:
                    got = rechoose_group(chain, basis, [*basis["stretches"], idx], pts_of)
                    if got is not None:
                        group, per = got
                        chain[: basis["n"]] = group
                        joined = True
                        off = per[idx]
                else:
                    got = rechoose_final(chain, basis["how"], [*basis["stretches"], idx], pts_of)
                    if got is not None:
                        chain[0], per = got
                        basis["r"] = 1 / chain[0].f
                        joined = True
                        off = per[idx]
            elif "r4" in RULES and basis["how"] in ("computed", "fitted"):
                members = [i for i in basis["stretches"] if placed_in.get(i) is not None] + [idx]
                own = [p for i in members for p in pts_of[i]]
                best = None
                for tick in TICKS:
                    if basis["how"] == "fitted":
                        r = fit_r(chain[1:], own, tick)
                        if r is None:
                            continue
                        base_stage = replace(chain[0], f=1 / r, tick=tick)
                    else:
                        base_stage = replace(chain[0], tick=tick)
                    for ties in TIES:
                        trial = replace(base_stage, ties=ties)
                        tc = [trial, *chain[1:]]
                        per = {i: off_of(tc, pts_of[i]) for i in members}
                        if all(len(per[i]) <= cap_of(len(pts_of[i])) for i in per):
                            k = (sum(len(v) for v in per.values()), TIES.index(ties), -tick)
                            if best is None or k < best[0]:
                                best = (k, trial, per[idx])
                if best is not None:
                    chain[0] = best[1]
                    basis["r"] = 1 / best[1].f
                    joined = True
                    off = best[2]
            elif len(off) <= cap:
                joined = True
            elif basis["how"] == "computed":
                # a tick and tie rule read off a short stretch can be wrong (BAJFINANCE's 11
                # bars chose Rs 0.50 over Rs 1): choose them again over the whole basis
                for tick in TICKS:
                    for ties in TIES:
                        trial = replace(chain[0], tick=tick, ties=ties)
                        tc = [trial, *chain[1:]]
                        per = {i: off_of(tc, pts_of[i]) for i in [*basis["stretches"], idx]}
                        if all(len(per[i]) <= cap_of(len(pts_of[i])) for i in per):
                            chain[0] = trial
                            joined = True
                            off = per[idx]
                            break
                    if joined:
                        break
            elif basis["how"] in ("fitted", "line"):
                own = [p for i in basis["stretches"] for p in pts_of[i]] + pts
                t = chain[0].tick
                if basis["how"] == "fitted":
                    r = fit_r(chain[1:], own, t)
                    trial = None if r is None else replace(chain[0], f=1 / r)
                else:
                    got = fit_line(chain[1:], own, t)
                    trial = None if got is None else replace(chain[0], f=got[0], b=got[1])
                if trial is not None:
                    tc = [trial, *chain[1:]]
                    per = {i: off_of(tc, pts_of[i]) for i in [*basis["stretches"], idx]}
                    if all(len(per[i]) <= cap_of(len(pts_of[i])) for i in per):
                        chain[0] = trial
                        basis["r"] = 1 / trial.f
                        joined = True
                        off = per[idx]
            if joined:
                basis["stretches"].append(idx)
                placed_in[idx] = len(bases) - 1
        elif len(off_of([], pts)) <= cap:
            bases.append(dict(how="raw", stretches=[idx], r=None, tick=None, eds=[]))
            placed_in[idx] = 0
            joined = True
        if not joined and FINAL:
            # a stage enters the chain only when its own stretch stands; the Ex-Dates in play run
            # back to the last stretch that stood, so a stretch that did not costs nothing older
            eds = ex[idx:prev_idx] if prev_idx is not None else ex[idx:]
            mixed = bool(eds) and is_mixed(actions, eds)
            if mixed and STRICT_FIT:
                best = options_strict(chain, pts, eds, actions, prev_close, cap)
            else:
                best = options_final(chain, pts, eds, actions, prev_close, allow_fit=not (mixed and NOFIT_MIXED)) if eds else []
            if best and len(best[0]["off"]) <= cap and "layout" in best[0]:
                o = best[0]
                chain[0:0] = o["stages"]
                bases.append(dict(how=o["how"], form=o["form"], stretches=[idx], eds=eds, ex_date=o["ex_date"],
                                  fit_kinds=o.get("fit_kinds"), layout=o["layout"], unstaged=o["unstaged"],
                                  n=len(o["stages"]), gap=gap_info(actions, eds, prev_close),
                                  runner_up=[(p["form"], [float(1 / s.f) for s in p["stages"]], len(p["off"])) for p in best[1:4]]))
                placed_in[idx] = len(bases) - 1
                joined = True
            elif best and len(best[0]["off"]) <= cap:
                o = best[0]
                chain.insert(0, o["stage"])
                bases.append(dict(how=o["how"], form=o["form"], stretches=[idx], r=o["r"], tick=o["stage"].tick,
                                  eds=eds, ex_date=o["ex_date"], b=o["stage"].b, fit_kinds=o.get("fit_kinds"),
                                  n=1, gap=gap_info(actions, eds, prev_close),
                                  runner_up=[(p["how"], p.get("form"), float(p["r"]), float(p["stage"].b), str(p["stage"].tick), p["stage"].ties, len(p["off"])) for p in best[1:4]]))
                placed_in[idx] = len(bases) - 1
                joined = True
            else:
                placed_in[idx] = None
        elif not joined:
            eds = ex[idx:prev_idx] if prev_idx is not None else ex[idx:]
            best = options(chain, pts, eds, actions, prev_close) if eds else []
            # the best stage goes on even where this stretch does not stand, so a stretch
            # Kite holds other closes for (NMDC 2023-24) does not cost every older one its chain
            if best:
                o = best[0]
                chain.insert(0, o["stage"])
                bases.append(dict(how=o["how"], form=o.get("form"), stretches=[idx], r=o["r"], tick=o["stage"].tick,
                                  eds=eds, ex_date=o["ex_date"], b=o["stage"].b,
                                  runner_up=[(p["how"], p.get("form"), float(p["r"]), float(p["stage"].b), str(p["stage"].tick), p["stage"].ties, len(p["off"])) for p in best[1:4]]))
                placed_in[idx] = len(bases) - 1 if len(o["off"]) <= cap else None
            else:
                placed_in[idx] = None
        depth[idx] = len(chain)
        if not FINAL or placed_in.get(idx) is not None:
            prev_idx = idx
    # the stages each stretch passes through: every basis's stage from its own back to today
    staged = [k for k, b in enumerate(bases) if b["how"] != "raw"]
    out_stretches = {}
    for idx, rows in stretches:
        c = chain[len(chain) - depth[idx]:] if depth[idx] else []
        offs = set(off_of(c, pts_of[idx]))
        out_stretches[idx] = dict(
            start=rows[0][0], end=rows[-1][0], bars=len(rows), basis=placed_in[idx],
            stands=placed_in[idx] is not None,
            outcomes=Counter(r[4] for r in rows),
            reproduced_by_outcome=Counter(rows[i][4] for i in range(len(rows)) if i not in offs),
            off=[(rows[i][0], rows[i][2], rows[i][3], float(replay(c, pts_of[idx][i][0])), rows[i][4]) for i in sorted(offs)],
            # every close Kite Parity flags today: (day, outcome, reproduced exactly)
            mismatches=[(rows[i][0], rows[i][4], i not in offs) for i in range(len(rows)) if rows[i][4] != "match"],
        )
    # each basis's stages, innermost first; the newest basis's stages sit at the chain's end
    stages_of, pos = {}, len(chain)
    for k in staged:
        n = bases[k].get("n", 1)
        stages_of[k] = chain[pos - n:pos]
        pos -= n
    out_bases = []
    for k, b in enumerate(bases):
        group = stages_of.get(k, [])
        layout = b.get("layout")
        if layout is not None:
            # the stage a group is reported by: its fitted stage, else its outermost
            at = next((j for j, e in enumerate(layout) if e[0] == "fit"), len(group) - 1)
        else:
            at = 0
        s = group[at] if group else None
        out_bases.append(dict(how=b["how"], form=b.get("form"), stretches=b["stretches"], eds=b["eds"],
                              ex_date=b.get("ex_date"), r=None if s is None else float(1 / s.f),
                              b=None if s is None else float(s.b), tick=None if s is None else str(s.tick),
                              ties=None if s is None else s.ties,
                              runner_up=b.get("runner_up"), fit_kinds=b.get("fit_kinds"), gap=b.get("gap"),
                              group=layout is not None,
                              stages=[dict(ex_date=st.ex_date, how="fitted" if (layout and layout[j][0] == "fit") or (not layout and b["how"] == "fitted") else "computed",
                                           form=(layout[j][2] if layout[j][0] == "c" else "fitted") if layout else b.get("form"),
                                           r=float(1 / st.f), b=float(st.b), tick=str(st.tick), ties=st.ties)
                                      for j, st in enumerate(group)]))
    return (inst, series), dict(bases=out_bases, stretches=out_stretches,
                                actions=[a for a in actions if a[0] >= stretches[-1][1][0][0]])


if __name__ == "__main__":
    data = pickle.load(open(sys.argv[1], "rb"))
    by = defaultdict(list)
    for (inst, series, idx), rows in data["stretches"].items():
        by[(inst, series)].append((idx, rows))
    items = [(k, sorted(v, key=lambda t: -t[0]), data["ex"][k[0]], data["actions"][k[0]]) for k, v in by.items()]
    if len(sys.argv) > 5:
        items = [it for it in items if it[0][0].split("@")[0] in sys.argv[5].split(",")]
    with ProcessPoolExecutor() as pool:
        out = dict(pool.map(run, items, chunksize=2))
    pickle.dump(dict(meta=data["meta"], max_off=MAX_OFF, strict=STRICT, rules=sorted(RULES), results=out), open(sys.argv[2], "wb"))
    tot, rep = Counter(), Counter()
    for res in out.values():
        for s in res["stretches"].values():
            tot.update(s["outcomes"]); rep.update(s["reproduced_by_outcome"])
    print(MAX_OFF, "strict" if STRICT else "loose", sorted(RULES), {k: (rep[k], tot[k]) for k in tot})
