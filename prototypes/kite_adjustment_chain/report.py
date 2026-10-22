"""PROTOTYPE (#62): summarise a replay_run.py result. Throwaway.
    uv run --with scipy python prototypes/kite_adjustment_chain/report.py <v2.pkl>
"""
import pickle, sys
from collections import Counter, defaultdict
D = pickle.load(open(sys.argv[1], "rb")); R = D["results"]; meta = D["meta"]
def form(res, st):
    if st["basis"] is None: return "does not stand"
    # the innermost stage a close passes through decides how it was reached; report the weakest in its chain
    chain = [b for b in res["bases"][: st["basis"] + 1] if b["how"] != "raw"]
    if not chain: return "raw"
    hows = {b["how"] if b["how"] != "computed" else "computed/" + b["form"] for b in chain}
    for w in ("line", "fitted", "computed/reround", "computed/ratio_2dp", "computed/subtract", "computed/ratio"):
        if w in hows: return w
by_form = defaultdict(Counter); rep_form = defaultdict(Counter)
off_unexpl = defaultdict(list)
for (i, s), res in R.items():
    for idx, st in res["stretches"].items():
        f = form(res, st)
        by_form[f].update(st["outcomes"]); rep_form[f].update(st["reproduced_by_outcome"])
        for o in st["off"]:
            if o[4] != "match": off_unexpl[(i.split("@")[0], s)].append((idx, st["basis"] is not None) + o)
cats = ["match", "additive_adjustment", "coarse_rounding", "unexplained"]
print(f"{'weakest stage in chain':22} " + " ".join(f"{c[:12]:>22}" for c in cats))
for f in ("raw", "computed/ratio", "computed/ratio_2dp", "computed/subtract", "computed/reround", "fitted", "line", "does not stand"):
    print(f"{f:22} " + " ".join(f"{rep_form[f][c]:>10}/{by_form[f][c]:<11}" for c in cats))
tot_mis = sum(sum(by_form[f][c] for f in by_form) for c in cats[1:])
rep_mis = sum(sum(rep_form[f][c] for f in by_form) for c in cats[1:])
un = tot_mis - rep_mis + meta["missing_unexplained"] + meta["extra_unexplained"]
print(f"parity mismatches {tot_mis}, reproduced {rep_mis}; unexplained {un} incl. {meta['missing_unexplained']} missing = {100*un/meta['rows']:.4f}% of {meta['rows']} rows")
print("\nmismatched closes off the replay, by Instrument:")
for k, L in sorted(off_unexpl.items(), key=lambda kv: -len(kv[1])):
    print(f"  {k[0]} {k[1]}: {len(L)}  e.g. " + "; ".join(f"{o[2]} nse {o[3]} kite {o[4]} replay {o[5]:.2f}" for o in L[:3]))

CASES = {
    "two stages proven": "TATASTEEL INOXWIND TRIDENT",
    "1 paisa, 70-85%": "CANBK IOC GAIL",
    "the open question": "GNFC HINDPETRO M&MFIN BPCL",
    "Rs 0.50 tick, fail one stage": "OFSS BSE",
    "one stage passes today": "ASHOKLEY HEROMOTOCO NIITTECH NAUKRI GODFRYPHLP TATAINVEST MCX",
    "listing-day closes": "DMART SBILIFE SHANKARA AUBANK PNBHOUSING TEJASNET DIXON HUDCO LAURUSLABS VBL CROMPGREAV",
}
print("\ncases (closes parity does not match today: reproduced / all):")
for case, names in CASES.items():
    print(f"## {case}")
    for name in names.split():
        for (i, s_), res in sorted(R.items()):
            if i.split("@")[0] != name: continue
            mis = Counter(); rep = Counter()
            for st in res["stretches"].values():
                mis.update({k: v for k, v in st["outcomes"].items() if k != "match"})
                rep.update({k: v for k, v in st["reproduced_by_outcome"].items() if k != "match"})
            if not sum(mis.values()): continue
            stages = [f"{b['ex_date']} {b['how'] if b['how'] != 'computed' else b['form']} r={b['r']:.6f}{f' b={b[chr(98)]:g}' if b['b'] else ''} t={b['tick']}{'' if b['ties'] == 'half_even' else ' ' + b['ties']}"
                      for b in res["bases"] if b["how"] != "raw"]
            print(f"  {name} {s_}: {sum(rep.values())}/{sum(mis.values())} " + " ".join(f"{k[:5]} {rep[k]}/{v}" for k, v in mis.items()))
            for st in stages: print("      stage", st)
