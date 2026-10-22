"""PROTOTYPE (#62): Kite's Adjustment as a chain of stages. Pure functions, exact arithmetic.

A stage is what Kite did to an Instrument's history before one Ex-Date: every close
already held (itself adjusted and rounded by earlier stages) becomes
round_to_tick(f x close - b). Today's close for a day is the raw close passed through
every stage whose Ex-Date is after that day, earliest first:

    kite = S_n( ... S_2( S_1( nse_raw ) ) )

``replay`` runs a chain forward; ``preimage`` runs it backward, giving the interval of
values one inner stage must output for the chain to land exactly on Kite's close. That
interval is what lets a new inner stage be found by a 2-variable linear program.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date
from fractions import Fraction as F

# The tick ladder, finest first: the ticks Kite's closes are read on (decided 3).
LADDER = (F(1, 100), F(5, 100), F(10, 100), F(50, 100), F(1))


@dataclass(frozen=True)
class Stage:
    ex_date: date  # the Ex-Date it adjusts the history before
    f: F  # the rescale
    b: F  # the subtraction (0 for a plain rescale)
    tick: F  # the tick it rounds to
    ties: str = "half_even"  # how an exact half-tick goes: half_even, half_down, half_up


def round_to(v: F, tick: F, mode: str = "half_even") -> F:
    q = v / tick
    n = math.floor(q)
    r = q - n
    if r > F(1, 2) or (r == F(1, 2) and (mode == "half_up" or (mode == "half_even" and n % 2))):
        n += 1
    return n * tick


def apply(stage: Stage, v: F) -> F:
    return round_to(stage.f * v - stage.b, stage.tick, stage.ties)


def replay(chain: list[Stage], raw: F) -> F:
    """``chain`` innermost (earliest Ex-Date) first."""
    v = raw
    for s in chain:
        v = apply(s, v)
    return v


def preimage(chain: list[Stage], y: F) -> tuple[F, F] | None:
    """The closed interval of inputs to ``chain`` that it maps to ``y`` (ties at the
    edges aside, which the forward replay settles), or None when no input does."""
    lo, hi = y, y
    for s in reversed(chain):
        g_lo = math.ceil(lo / s.tick) * s.tick
        g_hi = math.floor(hi / s.tick) * s.tick
        if g_lo > g_hi:
            return None
        # round_to(u) lands on [g_lo, g_hi] for u in [g_lo - t/2, g_hi + t/2]; u = f v - b
        lo = (g_lo - s.tick / 2 + s.b) / s.f
        hi = (g_hi + s.tick / 2 + s.b) / s.f
    return lo, hi


def stage_window(chain: list[Stage], tick: F, y: F) -> tuple[F, F] | None:
    """The interval f x raw - b must fall in for a new innermost stage on ``tick``,
    under the outer ``chain``, to land on Kite's close ``y``."""
    window = preimage(chain, y)
    if window is None:
        return None
    g_lo = math.ceil(window[0] / tick) * tick
    g_hi = math.floor(window[1] / tick) * tick
    if g_lo > g_hi:
        return None
    return g_lo - tick / 2, g_hi + tick / 2


def read_tick(closes: list[F]) -> F:
    """The coarsest tick on the ladder every non-zero close sits on."""
    nz = [c for c in closes if c]
    for t in reversed(LADDER):
        if all(c % t == 0 for c in nz):
            return t
    return F(1, 1000)
