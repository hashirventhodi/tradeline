"""PROTOTYPE (#62): score a replay_run.py result under the settled counting rules. Throwaway.

    uv run python prototypes/kite_adjustment_chain/score.py <replay.pkl> [<other_replay.pkl>]

Counting (only the closes Kite Parity flags today are candidates; matched closes are untouched):
  - a flagged close is explained when its stretch stands, its Kite Basis has at least
    MIN_BASIS compared bars, and the chain reproduces it exactly;
  - every other flagged close is unexplained, and so is every missing or extra day parity
    leaves unexplained; the percentage is over the rows compared.
Each unexplained close gets one cause, first that applies: listing-day (the Instrument's
listing date), non-standing stretch, basis under MIN_BASIS bars, off-close in a standing
stretch. With a second result, the unexplained counts are compared by Instrument.

It also lists every fitted stage with the gap it covers (every Ex-Date between the older stretch
and the newer one it joins) and classifies it: clean (no split, bonus, consolidation or dividend
in the gap) or conflicting; a conflicting one is absorbing when its r is within ABSORB_TOL of a
value a computed-kind action of the gap without a stage of its own would give (split ratio,
2-decimal ratio, P / (P - D) of a dividend of any era, or a product of these). With a second
result, every computed-kind action in a mixed gap is listed with how each result handled it.
"""

import pickle
import sys
from collections import Counter, defaultdict
from datetime import date

MIN_BASIS = 20
CATS = ("unexplained", "additive_adjustment", "coarse_rounding")
GATE = 0.1  # per cent of rows compared, unchanged
ABSORB_TOL = 1e-3
COMPUTED_KINDS = ("split", "dividend")


def stages_of(b):
    """A basis's stages as dicts, innermost first (older results carry only the one stage)."""
    if b.get("stages") is not None:
        return b["stages"]
    return [dict(ex_date=b["ex_date"], how=b["how"], form=b.get("form"), r=b["r"], b=b["b"], tick=b["tick"], ties=b["ties"])]


def products(value_sets):
    """Every product of one value from each of a non-empty subset of value_sets: (value, labels)."""
    prods = [(1.0, ())]
    for ed, vals in value_sets:
        prods = prods + [(p * v, lbl + (f"{lbl_v} {ed}",)) for p, lbl in prods for lbl_v, v in vals]
    return prods[1:]


def classify(b):
    """(class, detail, nearest) for a basis with a fitted stage; None when it has none or no gap info."""
    fit = [st for st in stages_of(b) if st["how"] == "fitted"]
    if not fit or b.get("gap") is None:
        return None
    r = fit[0]["r"]
    staged = {st["ex_date"] for st in stages_of(b) if st["how"] == "computed"}
    comp = [g for g in b["gap"] if any(a[1] in COMPUTED_KINDS for a in g["actions"])]
    if not comp:
        return "clean", "", None
    unstaged = [g for g in comp if g["ex_date"] not in staged and g["values"]]
    sets = [(g["ex_date"], g["values"]) for g in unstaged]
    near = min(((abs(r / p - 1), lbl, p) for p, lbl in products(sets)), default=None)
    if near and near[0] <= ABSORB_TOL:
        return "conflicting: absorbing", f"r {r:.6f} = {' x '.join(near[1])} ({near[2]:.6f}, off {near[0]:.1e})", near
    # a split, bonus or consolidation with no stage of its own was applied by Kite all the same, so
    # a fitted r over its gap carries it: absorbing, with what is left over the fitted action's own
    splits = [(g["ex_date"], v) for g in unstaged for lbl, v in g["values"] if lbl == "split"]
    if splits:
        p = 1.0
        for _, v in splits:
            p *= v
        names = " x ".join(f"split {ed} ({v:g})" for ed, v in splits)
        return "conflicting: absorbing + residual", f"r {r:.6f} = {names} x residual {r / p:.6f}", near
    if abs(r - 1) <= 1e-4:
        return "conflicting: near-identity", f"r {r:.6f}, not a computed value", near
    return "conflicting: separate", f"r {r:.6f}, not a computed value", near


def listing_date(inst):
    try:
        return date.fromisoformat(inst.split("@", 1)[1])
    except (IndexError, ValueError):
        return None


