"""``pullback_v1``, family ``pullback``: buy a pullback to the 20-day average in an uptrend.

Screen, on the Adjusted Series as of the Run: the close is above the 200-day
simple average, that average is above its value 20 trading days earlier, and
the day's low reached the 20-day average while the close held at or above it.
Candidates are ranked by their 126-trading-day return, highest first, ties by
Symbol.

Plan: entry limit at the close; stop at entry minus k times ATR(14), the simple
average of the true range on the Adjusted Series as of the Run, so its scale is
the day's actual prices; target at entry plus r times the risk (entry minus
stop); time stop n trading days from the Fill. The SMA lengths are fixed; k, r
and n are the free parameters a Protocol's grid searches.

An Instrument without every bar the screen needs up to and including the Run's
day (one listed too recently, or that did not trade that day) is not screened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from tradeline.strategy import Candidate, MarketData, Member, Plan
from tradeline.strategy.indicators import atr, sma, trailing_return

FAST = 20
SLOW = 200
SLOPE_LOOKBACK = 20
RETURN_DAYS = 126
ATR_DAYS = 14
# The bars the screen reads: SLOW closes ending SLOPE_LOOKBACK days before the Run.
BARS_NEEDED = SLOW + SLOPE_LOOKBACK


@dataclass(frozen=True)
class PullbackV1:
    k: Decimal = Decimal("2")
    r: Decimal = Decimal("2")
    n: int = 10

    family = "pullback"
    name = "pullback_v1"
    ranking = "126-trading-day return descending, ties by Symbol ascending"

    def parameters(self) -> Mapping[str, str]:
        return {"k": str(self.k), "r": str(self.r), "n": str(self.n)}

    def screen(self, universe: Sequence[Member], data: MarketData, as_of: date) -> list[Candidate]:
        found: list[tuple[Decimal, str, str, dict[str, Decimal]]] = []
        for member in universe:
            bars = data.last_bars(member.instrument, BARS_NEEDED)
            if len(bars) < BARS_NEEDED or bars[-1].trade_date != as_of:
                continue
            today = bars[-1]
            assert today.close is not None and today.low is not None
            facts = {
                "close": today.close,
                "low": today.low,
                "sma20": sma(bars, FAST),
                "sma200": sma(bars, SLOW),
                "sma200_20_days_earlier": sma(bars[:-SLOPE_LOOKBACK], SLOW),
                "atr14": atr(bars, ATR_DAYS),
            }
            if (
                facts["close"] > facts["sma200"]
                and facts["sma200"] > facts["sma200_20_days_earlier"]
                and facts["low"] <= facts["sma20"] <= facts["close"]
            ):
                score = trailing_return(bars, RETURN_DAYS)
                found.append((score, member.symbol, member.instrument, facts))
        found.sort(key=lambda f: (-f[0], f[1]))
        return [
            Candidate(rank, instrument, symbol, score, facts)
            for rank, (score, symbol, instrument, facts) in enumerate(found, start=1)
        ]

    def plan(self, candidate: Candidate) -> Plan:
        entry = candidate.facts["close"]
        stop = entry - self.k * candidate.facts["atr14"]
        target = entry + self.r * (entry - stop)
        return Plan(
            rank=candidate.rank,
            instrument=candidate.instrument,
            symbol=candidate.symbol,
            entry=entry,
            stop=stop,
            target=target,
            time_stop_days=self.n,
            facts={"atr14": candidate.facts["atr14"]},
        )
