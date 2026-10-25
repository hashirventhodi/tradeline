"""``lowvol_v1``, family ``lowvol``: hold the 30 least volatile names of the Universe.

Eligible, as of a decision day: a member of the Universe of the month containing
it, with a close in the Adjusted Series on each of its last 253 trading days up to
and including that day, so 252 daily log returns with none missing. A day it did
not trade, a suspension, or too short a history makes it ineligible.

Ranked by the sample standard deviation of those 252 log returns, lowest first,
ties by Symbol. 30 are held, at equal weight (the engine splits the cash).

At a Rebalance a held name leaves if it has left the Universe, has become
ineligible, or ranks below 60th; every other held name stays, and the empty
slots are filled with the best-ranked names not held, in rank order, until 30
are held. The Initial Buy is the same decision with nothing held. Rebalances
are decided on the last trading day of March and of September.

Log returns and the buffer of twice the count are NSE's NIFTY100 Low Volatility
30 method; equal weight and the semiannual timing are this version's choices
(#89). NSE's eligible set is about 100 names and this one about 150 to 200, so
the buffer of 60 is NSE's rule kept literally at a different scale.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Context, Decimal

from tradeline.strategy import Candidate, MarketData, Member
from tradeline.strategy.portfolio import Rebalance

HELD = 30
# A held name ranked below this leaves at a Rebalance.
BUFFER = 60
RETURNS = 252
BARS = RETURNS + 1
REBALANCE_MONTHS = (3, 9)
# Every log, square root and quotient of the ranking, in one fixed precision.
EXACT = Context(prec=28)


@dataclass(frozen=True)
class LowvolV1:
    family = "lowvol"
    name = "lowvol_v1"
    ranking = (
        "sample standard deviation of the last 252 daily log returns ascending,"
        " ties by Symbol ascending"
    )
    rebalance_months = REBALANCE_MONTHS

    def parameters(self) -> Mapping[str, str]:
        # One rule, frozen: nothing for a grid to search.
        return {}

    def rebalance(
        self,
        holdings: Sequence[Member],
        universe: Sequence[Member],
        data: MarketData,
        as_of: date,
    ) -> Rebalance:
        ranked = self.rank(universe, data, as_of)
        rank_of = {c.instrument: c.rank for c in ranked}
        members = {m.instrument for m in universe}
        leavers = tuple(
            h.instrument
            for h in holdings
            if h.instrument not in members or rank_of.get(h.instrument, BUFFER + 1) > BUFFER
        )
        held = {h.instrument for h in holdings}
        slots = HELD - (len(held) - len(leavers))
        entrants = tuple(c for c in ranked if c.instrument not in held)[: max(slots, 0)]
        return Rebalance(leavers, entrants)

    def rank(self, universe: Sequence[Member], data: MarketData, as_of: date) -> list[Candidate]:
        """Every eligible member, ranked: its rank, its standard deviation as the score,
        and its close on the day."""
        found: list[tuple[Decimal, str, str, Decimal]] = []
        for member in universe:
            bars = data.last_bars(member.instrument, BARS)
            closes = [b.close for b in bars if b.close is not None and b.close > 0]
            if len(bars) < BARS or len(closes) < BARS or bars[-1].trade_date != as_of:
                continue
            found.append((_deviation(closes), member.symbol, member.instrument, closes[-1]))
        found.sort(key=lambda f: (f[0], f[1]))
        return [
            Candidate(rank, instrument, symbol, deviation, {"close": close})
            for rank, (deviation, symbol, instrument, close) in enumerate(found, start=1)
        ]


def _deviation(closes: Sequence[Decimal]) -> Decimal:
    """The sample standard deviation of the daily log returns of ``closes``."""
    returns = [EXACT.ln(EXACT.divide(b, a)) for a, b in zip(closes, closes[1:], strict=False)]
    mean = EXACT.divide(sum(returns, Decimal(0)), len(returns))
    squares = sum((EXACT.power(r - mean, 2) for r in returns), Decimal(0))
    return EXACT.sqrt(EXACT.divide(squares, len(returns) - 1))