def score(path):
    D = pickle.load(open(path, "rb"))
    meta, R = D["meta"], D["results"]
    by_inst, by_cause = Counter(), Counter()
    total, explained = Counter(), Counter()
    via_fitted = Counter()
    stages = Counter()
    listing_month = 0
    fitted_rows = []
    non_standing = 0
    for (inst, series), res in R.items():
        bases = res["bases"]
        bars = Counter()
        for st in res["stretches"].values():
            if st["basis"] is not None:
                bars[st["basis"]] += st["bars"]
        for k, b in enumerate(bases):
            if b["how"] == "raw":
                continue
            big = bars[k] >= MIN_BASIS
            for st in stages_of(b):
                if st["how"] == "fitted":
                    stages[("fitted", "+".join(b.get("fit_kinds") or ["?"]), big)] += 1
                else:
                    stages[("computed", st["form"], big)] += 1
            got = classify(b)
            if got is not None or b["how"] == "fitted":
                fitted_rows.append((inst.split("@")[0], series, b, got))
        non_standing += sum(1 for st in res["stretches"].values() if st["basis"] is None)
        listed = listing_date(inst)
        for st in res["stretches"].values():
            k = st["basis"]
            chain = [] if k is None else [b for b in bases[: k + 1] if b["how"] != "raw"]
            fitted = any(b["how"] == "fitted" for b in chain)
            for day, outcome, reproduced in st.get("mismatches", []):
                total[outcome] += 1
                if k is not None and bars[k] >= MIN_BASIS and reproduced:
                    explained[outcome] += 1
                    if fitted:
                        via_fitted[outcome] += 1
                    continue
                by_inst[f"{inst.split('@')[0]} {series}"] += 1
                if day == listed:
                    by_cause["listing-day"] += 1
                elif k is None:
                    by_cause["non-standing stretch"] += 1
                elif bars[k] < MIN_BASIS:
                    by_cause["basis under 20 bars"] += 1
                else:
                    by_cause["off-close in standing stretch"] += 1
                if listed and 0 < (day - listed).days <= 31:
                    listing_month += 1
    gaps = meta["missing_unexplained"] + meta["extra_unexplained"]
    by_cause["missing day"] += meta["missing_unexplained"]
    if meta["extra_unexplained"]:
        by_cause["extra day"] += meta["extra_unexplained"]
    un = sum(total.values()) - sum(explained.values()) + gaps
    return dict(D=D, meta=meta, un=un, by_inst=by_inst, by_cause=by_cause, total=total, explained=explained,
                via_fitted=via_fitted, stages=stages, listing_month=listing_month, fitted_rows=fitted_rows,
                non_standing=non_standing,
                stretches=sum(len(res["stretches"]) for res in R.values()))


def handling(res, ed):
    """How a result handled a computed-kind Ex-Date: its own stage, folded into a fitted stage, or none."""
    for b in res["bases"]:
        if b["how"] == "raw" or ed not in b["eds"]:
            continue
        own = [st for st in stages_of(b) if st["how"] == "computed" and st["ex_date"] == ed]
        if own:
            st = own[0]
            return f"own stage {st['form']} r={st['r']:.6f}{f' b={st[chr(98)]:g}' if st['b'] else ''} t={st['tick']}"
        fit = [st for st in stages_of(b) if st["how"] == "fitted"]
        if fit:
            got = classify(b)
            tag = " (absorbed)" if got and got[0].endswith("absorbing") and any(str(ed) in x for x in got[2][1]) else ""
            return f"no own stage; in gap of fitted r={fit[0]['r']:.6f} at {fit[0]['ex_date']}{tag}"
        st = stages_of(b)[-1]
        return f"no own stage; gap's stage is {st['form']} at {st['ex_date']}"
    return "no stage; no standing stage covers its gap"


