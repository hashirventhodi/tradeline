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


def off_of(chain, pts):
    return [i for i, (x, y) in enumerate(pts) if replay(chain, x) != y]


def cap_of(n):
    return math.ceil(MAX_OFF * n)


def fit_r(outer, pts, tick):
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
    comp = [(ed, form, f, b) for ed in eds for form, f, b in computed(actions, ed, prev_close.get(ed))]
    computable = any(form != "reround" for _, form, _, _ in comp)
    for tick in TICKS:
        r_fit = fit_r(outer, pts, tick)
        for ties in TIES:
            for ed, form, f, b in comp:
                found.append(dict(how="computed", form=form, ex_date=ed, r=1 / f, stage=Stage(ed, f, b, tick, ties)))
            if r_fit is not None and not (STRICT and computable):
                found.append(dict(how="fitted", ex_date=eds[-1], r=r_fit, stage=Stage(eds[-1], 1 / r_fit, F(0), tick, ties)))
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
            if len(off) <= cap:
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
        if not joined:
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
        )
    stage_of = {k: chain[len(chain) - 1 - j] for j, k in enumerate(staged)}
    out_bases = []
    for k, b in enumerate(bases):
        s = stage_of.get(k)
        out_bases.append(dict(how=b["how"], form=b.get("form"), stretches=b["stretches"], eds=b["eds"],
                              ex_date=b.get("ex_date"), r=None if s is None else float(1 / s.f),
                              b=None if s is None else float(s.b), tick=None if s is None else str(s.tick),
                              ties=None if s is None else s.ties,
                              runner_up=b.get("runner_up")))
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
    pickle.dump(dict(meta=data["meta"], max_off=MAX_OFF, strict=STRICT, results=out), open(sys.argv[2], "wb"))
    tot, rep = Counter(), Counter()
    for res in out.values():
        for s in res["stretches"].values():
            tot.update(s["outcomes"]); rep.update(s["reproduced_by_outcome"])
    print(MAX_OFF, "strict" if STRICT else "loose", {k: (rep[k], tot[k]) for k in tot})
