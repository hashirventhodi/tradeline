"""Kite's Adjustment: Kite's closes replayed from NSE's raw closes (ADR 0018, amended by #62).

Kite adjusts an Instrument's history one stage at a time. A **stage** sits on an
Ex-Date and rewrites every close before it, already adjusted and rounded by the
stages before it, as ``round(close * multiplier - subtracted)`` to the stage's own
tick, an exact half going to even, up or down by the stage's own tie rule. A
day's close today is NSE's raw close passed through every stage after it,
earliest first. ``replay`` finds those stages for one Instrument and series, and
the closes they reproduce exactly; nothing is a tolerance.

A stage's ratio or amount is **computed** from its Ex-Date's actions wherever a
rule gives it (``_Gap.forms``):

- a split, bonus or consolidation divides by its exact ratio, or by that ratio
  rounded to two decimals (Kite divides a 1:3 bonus by 1.33);
- a dividend with an Ex-Date before 2023-07-01 is never a stage;
- one from 2023-07-01 to 2024-09-30 is subtracted, or divides by P / (P - D)
  rounded to six decimals, P being NSE's close on the last day before the
  Ex-Date that NSE has one, a candle or not (inferred: it reproduces Kite's
  closes exactly, ITC's 343 among them, but Kite publishes no such rule);
- one from 2024-10-01 divides by that ratio only;
- from 2023-07-01 a dividend's Ex-Date can carry a stage that only rounds the
  history again, to a new tick.

An action whose purpose names a dividend is a dividend, however NSE files it.
For every other action (a rights issue, a demerger, the rest) no rule gives
Kite's ratio: it is read from Kite's closes (``_fitted_divisor``) and the stage
is **fitted**. A fitted stage never stands in for a computed one. It sits at its
own action's Ex-Date, and only where one Ex-Date of the gap could hold it: where
several could, nothing in Kite's closes says which action the ratio belongs to,
so none is placed and the ambiguity is reported (``AmbiguousFit``). Where an
older stretch is joined across several Ex-Dates (a gap) holding both kinds, each
split, bonus, consolidation and dividend there has its own computed stage or none
where the rules give none, and the fitted ratio carries only what those leave
(``_mixed_gap_layouts``). A fitted ratio within 0.1 percent of what an unstaged
action of its gap would give, or a product of them, absorbs it and is refused;
so is one within 0.1 percent of 1, which adjusts nothing.

The stretches between Ex-Dates are taken newest first. The newest is tried as
Kite's raw close, and each older one joins the **Kite Basis** before it while
the same stages, their ticks, tie rule and fitted ratio chosen again over the
whole basis, reproduce it; otherwise the Ex-Dates between it and the last
stretch that stood are tried as a new innermost group of stages, every tick
and tie rule, fewest closes off winning. A stretch **stands** when at most 5
percent of its closes (rounded up) are off the replay; one that does not
explains nothing and adds no stage. A Kite Basis of fewer than 20 compared bars
explains nothing.

Known limits (docs/research.md, #62):

- A group of stages found for one gap shares a tick among its computed stages and
  one tie rule among all of them; only its fitted stage has a tick of its own. ADR
  0018 gives each stage its own; a tick and tie rule per stage would multiply the
  search by 18 for every stage after the first, and no group of more than one stage
  stands in any replayed Instrument of the #59 run.
- A gap without a fittable action gets one computed stage at most, however many
  of its Ex-Dates hold a split or dividend (the prototype's rule, not measured).
- A gap with more than ``EXHAUSTIVE`` layouts is searched one Ex-Date's choice at a
  time, and reported (``BoundedSearch``).
"""

from __future__ import annotations

import bisect
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from fractions import Fraction
from typing import Literal, get_args

from tradeline.adjust import ADJUSTED_KINDS, CorporateAction

