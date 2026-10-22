"""PROTOTYPE (#62): write kite-adjustment-chain.html, a single-file demo of Kite's Adjustment
replayed stage by stage, with the real closes of a few Instruments embedded. Throwaway.

    uv run python prototypes/kite_adjustment_chain/build_demo.py <all.pkl> <out.html>
"""

import json
import pickle
import sys
from fractions import Fraction as F
from pathlib import Path

A = pickle.load(open(sys.argv[1], "rb"))
CODE = {"match": "m", "additive_adjustment": "a", "coarse_rounding": "c", "unexplained": "u"}
NAMES = ["ITC", "TATASTEEL", "GAIL", "IOC", "HDFCBANK", "OFSS", "NMDC", "SHANKARA"]


def frac(v):
    f = F(v)
    return [str(f.numerator), str(f.denominator)]


def stage(ex, label, r, tick, ties="half_even", b="0", on=True):
    return dict(ex=ex, label=label, r=frac(r), b=frac(b), tick=int(F(tick) * 100), ties=ties, on=on)


data = {}
for name in NAMES:
    inst = next(k for k in A["ex"] if k.split("@")[0] == name)
    stretches = sorted(((i, rows) for (a, s, i), rows in A["stretches"].items() if a == inst and s == "EQ"), key=lambda t: -t[0])
    data[name] = dict(
        key=inst,
        stretches=[dict(idx=i, rows=[[str(r[0]), str(r[2]), str(r[3]), CODE[r[4]]] for r in rows]) for i, rows in stretches],
        actions=[[str(a[0]), a[1], a[2]] for a in A["actions"][inst] if a[0] >= stretches[-1][1][0][0]],
    )

# The stages the strict replay found (replay_run.py), and the wrong turns the walkthroughs take.
STAGES = {
    "ITC": [
        stage("2026-05-27", "Dividend Rs 8: divide by 301.65 / 293.65, rounded to 6 decimals", "1.027243", "0.05"),
        stage("2026-02-04", "Interim dividend Rs 6.50: divide by 316.65 / 310.15, to 6 decimals", "1.020958", "0.05"),
        stage("2025-01-06", "Demerger (ITC Hotels): divisor read from Kite's closes", "1.156203", "0.05"),
    ],
    "TATASTEEL": [
        stage("2025-06-06", "Dividend Rs 3.60: divide by P / (P - D), to 6 decimals", "1.023321", "0.01"),
        stage("2024-06-21", "Dividend Rs 3.60: subtract it", "1", "0.01", b="3.6"),
        stage("2022-07-28", "Split 1:10: divide by 10, ties down", "10", "0.05", "half_down"),
    ],
    "GAIL": [
        stage("2026-02-05", "Dividend: divide by P / (P - D)", "1.03117", "0.01"),
        stage("2025-02-07", "Dividend: divide by P / (P - D)", "1.03781", "0.01"),
        stage("2024-02-06", "Dividend Rs 5.50: subtract it", "1", "0.05", b="5.5"),
        stage("2022-09-06", "Bonus 1:2: divide by 1.5", "1.5", "0.05"),
        stage("2019-07-09", "Bonus 1:1: divide by 2, ties down", "2", "0.05", "half_down"),
        stage("2018-03-27", "Bonus 1:3: divide by 1.33 (not 4/3)", "1.33", "0.01"),
        stage("2017-03-09", "Bonus 1:3: divide by 1.33 (not 4/3)", "1.33", "0.01"),
    ],
    "IOC": [
        stage("2025-12-18", "Dividend: divide by P / (P - D)", "1.030645", "0.01"),
        stage("2025-08-08", "Dividend: divide by P / (P - D)", "1.021634", "0.01"),
        stage("2024-07-12", "Dividend Rs 7: subtract it", "1", "0.01", b="7"),
        stage("2023-11-10", "Dividend Rs 5: subtract it", "1", "0.05", b="5"),
        stage("2023-07-28", "Dividend Rs 3: subtract it", "1", "0.05", b="3"),
        stage("2022-06-30", "Bonus 1:2: divide by 1.5", "1.5", "0.05"),
        stage("2018-03-15", "Bonus 1:1: divide by 2", "2", "0.01"),
    ],
    "HDFCBANK": [
        stage("2025-08-26", "Bonus 1:1: divide by 2, on a 10 paisa tick", "2", "0.10"),
        stage("2019-09-19", "Split 1:2: divide by 2, ties down", "2", "0.05", "half_down"),
    ],
    "OFSS": [
        stage("2026-05-07", "Dividend: divide by P / (P - D), on a Rs 0.50 tick", "1.028649", "0.50"),
        stage("2025-05-08", "Dividend: divide by P / (P - D), on a Rs 0.50 tick", "1.032057", "0.50"),
        stage("2024-05-07", "Dividend Rs 240: subtract it", "1", "0.05", b="240"),
    ],
    "NMDC": [
        stage("2026-02-13", "Dividend: divide by P / (P - D)", "1.030399", "0.01"),
        stage("2025-03-21", "Dividend: divide by P / (P - D)", "1.034592", "0.01"),
        stage("2024-12-27", "Bonus 2:1: divide by 3", "3", "0.01"),
        stage("2024-02-27", "Dividend Rs 5.75: divide by P / (P - D)", "1.025087", "0.05"),
    ],
    "SHANKARA": [],
}
for name in NAMES:
    data[name]["stages"] = STAGES[name]