def main():
    s = score(sys.argv[1])
    meta = s["meta"]
    print(f"rules {s['D'].get('rules')}, max_off {s['D']['max_off']}")
    pct = 100 * s["un"] / meta["rows"]
    print(f"unexplained {s['un']} (incl. {meta['missing_unexplained']} missing, {meta['extra_unexplained']} extra)"
          f" = {pct:.4f}% of {meta['rows']} rows compared: {'PASS' if pct <= GATE else 'FAIL'} against the {GATE} % gate")
    nfit = sum(v for k, v in s["stages"].items() if k[0] == "fitted")
    print(f"fitted stages {nfit}; explained closes depending on a fitted stage {sum(s['via_fitted'].values())};"
          f" non-standing stretches {s['non_standing']} of {s['stretches']}")
    print("\nby cause:")
    for c, n in s["by_cause"].most_common():
        print(f"  {c:32} {n}")
    print(f"  (of the rest, {s['listing_month']} fall within 31 days after a listing date)")
    print("\nby Instrument (top 15):")
    for k, n in s["by_inst"].most_common(15):
        print(f"  {k:24} {n}")
    print("\nflagged closes explained, by today's outcome:")
    for c in CATS:
        print(f"  {c:22} {s['explained'][c]:>6} / {s['total'][c]:<6}  via a chain with a fitted stage: {s['via_fitted'][c]}")
    print(f"  {'all':22} {sum(s['explained'].values()):>6} / {sum(s['total'].values()):<6}"
          f"  via a chain with a fitted stage: {sum(s['via_fitted'].values())}")
    print("\nstages entering the chain (basis >= 20 bars / under 20):")
    keys = sorted({k[:2] for k in s["stages"]})
    for how, form in keys:
        print(f"  {how:9} {form:28} {s['stages'][(how, form, True)]:>4} / {s['stages'][(how, form, False)]}")
    print("\nfitted stages and their gaps (class, Instrument, fitted Ex-Date, r, gap):")
    cls = Counter()
    for name, series, b, got in sorted(s["fitted_rows"], key=lambda t: (t[3] is None or t[3][0] == "clean", t[0])):
        c = got[0] if got else "unclassified (no gap info)"
        cls[c] += 1
        if got and got[0] == "clean":
            continue
        fit = [st for st in stages_of(b) if st["how"] == "fitted"][0]
        print(f"  {c:28} {name} {series} fitted at {fit['ex_date']} r={fit['r']:.6f}  {got[1] if got else ''}")
        for g in b.get("gap") or []:
            for kind, klass, subject, factor in g["actions"]:
                own = [st for st in stages_of(b) if st["ex_date"] == g["ex_date"] and st["how"] == "computed"]
                mark = f" -> own stage {own[0]['form']} r={own[0]['r']:.6f}" if own else ""
                print(f"      {g['ex_date']} {klass:9} {kind:9} {(subject or '')[:60]!r}"
                      f"{' P=' + format(g['prev_close'], 'g') if g['prev_close'] else ''}{mark}")
        if got and got[2]:
            print(f"      nearest computed value {got[2][2]:.6f} ({' x '.join(got[2][1])}), off {got[2][0]:.1e}")
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(cls.items())))
    if len(sys.argv) > 2:
        o = score(sys.argv[2])
        R1, R2 = s["D"]["results"], o["D"]["results"]
        print(f"\ncomputed-kind actions in mixed gaps, as handled by {sys.argv[2]} -> {sys.argv[1]}:")
        seen = set()
        for R in (R2, R1):
            for key, res in R.items():
                for b in res["bases"]:
                    gap = b.get("gap") or []
                    if not any(a[1] == "fittable" for g in gap for a in g["actions"]):
                        continue
                    for g in gap:
                        for a in g["actions"]:
                            if a[1] in COMPUTED_KINDS and (key, g["ex_date"], a[2]) not in seen:
                                seen.add((key, g["ex_date"], a[2]))
                                vals = ", ".join(f"{lbl} {v:.6f}" for lbl, v in g["values"]) or "no value"
                                print(f"  {key[0].split('@')[0]} {g['ex_date']} {a[0]} {(a[2] or '')[:40]!r} [{vals}]")
                                print(f"      {handling(R2[key], g['ex_date']) if key in R2 else '-'}")
                                print(f"   -> {handling(R1[key], g['ex_date']) if key in R1 else '-'}")
        print(f"\nagainst {sys.argv[2]}: {o['un']} unexplained there")
        d = {k: s["by_inst"][k] - o["by_inst"][k] for k in set(s["by_inst"]) | set(o["by_inst"])}
        for k, v in sorted(d.items(), key=lambda kv: -abs(kv[1]))[:25]:
            if v:
                print(f"  {k:24} {o['by_inst'][k]:>5} -> {s['by_inst'][k]:<5} ({v:+d})")


if __name__ == "__main__":
    main()