Ties = Literal["half_even", "half_down", "half_up"]
# Each tried in this order; an equal result goes to the earlier one.
TIES: tuple[Ties, ...] = get_args(Ties)
# The ticks Kite's closes show, finest first: 1, 5 and 10 paisa, 50 paisa, a rupee, five.
TICKS = tuple(Fraction(t) for t in ("0.01", "0.05", "0.10", "0.50", "1", "5"))
# A stretch stands with at most this share of its closes off the replay, rounded up.
MAX_OFF = Fraction(5, 100)
# A Kite Basis of fewer compared bars than this explains nothing.
MIN_BASIS_BARS = 20
# A fitted ratio this near 1, or what an unstaged action of its gap would give, is refused.
NEAR = Fraction(1, 1000)
DIVIDENDS_FROM = date(2023, 7, 1)
DIVIDED_ONLY_FROM = date(2024, 10, 1)
SIX_DECIMALS = 10**6
# Gaps with more layouts than this are searched one Ex-Date's choice at a time.
EXHAUSTIVE = 64
_AMOUNT = re.compile(r"R[se]\.?\s*-?\s*(\d+(?:\.\d+)?)")
_DIVIDEND_WORD = re.compile(r"div(?:idend|dend|ided|idned)", re.IGNORECASE)

Class = Literal["split", "dividend", "fitted"]
SplitForm = Literal["split", "split_2dp"]
# How a stage's ratio or amount was found: read from Kite's closes, or the computed rule.
Form = Literal[
    "fitted",
    "split",
    "split_2dp",
    "dividend_ratio",
    "dividend_subtracted",
    "split+dividend_ratio",
    "split+dividend_subtracted",
    "split_2dp+dividend_ratio",
    "split_2dp+dividend_subtracted",
    "reround",
]
# A dividend's two forms, divided and subtracted, alone or after each form of a split.
_DIVIDEND_FORMS: Mapping[SplitForm | None, tuple[Form, Form]] = {
    None: ("dividend_ratio", "dividend_subtracted"),
    "split": ("split+dividend_ratio", "split+dividend_subtracted"),
    "split_2dp": ("split_2dp+dividend_ratio", "split_2dp+dividend_subtracted"),
}


@dataclass(frozen=True)
class Close:
    """One compared day: NSE's raw close and Kite's close."""

    trade_date: date
    nse: Decimal
    kite: Decimal


@dataclass(frozen=True)
class Stage:
    """What Kite did to the history before ``ex_date``: each close becomes
    ``close * multiplier - subtracted`` rounded to ``tick`` by ``ties``."""

    ex_date: date
    form: Form
    multiplier: Fraction
    subtracted: Fraction
    tick: Fraction
    ties: Ties

    @property
    def fitted(self) -> bool:
        return self.form == "fitted"

    @property
    def divisor(self) -> Fraction:
        return 1 / self.multiplier

    def apply(self, value: Fraction) -> Fraction:
        return _round(value * self.multiplier - self.subtracted, self.tick, self.ties)


@dataclass(frozen=True)
class KiteBasis:
    """Consecutive stretches Kite adjusted alike."""

    # Each of its standing stretches as its first and last day, newest first.
    stretches: tuple[tuple[date, date], ...]
    # Compared bars in its standing stretches.
    bars: int
    # Its own stages, innermost (earliest Ex-Date) first; none for Kite's raw closes.
    stages: tuple[Stage, ...]
    # Every stage its closes pass through, its own and each newer basis's, innermost first.
    chain: tuple[Stage, ...]

    @property
    def start(self) -> date:
        return self.stretches[-1][0]

    @property
    def end(self) -> date:
        return self.stretches[0][1]

    def holds(self, day: date) -> bool:
        """Whether ``day`` is in one of its standing stretches."""
        return any(first <= day <= last for first, last in self.stretches)

    @property
    def explains(self) -> bool:
        """Whether its closes can be explained: it adjusts, over enough bars to show it."""
        return bool(self.stages) and self.bars >= MIN_BASIS_BARS

    @property
    def fitted(self) -> bool:
        """Whether any close it explains depends on a fitted stage."""
        return any(s.fitted for s in self.chain)