def load(name, upto=None):
    """Load an Instrument with only its newest ``upto`` stages switched on."""
    st = [dict(s) for s in STAGES[name]]
    for k, s in enumerate(st):
        s["on"] = upto is None or k < upto
    return {"op": "load", "inst": name, "stages": st}


def setop(name, ex, **patch):
    return {"op": "set", "ex": ex, "patch": patch}


SCENARIOS = [
    dict(id="itc", title="ITC: a dividend divides", inst="ITC",
         blurb="Kite adjusts a dividend by dividing every older close by P / (P - D), with P the close before "
               "the Ex-Date, and rounds that divisor to 6 decimals before using it. Watch the 2026-05-27 stage: "
               "the exact divisor leaves one close a paisa off; the 6-decimal one reproduces every close.",
         steps=[
             dict(label="Start from Kite's raw closes, no adjustment", ops=[load("ITC", 0)]),
             dict(label="Add the 2026-05-27 dividend with the exact divisor 301.65 / 293.65",
                  ops=[setop("ITC", "2026-05-27", on=True, r=frac(F("301.65") / F("293.65")))]),
             dict(label="Round that divisor to 6 decimals (1.027243)", ops=[setop("ITC", "2026-05-27", r=frac("1.027243"))]),
             dict(label="Add the 2026-02-04 interim dividend the same way", ops=[setop("ITC", "2026-02-04", on=True)]),
             dict(label="Add the 2025-01-06 demerger, its divisor read from Kite's closes",
                  ops=[setop("ITC", "2025-01-06", on=True)]),
         ]),
    dict(id="tata", title="TATASTEEL: subtract, then divide", inst="TATASTEEL",
         blurb="In 2024 Kite subtracted the dividend outright; in 2025 it divided. Its 2022 split rounds "
               "half-tick ties down, not to even. These are the 'additive adjustment' and the hidden "
               "5 paisa grid the #59 refit found.",
         steps=[
             dict(label="Start with the 2025 dividend stage only", ops=[load("TATASTEEL", 1)]),
             dict(label="Treat the 2024-06-21 dividend as a divisor too (P / (P - D) = 1.020148)",
                  ops=[setop("TATASTEEL", "2024-06-21", on=True, r=frac("1.020148"), b=frac("0"))]),
             dict(label="Subtract Rs 3.60 instead", ops=[setop("TATASTEEL", "2024-06-21", r=frac("1"), b=frac("3.6"))]),
             dict(label="Add the 2022 split, ties to even", ops=[setop("TATASTEEL", "2022-07-28", on=True, ties="half_even")]),
             dict(label="Round the split's ties down", ops=[setop("TATASTEEL", "2022-07-28", ties="half_down")]),
         ]),
    dict(id="gail", title="GAIL: a 1:3 bonus divides by 1.33", inst="GAIL",
         blurb="GAIL is the case that matched only 70-85% under two stages. It takes seven, and its two 1:3 "
               "bonuses divide by 1.33, not by 4/3.",
         steps=[
             dict(label="All seven stages, with the 1:3 bonuses at 4/3",
                  ops=[load("GAIL"), setop("GAIL", "2018-03-27", r=frac(F(4, 3))), setop("GAIL", "2017-03-09", r=frac(F(4, 3)))]),
             dict(label="Divide the 2018 bonus by 1.33", ops=[setop("GAIL", "2018-03-27", r=frac("1.33"))]),
             dict(label="Divide the 2017 bonus by 1.33", ops=[setop("GAIL", "2017-03-09", r=frac("1.33"))]),
         ]),
    dict(id="ioc", title="IOC: the subtraction era", inst="IOC",
         blurb="From July 2023 to September 2024 Kite subtracted dividends (three stages here); from 2025 it "
               "divides. Switch a subtraction to a divisor and its closes fall away.",
         steps=[
             dict(label="The chain the replay found", ops=[load("IOC")]),
             dict(label="Make the 2024-07-12 Rs 7 stage a divisor instead (P / (P - D))",
                  ops=[setop("IOC", "2024-07-12", r=frac("1.041806"), b=frac("0"))]),
             dict(label="Put it back to a subtraction", ops=[setop("IOC", "2024-07-12", r=frac("1"), b=frac("7"))]),
         ]),
    dict(id="hdfc", title="HDFCBANK: a stage's own tick", inst="HDFCBANK",
         blurb="Each stage rounds to its own tick. HDFCBANK's 2025 bonus rounded to 10 paisa, ties to even, "
               "over a split that rounded to 5 paisa, ties down. And its 2026 dividend (1.7%) was not adjusted.",
         steps=[
             dict(label="The chain the replay found", ops=[load("HDFCBANK")]),
             dict(label="Round the 2025 bonus to 5 paisa instead", ops=[setop("HDFCBANK", "2025-08-26", tick=5)]),
             dict(label="Back to 10 paisa", ops=[setop("HDFCBANK", "2025-08-26", tick=10)]),
         ]),
    dict(id="ofss", title="OFSS: Rs 0.50, and Rs 240 off", inst="OFSS",
         blurb="OFSS failed a single stage on a Rs 0.50 tick. It is three: a Rs 240 dividend subtracted in 2024, "
               "then two dividends divided and rounded to Rs 0.50.",
         steps=[
             dict(label="The chain the replay found", ops=[load("OFSS")]),
             dict(label="Drop the 2024 subtraction", ops=[setop("OFSS", "2024-05-07", on=False)]),
             dict(label="Put it back", ops=[setop("OFSS", "2024-05-07", on=True)]),
         ]),
    dict(id="nmdc", title="NMDC: what no chain reproduces", inst="NMDC",
         blurb="Back to 2024-02-27 the chain reproduces NMDC. Before that Kite's close wanders 0.2-2% from any "
               "constant multiple of NSE's: Kite holds different closes, not a different adjustment. These "
               "stay unexplained.",
         steps=[dict(label="The chain the replay found", ops=[load("NMDC")])]),
    dict(id="shankara", title="SHANKARA: a listing month", inst="SHANKARA",
         blurb="From listing (2017-04-05) to 2017-05-15 Kite's closes differ from NSE's by Rs 1.5 to 6 either way; "
               "afterwards they are NSE's own. No adjustment is involved, and none should explain them.",
         steps=[dict(label="Kite's raw closes", ops=[load("SHANKARA")])]),
]

html = Path(sys.argv[0]).with_name("demo_template.html").read_text()
html = html.replace("/*DATA*/", json.dumps(dict(data=data, scenarios=SCENARIOS), separators=(",", ":")))
Path(sys.argv[2]).write_text(html)
print(sys.argv[2], len(html))
