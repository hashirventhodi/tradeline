"""The Relative Drawdown Halt's threshold, set before any backtest (ADR 0020).

The threshold is the worst Relative Drawdown of the NIFTY100 Low Volatility 30
Total Return Index against the NIFTY 500 Total Return Index from
``HISTORY_START`` to ``HISTORY_END``, rounded up to the next 5 points
(``metrics.round_up_to_points``). ``threshold_from_history`` computes it from
the history ``ingest index-history`` stored (``index_values``, the latest value
of each day), and states the source, the peak and trough days, and each index's
history with the hash of its (day, value) rows, which the Protocol freezes. The
two indices must close on the same days.

Only if neither NSE's archive host nor niftyindices.com serves that history is
``fallback_threshold`` used, and only when the Operator asks for it: the worst
calendar-year shortfall in NSE's June 2018 white paper, which compares the index
with the NIFTY 100, not the NIFTY 500, and over calendar years, not from a
running peak, so it is not the same measure and will tend to be too low. The
shortfall of a year is 1 - (1 + the index's return) / (1 + the NIFTY 100's),
the ratio's fall over that year.
"""

from __future__ import annotations

import hashlib
from datetime import date
from decimal import Decimal

import psycopg

from tradeline.metrics import (
    IndexHistory,
    RelativeDrawdownHalt,
    round_up_to_points,
    worst_relative_drawdown,
)
from tradeline.protocol import fixed_decimal
from tradeline.sources.niftyindices import SITE

STRATEGY_INDEX = "NIFTY100 LOW VOLATILITY 30"
BENCHMARK_INDEX = "NIFTY 500"
INDICES = (STRATEGY_INDEX, BENCHMARK_INDEX)
# 2005-04 to 2019-09: the index's base date to the month before the Stitched Curve.
HISTORY_START = date(2005, 4, 1)
HISTORY_END = date(2019, 9, 30)
ROUND_TO_POINTS = 5
# The worst measure is recorded to this; the threshold is rounded up from the exact one.
WORST_PLACES = Decimal("0.000001")

HISTORY_SOURCE = (
    f"{SITE.removeprefix('https://')} Total Return Index history, {STRATEGY_INDEX} against"
    f" {BENCHMARK_INDEX}, {HISTORY_START} to {HISTORY_END}"
)
BACKFILL_NOTE = (
    "History before the NIFTY100 Low Volatility 30's launch is NSE's backfill: acceptable for"
    " a risk threshold, never evidence of performance."
)

# NSE Indices, "NIFTY100 Low Volatility 30", white paper series #13, June 2018, Exhibit 4
# (calendar-year returns, 2005 from April 1, 2018 to May 31): 2007 is the worst year
# against the NIFTY 100, 29.4 percent against 57.5 percent.
WHITE_PAPER = (
    "archives.nseindia.com/content/indices/NIFTY100_Low_Vol30_White_paper_Jun2018.pdf, Exhibit 4"
)
WHITE_PAPER_YEAR = 2007
WHITE_PAPER_INDEX_RETURN = Decimal("0.294")
WHITE_PAPER_NIFTY100_RETURN = Decimal("0.575")
FALLBACK_NOTE = (
    "Fallback: niftyindices.com and NSE's archive host did not serve the index history. The"
    " worst calendar-year shortfall against the NIFTY 100, not the NIFTY 500, is not the same"
    " measure as a Relative Drawdown and will tend to be too low."
)


class NoIndexHistory(ValueError):
    """The index history the threshold is computed from is not stored."""


def index_history(conn: psycopg.Connection, index_name: str) -> dict[date, Decimal]:
    """The index's latest stored value of each day from ``HISTORY_START`` to ``HISTORY_END``."""
    rows = conn.execute(
        "SELECT DISTINCT ON (day) day, value FROM index_values"
        " WHERE index_name = %s AND day BETWEEN %s AND %s"
        " ORDER BY day, fetched_at DESC",
        (index_name, HISTORY_START, HISTORY_END),
    ).fetchall()
    return dict(rows)


def threshold_from_history(conn: psycopg.Connection) -> RelativeDrawdownHalt:
    """The threshold from the stored history; ``NoIndexHistory`` when an index has none."""
    histories = {name: index_history(conn, name) for name in INDICES}
    missing = [name for name, values in histories.items() if not values]
    if missing:
        raise NoIndexHistory(
            f"no {' or '.join(missing)} Total Return Index history is stored;"
            " run `tradeline ingest index-history`"
        )
    strategy, benchmark = histories[STRATEGY_INDEX], histories[BENCHMARK_INDEX]
    unmatched = sorted(set(strategy) ^ set(benchmark))
    if unmatched:
        raise ValueError(
            f"{len(unmatched)} days have a close of one index and not the other, the first"
            f" {unmatched[0]}; the Relative Drawdown needs both every day"
        )
    days = sorted(strategy)
    worst = worst_relative_drawdown(days, [strategy[d] for d in days], [benchmark[d] for d in days])
    return RelativeDrawdownHalt(
        threshold=round_up_to_points(worst.fall, ROUND_TO_POINTS),
        worst=worst.fall.quantize(WORST_PLACES),
        source=HISTORY_SOURCE,
        fallback=False,
        note=BACKFILL_NOTE,
        peak=worst.peak,
        trough=worst.trough,
        history=tuple(_history(name, values) for name, values in histories.items()),
    )


def relative_drawdown_threshold(
    conn: psycopg.Connection, *, fallback: bool
) -> RelativeDrawdownHalt:
    """The threshold a Protocol freezes: from the stored history, or, only when the Operator
    asks with ``fallback``, the white paper's, which is refused while any history is stored."""
    if not fallback:
        try:
            return threshold_from_history(conn)
        except NoIndexHistory as error:
            raise NoIndexHistory(
                f"{error}; only if neither niftyindices.com nor NSE's archive host serves it,"
                " freeze with --relative-drawdown-fallback"
            ) from error
    stored = [name for name in INDICES if index_history(conn, name)]
    if stored:
        raise ValueError(
            f"{' and '.join(stored)} history is stored; the white paper's fallback is only for"
            " when the index history cannot be served"
        )
    return fallback_threshold()


def fallback_threshold() -> RelativeDrawdownHalt:
    """The white paper's worst calendar-year shortfall, rounded up likewise."""
    shortfall = 1 - (1 + WHITE_PAPER_INDEX_RETURN) / (1 + WHITE_PAPER_NIFTY100_RETURN)
    return RelativeDrawdownHalt(
        threshold=round_up_to_points(shortfall, ROUND_TO_POINTS),
        worst=shortfall.quantize(WORST_PLACES),
        source=f"{WHITE_PAPER}, calendar year {WHITE_PAPER_YEAR}",
        fallback=True,
        note=FALLBACK_NOTE,
    )


def _history(name: str, values: dict[date, Decimal]) -> IndexHistory:
    digest = hashlib.sha256()
    for day in sorted(values):
        digest.update(f"{day.isoformat()},{fixed_decimal(values[day])}\n".encode())
    return IndexHistory(name, min(values), max(values), len(values), digest.hexdigest())