@dataclass(frozen=True)
class Replay:
    # Newest first.
    bases: tuple[KiteBasis, ...]
    # Each day whose close the replay reproduces exactly in a standing stretch of a
    # Kite Basis that ``explains``, with that basis.
    explained: Mapping[date, KiteBasis]
    # Each stretch that does not stand, as its first and last day.
    not_standing: tuple[tuple[date, date], ...] = ()
    ambiguous_fits: tuple[AmbiguousFit, ...] = ()
    bounded_searches: tuple[BoundedSearch, ...] = ()


@dataclass(frozen=True)
class AmbiguousFit:
    """A stretch a fitted stage would let stand at any of several of its gap's Ex-Dates.

    Nothing in Kite's closes says which action the ratio belongs to, so no fitted
    stage is placed: the stretch does not stand and its closes stay unexplained."""

    # Its first and last day.
    stretch: tuple[date, date]
    # Each Ex-Date a fitted stage at which would let it stand.
    ex_dates: tuple[date, ...]


@dataclass(frozen=True)
class BoundedSearch:
    """A gap with more layouts than ``EXHAUSTIVE``, searched one Ex-Date's choice at a
    time rather than every layout: where its stretch does not stand, a layout that would
    have let it may have been missed. Where it stands, its closes are reproduced all the
    same, though another layout might reproduce more."""

    # The stretch the gap's stages were sought for, as its first and last day.
    stretch: tuple[date, date]
    ex_dates: tuple[date, ...]
    layouts: int
    stands: bool


def replay(
    closes: Sequence[Close],
    actions: Iterable[CorporateAction],
    *,
    nse_closes: Mapping[date, Decimal],
) -> Replay:
    """Kite's Adjustment for one Instrument and series: its compared ``closes``, the
    Corporate Actions filed under any of its Symbols, and NSE's close on every day it
    has one in the series, a candle or not (a dividend's P)."""
    found = _distinct(actions)
    ex_dates = sorted({a.ex_date for a in found})
    grouped: dict[int, list[Close]] = {}
    for c in sorted(closes, key=lambda c: c.trade_date):
        grouped.setdefault(bisect.bisect_right(ex_dates, c.trade_date), []).append(c)
    previous = _previous_closes(nse_closes, ex_dates)
    return _Replayer(found, ex_dates, previous, grouped).run()


Point = tuple[Fraction, Fraction]


def _round(value: Fraction, tick: Fraction, ties: Ties) -> Fraction:
    q = value / tick
    n = math.floor(q)
    rest = q - n
    if rest > Fraction(1, 2) or (
        rest == Fraction(1, 2) and (ties == "half_up" or (ties == "half_even" and n % 2))
    ):
        n += 1
    return n * tick


def _through(chain: Sequence[Stage], value: Fraction) -> Fraction:
    for stage in chain:
        value = stage.apply(value)
    return value


def _off(chain: Sequence[Stage], points: Sequence[Point], cap: int | None = None) -> list[int]:
    """The points ``chain`` does not reproduce, or more than ``cap`` of them once there are."""
    off = []
    for i, (x, y) in enumerate(points):
        if _through(chain, x) != y:
            off.append(i)
            if cap is not None and len(off) > cap:
                break
    return off


def _cap(bars: int) -> int:
    return math.ceil(MAX_OFF * bars)


def _window(
    chain: Sequence[Stage], tick: Fraction, y: Fraction
) -> tuple[Fraction, Fraction] | None:
    """The interval a new innermost stage on ``tick`` must take ``x * m - b`` into for
    the outer ``chain`` to land on Kite's ``y``; ties at the edges aside, which the
    forward replay settles."""
    lo, hi = y, y
    for stage in reversed(chain):
        g_lo, g_hi = (
            math.ceil(lo / stage.tick) * stage.tick,
            math.floor(hi / stage.tick) * stage.tick,
        )
        if g_lo > g_hi:
            return None
        half = stage.tick / 2
        lo = (g_lo - half + stage.subtracted) / stage.multiplier
        hi = (g_hi + half + stage.subtracted) / stage.multiplier
    g_lo, g_hi = math.ceil(lo / tick) * tick, math.floor(hi / tick) * tick
    if g_lo > g_hi:
        return None
    return g_lo - tick / 2, g_hi + tick / 2


def _fitted_divisor(
    outer: Sequence[Stage], points: Sequence[Point], tick: Fraction
) -> Fraction | None:
    """The divisor, on the 6-decimal grid where one fits, that the most closes allow
    under the ``outer`` stages; None where none does or it is within ``NEAR`` of 1."""
    events: list[tuple[Fraction, int]] = []
    for x, y in points:
        window = _window(outer, tick, y)
        if window is None or window[0] <= 0:
            continue
        events.append((x / window[1], 0))  # opens: r small is x / r large
        events.append((x / window[0], 1))
    if not events:
        return None
    events.sort()
    best, count, at = -1, 0, 0
    for k, (_, kind) in enumerate(events):
        if kind == 0:
            count += 1
            if count > best:
                best, at = count, k
        else:
            count -= 1
    lo = events[at][0]
    hi = next(v for v, kind in events[at + 1 :] if kind == 1)
    divisor = next(
        (
            r
            for r in (
                Fraction(math.ceil(lo * SIX_DECIMALS), SIX_DECIMALS),
                Fraction(math.floor(hi * SIX_DECIMALS), SIX_DECIMALS),
                Fraction(round((lo + hi) / 2 * SIX_DECIMALS), SIX_DECIMALS),
            )
            if lo <= r <= hi
        ),
        (lo + hi) / 2,
    )
    return None if abs(divisor - 1) <= NEAR else divisor


def _class(action: CorporateAction) -> Class:
    if action.kind in ADJUSTED_KINDS:
        return "split"
    if action.kind == "dividend" or _DIVIDEND_WORD.search(action.subject):
        return "dividend"
    return "fitted"


def _dividend(subject: str) -> Fraction | None:
    """The amount a dividend's subject names: the sum of each, or the first where it
    says the others are included in it."""
    found = [Fraction(a.rstrip(".")) for a in _AMOUNT.findall(subject)]
    if not found:
        return None
    return found[0] if "includ" in subject.lower() else sum(found, Fraction(0))


def _distinct(actions: Iterable[CorporateAction]) -> list[CorporateAction]:
    """One action per (Ex-Date, kind, factor), or per subject without a factor: NSE can
    list one under each series or Symbol, and Kite adjusts for it once."""
    distinct: dict[tuple[object, ...], CorporateAction] = {}
    for a in sorted(actions, key=lambda a: a.ex_date):
        distinct.setdefault((a.ex_date, a.kind, a.factor if a.factor else a.subject), a)
    return list(distinct.values())


def _previous_closes(
    nse_closes: Mapping[date, Decimal], ex_dates: Sequence[date]
) -> dict[date, Fraction]:
    """NSE's close on the last day before each Ex-Date it has one: the trading day
    before, unless NSE has no close for the series that day."""
    days = sorted(nse_closes.items())
    found = {}
    for ex_date in ex_dates:
        k = bisect.bisect_left(days, (ex_date,))
        if k:
            found[ex_date] = Fraction(days[k - 1][1])
    return found


@dataclass(frozen=True)
class _Gap:
    """What one Ex-Date's actions give: their split ratio and dividend amount."""

    ex_date: date
    classes: frozenset[Class]
    # The product of its splits', bonuses' and consolidations' factors.
    split: Fraction
    # The dividends it names, under NSE's close the day before; 0 where none can be read.
    dividend: Fraction
    previous: Fraction | None

    @property
    def computed_kind(self) -> bool:
        return bool(self.classes & {"split", "dividend"})

    @property
    def has_split(self) -> bool:
        return "split" in self.classes and self.split != 1

    @property
    def two_decimals(self) -> Fraction | None:
        """Its split's divisor rounded to two decimals, where that changes it (1.33 for 4/3)."""
        two = Fraction(round(1 / self.split * 100), 100)
        return None if two == 1 / self.split else two

    @property
    def dividend_ratio(self) -> Fraction:
        """P / (P - D), P being NSE's close the day before."""
        p = _of(self.previous)
        return p / (p - self.dividend)

    def forms(self) -> list[_Form]:
        """Its computed stages, as (form, multiplier, subtracted)."""
        splits: list[tuple[SplitForm | None, Fraction]] = []
        if self.has_split:
            splits.append(("split", self.split))
            if (two := self.two_decimals) is not None:
                splits.append(("split_2dp", 1 / two))
        found: list[_Form] = [(_of(form), m, Fraction(0)) for form, m in splits]
        if self.dividend and self.ex_date >= DIVIDENDS_FROM:
            divisor = Fraction(round(self.dividend_ratio * SIX_DECIMALS), SIX_DECIMALS)
            for form, m in splits or [(None, Fraction(1))]:
                divided, subtracted = _DIVIDEND_FORMS[form]
                found.append((divided, m / divisor, Fraction(0)))
                if self.ex_date < DIVIDED_ONLY_FROM:
                    found.append((subtracted, m, self.dividend * m))
        if (
            "dividend" in self.classes
            and "split" not in self.classes
            and self.ex_date >= DIVIDENDS_FROM
        ):
            found.append(("reround", Fraction(1), Fraction(0)))
        return list(dict.fromkeys(found))

    def values(self) -> list[Fraction]:
        """The divisors its actions would give, a dividend of any era included: what a
        fitted ratio may not absorb."""
        found = []
        if self.split != 1:
            found.append(1 / self.split)
            if (two := self.two_decimals) is not None:
                found.append(two)
        if self.dividend:
            ratio = self.dividend_ratio
            found.append(ratio)
            if self.split != 1:
                found.append(ratio / self.split)
        return found


# One entry of a gap's layout, in Ex-Date order: a computed stage (its Ex-Date and
# form, multiplier, subtracted) or the fitted stage (its Ex-Date).
# A computed stage's (form, multiplier, subtracted).
_Form = tuple[Form, Fraction, Fraction]
_Computed = tuple[Literal["computed"], date, Form, Fraction, Fraction]
_Fit = tuple[Literal["fitted"], date]
_Entry = _Computed | _Fit


@dataclass
class _Option:
    layout: tuple[_Entry, ...]
    # The divisors of the gap's computed-kind Ex-Dates left without a stage.
    unstaged: tuple[tuple[Fraction, ...], ...]
    stages: list[Stage]
    off: list[int]
    key: tuple[object, ...]


@dataclass
class _Basis:
    layout: tuple[_Entry, ...]
    unstaged: tuple[tuple[Fraction, ...], ...]
    stretches: list[int] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.layout)


class _Replayer:
    def __init__(
        self,
        actions: list[CorporateAction],
        ex_dates: list[date],
        previous: dict[date, Fraction],
        grouped: dict[int, list[Close]],
    ) -> None:
        self.ex_dates = ex_dates
        self.grouped = grouped
        self.ambiguous: list[AmbiguousFit] = []
        self.bounded: list[BoundedSearch] = []
        self.points = {
            k: [(Fraction(c.nse), Fraction(c.kite)) for c in rows] for k, rows in grouped.items()
        }
        self.gaps = {}
        for ex_date in ex_dates:
            at = [a for a in actions if a.ex_date == ex_date]
            previous_close = previous.get(ex_date)
            split, dividend = Fraction(1), Fraction(0)
            for a in at:
                if _class(a) == "split" and a.factor:
                    split *= a.factor
                elif _class(a) == "dividend" and previous_close:
                    amount = _dividend(a.subject)
                    if amount is not None:
                        dividend += amount
            # Dividends of all NSE's close or more leave no ratio to divide by.
            if previous_close is not None and dividend >= previous_close:
                dividend = Fraction(0)
            self.gaps[ex_date] = _Gap(
                ex_date, frozenset(_class(a) for a in at), split, dividend, previous_close
            )

    def run(self) -> Replay:
        chain: list[Stage] = []  # innermost first
        bases: list[_Basis] = []
        placed: dict[int, int] = {}
        newer: int | None = None
        for k in sorted(self.grouped, reverse=True):
            points = self.points[k]
            cap = _cap(len(points))
            joined = False
            if bases:
                basis = bases[-1]
                members = [*basis.stretches, k]
                if not basis.layout:
                    joined = len(_off(chain, points, cap)) <= cap
                else:
                    group = self._rechoose(chain, basis, members)
                    if group is not None:
                        chain[: basis.size] = group
                        joined = True
            elif len(_off([], points, cap)) <= cap:
                bases.append(_Basis(layout=(), unstaged=()))
                joined = True
            if not joined:
                ex_dates = self.ex_dates[k:newer]
                span = (self.grouped[k][0].trade_date, self.grouped[k][-1].trade_date)
                best = self._options(chain, points, ex_dates, cap, span) if ex_dates else None
                if best is not None:
                    chain[0:0] = best.stages
                    bases.append(_Basis(best.layout, best.unstaged))
                    joined = True
            if joined:
                bases[-1].stretches.append(k)
                placed[k] = len(bases) - 1
                newer = k
        return self._result(chain, bases, placed)

    def _result(self, chain: list[Stage], bases: list[_Basis], placed: dict[int, int]) -> Replay:
        found: list[KiteBasis] = []
        position = len(chain)
        for basis in bases:
            stages = tuple(chain[position - basis.size : position])
            position -= basis.size
            found.append(
                KiteBasis(
                    stretches=tuple(
                        (self.grouped[k][0].trade_date, self.grouped[k][-1].trade_date)
                        for k in basis.stretches
                    ),
                    bars=sum(len(self.grouped[k]) for k in basis.stretches),
                    stages=stages,
                    chain=tuple(chain[position:]),
                )
            )
        explained: dict[date, KiteBasis] = {}
        not_standing = []
        for k, rows in self.grouped.items():
            if k not in placed:
                not_standing.append((rows[0].trade_date, rows[-1].trade_date))
                continue
            kite_basis = found[placed[k]]
            if not kite_basis.explains:
                continue
            off = set(_off(kite_basis.chain, self.points[k]))
            explained.update((c.trade_date, kite_basis) for i, c in enumerate(rows) if i not in off)
        return Replay(
            tuple(found),
            explained,
            tuple(sorted(not_standing)),
            tuple(self.ambiguous),
            tuple(self.bounded),
        )

    def _options(
        self,
        outer: list[Stage],
        points: list[Point],
        ex_dates: list[date],
        cap: int,
        span: tuple[date, date],
    ) -> _Option | None:
        """The best new innermost group of stages for a gap, or None where none lets the
        stretch stand. A fitted stage is tried only where one Ex-Date of the gap could
        hold it; where several could and one would let the stretch stand, the ambiguity
        is recorded instead."""
        gaps = [self.gaps[e] for e in ex_dates]
        fittable = [g.ex_date for g in gaps if "fitted" in g.classes]
        best = self._best(outer, points, gaps, fittable if len(fittable) == 1 else [], cap, span)
        if best is None and len(fittable) > 1:
            standing = tuple(
                e
                for e in fittable
                if self._best(outer, points, gaps, [e], cap, fitted_only=True) is not None
            )
            if standing:
                self.ambiguous.append(AmbiguousFit(span, standing))
        return best

    def _best(
        self,
        outer: list[Stage],
        points: list[Point],
        gaps: list[_Gap],
        fit_at: list[date],
        cap: int,
        span: tuple[date, date] | None = None,
        *,
        fitted_only: bool = False,
    ) -> _Option | None:
        """The best group for a gap with a fitted stage at no Ex-Date or one of
        ``fit_at``, or ``fitted_only`` at one of them; a bounded search is recorded only
        for the ``span`` sought."""
        fittable = any("fitted" in g.classes for g in gaps)
        if fittable and any(g.computed_kind for g in gaps):
            options = self._mixed_gap_layouts(
                outer, points, gaps, fit_at, cap, span, fitted_only=fitted_only
            )
        else:
            layouts: list[tuple[_Entry, ...]] = [
                (("computed", g.ex_date, *form),)
                for g in gaps
                for form in g.forms()
                if not fitted_only
            ]
            layouts += [(("fitted", e),) for e in fit_at]
            options = [o for layout in layouts for o in self._score(layout, (), outer, points, cap)]
        return min(options, key=lambda o: o.key, default=None)

    def _mixed_gap_layouts(
        self,
        outer: list[Stage],
        points: list[Point],
        gaps: list[_Gap],
        fit_at: list[date],
        cap: int,
        span: tuple[date, date] | None,
        *,
        fitted_only: bool = False,
    ) -> list[_Option]:
        """Every layout of a gap holding both kinds: for each computed-kind Ex-Date one of
        its computed stages, or none where the rules allow none (a split always has one),
        and no fitted stage or one at an Ex-Date of ``fit_at``, in Ex-Date order."""
        computed = [g for g in gaps if g.computed_kind]
        choices: list[list[tuple[_Gap, _Form | None]]] = []
        for g in computed:
            forms = g.forms()
            must = any(form.startswith("split") for form, _, _ in forms)
            choices.append([(g, f) for f in ([*forms] if must else [None, *forms])])

        def score(
            combo: list[tuple[_Gap, _Form | None]],
            fit: date | None,
            quick: bool,
        ) -> list[_Option]:
            layout: list[_Entry] = [("computed", g.ex_date, *f) for g, f in combo if f is not None]
            if fit is not None:
                layout.append(("fitted", fit))
            layout.sort(key=lambda e: (e[1], e[0] == "fitted"))
            if not layout:
                return []
            unstaged = tuple(tuple(g.values()) for g, f in combo if f is None and g.values())
            return self._score(tuple(layout), unstaged, outer, points, cap, quick=quick)

        combos_count = math.prod(len(c) for c in choices)
        found: list[_Option] = []
        fits: list[date | None] = [*fit_at] if fitted_only else [None, *fit_at]
        for fit in fits:
            if combos_count <= EXHAUSTIVE:
                combos: list[list[tuple[_Gap, _Form | None]]] = [[]]
                for c in choices:
                    combos = [[*combo, x] for combo in combos for x in c]
                for combo in combos:
                    found += score(combo, fit, quick=False)
                continue
            # Every optional dividend without a stage, splits exact; then each Ex-Date's
            # choice improved in turn, twice, on one tick and ties to even.
            combo = [c[0] for c in choices]
            best = min(score(combo, fit, quick=True), key=lambda o: o.key, default=None)
            for _ in range(2):
                for j, c in enumerate(choices):
                    for x in c:
                        trial = [*combo[:j], x, *combo[j + 1 :]]
                        got = min(score(trial, fit, quick=True), key=lambda o: o.key, default=None)
                        if got is not None and (best is None or got.key < best.key):
                            best, combo = got, trial
            found += score(combo, fit, quick=False)
        if combos_count > EXHAUSTIVE and span is not None:
            ex_dates = tuple(g.ex_date for g in gaps)
            self.bounded.append(BoundedSearch(span, ex_dates, combos_count, stands=bool(found)))
        return found

    def _score(
        self,
        layout: tuple[_Entry, ...],
        unstaged: tuple[tuple[Fraction, ...], ...],
        outer: list[Stage],
        points: list[Point],
        cap: int,
        *,
        quick: bool = False,
    ) -> list[_Option]:
        """The layout on every tick and tie rule, each leaving at most ``cap`` closes off:
        fewest off first, then without a fitted stage, fewer stages, the coarsest ticks."""
        fitted = any(e[0] == "fitted" for e in layout)
        found = []
        for computed_tick, fitted_tick, ties in _configurations(layout, quick=quick):
            stages = _stages(layout, outer, points, unstaged, computed_tick, fitted_tick, ties)
            if stages is None:
                continue
            off = _off([*stages, *outer], points, cap)
            if len(off) > cap:
                continue
            key = (len(off), fitted, len(layout), -computed_tick, -fitted_tick, TIES.index(ties))
            found.append(_Option(layout, unstaged, stages, off, key))
        return found

    def _rechoose(
        self, chain: list[Stage], basis: _Basis, members: list[int]
    ) -> list[Stage] | None:
        """The basis's stages with their ticks, tie rule and fitted ratio chosen again over
        every member stretch, its layout kept; None unless every member stands."""
        outer = chain[basis.size :]
        own = [p for k in members for p in self.points[k]]
        best: tuple[tuple[object, ...], list[Stage]] | None = None
        for computed_tick, fitted_tick, ties in _configurations(basis.layout):
            stages = _stages(
                basis.layout, outer, own, basis.unstaged, computed_tick, fitted_tick, ties
            )
            if stages is None:
                continue
            trial = [*stages, *outer]
            offs = [len(_off(trial, self.points[k], _cap(len(self.points[k])))) for k in members]
            if all(n <= _cap(len(self.points[k])) for n, k in zip(offs, members, strict=True)):
                key = (sum(offs), -computed_tick, -fitted_tick, TIES.index(ties))
                if best is None or key < best[0]:
                    best = (key, stages)
        return None if best is None else best[1]


def _configurations(
    layout: tuple[_Entry, ...], *, quick: bool = False
) -> list[tuple[Fraction, Fraction, Ties]]:
    """(computed stages' tick, fitted stage's tick, ties) to try: every tick for each kind
    the layout has, each with every tie rule; ``quick``, one tick for both and ties to even."""
    has_computed = any(e[0] == "computed" for e in layout)
    has_fitted = any(e[0] == "fitted" for e in layout)
    one = Fraction(1)
    if quick:
        return [(t if has_computed else one, t if has_fitted else one, "half_even") for t in TICKS]
    return [
        (tc, tf, ties)
        for tc in (TICKS if has_computed else (one,))
        for tf in (TICKS if has_fitted else (one,))
        for ties in TIES
    ]


def _stages(
    layout: tuple[_Entry, ...],
    outer: list[Stage],
    points: list[Point],
    unstaged: tuple[tuple[Fraction, ...], ...],
    computed_tick: Fraction,
    fitted_tick: Fraction,
    ties: Ties,
) -> list[Stage] | None:
    """A layout's stages, innermost first, its fitted divisor read with the computed
    stages in place; None where none can be read or it absorbs an unstaged action."""
    stages: list[Stage | None] = []
    fit_at = None
    for k, entry in enumerate(layout):
        if entry[0] == "computed":
            _, ex_date, form, multiplier, subtracted = entry
            stages.append(Stage(ex_date, form, multiplier, subtracted, computed_tick, ties))
        else:
            fit_at = k
            stages.append(None)
    if fit_at is None:
        return [_of(s) for s in stages]
    inner = [_of(s) for s in stages[:fit_at]]
    outer_group = [*[_of(s) for s in stages[fit_at + 1 :]], *outer]
    own = [(_through(inner, x), y) for x, y in points]
    divisor = _fitted_divisor(outer_group, own, fitted_tick)
    if divisor is None or _absorbs(divisor, unstaged):
        return None
    stages[fit_at] = Stage(layout[fit_at][1], "fitted", 1 / divisor, Fraction(0), fitted_tick, ties)
    return [_of(s) for s in stages]


def _absorbs(divisor: Fraction, unstaged: tuple[tuple[Fraction, ...], ...]) -> bool:
    """Whether ``divisor`` is within ``NEAR`` of a value one unstaged Ex-Date would give,
    or a product of one value from each of several."""
    products = [Fraction(1)]
    for values in unstaged:
        products += [p * v for p in products for v in values]
    return any(abs(divisor / p - 1) <= NEAR for p in products[1:])


def _of[T](value: T | None) -> T:
    if value is None:
        raise ValueError("a value was expected")
    return value
