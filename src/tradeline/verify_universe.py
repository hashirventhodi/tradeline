"""``verify-universe``: the current Universe against NSE's current NIFTY 200 (ADR 0005).

A report for the Operator, not a test: the index list is never the source of
truth, but a large disagreement with it is a reason to look at the rule. The
report gives the overlap as a share of the NIFTY 200's constituents, then the
two difference lists. Each symbol carries its rank and its traded value over
the month's window under the rule: a Universe member its stored rank, a
constituent outside the Universe the rank the rule gives it below the top,
or, when the rule cannot rank it, why not. Last, it lists the Universe members
with no sector in force on the as-of date, since the Risk Engine's sector
Limit needs one for every member. A member's sector is found through its
Instrument (``queries.sector``), so a member renamed before or after the month
keeps the sector NSE recorded under its other Symbol; the report lists those
members with the Symbol each sector was recorded under, for the Operator to
see the join.

The Universe compared is the one stored for the month under the rule given
(the configured rule, from the command); a month not yet computed is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Decimal

import psycopg

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.sources import nse
from tradeline.transport import Transport
from tradeline.universe import Standing, standings

INDEX_NAME = "NIFTY 200"


@dataclass(frozen=True)
class ComparedSymbol:
    symbol: str
    rank: int | None
    traded_value: Decimal | None
    note: str


@dataclass(frozen=True)
class SectorUnderAnotherSymbol:
    rank: int
    symbol: str
    sector: str
    recorded_under: str


@dataclass(frozen=True)
class UniverseCheck:
    month: date
    rule_version: str
    as_of: date
    index_size: int
    overlap: int
    universe_only: tuple[ComparedSymbol, ...]
    index_only: tuple[ComparedSymbol, ...]
    without_sector: tuple[queries.UniverseMember, ...]
    # Members whose sector was recorded under another Symbol of their Instrument, by rank.
    sectors_under_other_symbols: tuple[SectorUnderAnotherSymbol, ...]

    @property
    def overlap_percent(self) -> Decimal:
        share = Decimal(100 * self.overlap) / Decimal(self.index_size)
        return share.quantize(Decimal("0.01"), rounding=ROUND_HALF_EVEN)

    def text(self) -> str:
        """The human-readable report."""
        lines = [
            f"Universe {self.month:%Y-%m} (rule {self.rule_version[:12]}) against {INDEX_NAME}"
            f" as of {self.as_of}",
            f"Overlap: {self.overlap} of {self.index_size} {INDEX_NAME} constituents"
            f" ({self.overlap_percent}%)",
            "",
            *_table(f"In the Universe, not in {INDEX_NAME}", self.universe_only),
            "",
            *_table(f"In {INDEX_NAME}, not in the Universe", self.index_only),
            "",
            f"Universe members without a sector: {len(self.without_sector)}",
        ]
        if self.without_sector:
            lines.append(f"{'rank':>7}  symbol")
            lines.extend(f"{m.rank:>7}  {m.symbol}" for m in self.without_sector)
        lines += [
            "",
            "Universe members with a sector recorded under another Symbol:"
            f" {len(self.sectors_under_other_symbols)}",
        ]
        if self.sectors_under_other_symbols:
            lines.append(f"{'rank':>7}  {'symbol':<12} {'recorded under':<15} sector")
            lines.extend(
                f"{s.rank:>7}  {s.symbol:<12} {s.recorded_under:<15} {s.sector}"
                for s in self.sectors_under_other_symbols
            )
        return "\n".join(lines) + "\n"

    def summary(self) -> dict[str, object]:
        """The machine-readable report."""
        return {
            "month": self.month.isoformat(),
            "rule_version": self.rule_version,
            "as_of": self.as_of.isoformat(),
            "index_size": self.index_size,
            "overlap": self.overlap,
            "overlap_percent": str(self.overlap_percent),
            "universe_only": [s.symbol for s in self.universe_only],
            "index_only": [s.symbol for s in self.index_only],
            "without_sector": [m.symbol for m in self.without_sector],
            "sectors_under_other_symbols": {
                s.symbol: s.recorded_under for s in self.sectors_under_other_symbols
            },
        }


def _table(title: str, rows: tuple[ComparedSymbol, ...]) -> list[str]:
    lines = [f"{title}: {len(rows)}"]
    if rows:
        lines.append(f"{'rank':>7}  {'symbol':<12} {'traded value':>20}  note")
    for row in rows:
        rank = "-" if row.rank is None else str(row.rank)
        value = "-" if row.traded_value is None else str(row.traded_value)
        lines.append(f"{rank:>7}  {row.symbol:<12} {value:>20}  {row.note}".rstrip())
    return lines


def verify_universe(
    conn: psycopg.Connection,
    transport: Transport,
    rule: UniverseRule,
    *,
    month: date,
    as_of: date,
) -> UniverseCheck:
    members = queries.universe_membership(conn, month, rule_version=rule.version, as_of=as_of)
    if not members:
        raise ValueError(
            f"no Universe is stored for {month:%Y-%m} under the configured rule; run"
            f" `tradeline universe --month {month:%Y-%m}` first"
        )
    index = nse.fetch_index_constituents(nse.NseSession(transport), nse.NIFTY_200_LIST)
    if not index:
        raise nse.NseError(f"NSE's {INDEX_NAME} list has no constituents")
    in_index = {row.symbol for row in index}
    in_universe = {m.symbol for m in members}
    ranked = {s.symbol: s for s in standings(conn, rule, month=month)}

    universe_only = tuple(
        ComparedSymbol(m.symbol, m.rank, m.traded_value, "")
        for m in members
        if m.symbol not in in_index
    )
    index_only = tuple(
        _outside(rule, ranked.get(symbol), symbol) for symbol in sorted(in_index - in_universe)
    )
    found = {s.symbol: s for s in queries.sectors(conn, as_of=as_of, symbols=sorted(in_universe))}
    return UniverseCheck(
        month=month,
        rule_version=rule.version,
        as_of=as_of,
        index_size=len(in_index),
        overlap=len(in_index & in_universe),
        universe_only=universe_only,
        index_only=tuple(sorted(index_only, key=_rank_order)),
        without_sector=tuple(m for m in members if m.symbol not in found),
        sectors_under_other_symbols=tuple(
            SectorUnderAnotherSymbol(m.rank, m.symbol, s.sector, s.recorded_under)
            for m in members
            if (s := found.get(m.symbol)) is not None and s.recorded_under != m.symbol
        ),
    )


def _outside(rule: UniverseRule, standing: Standing | None, symbol: str) -> ComparedSymbol:
    if standing is None:
        return ComparedSymbol(symbol, None, None, f"no {rule.series} rows in the window")
    if standing.rank is not None:
        return ComparedSymbol(
            symbol, standing.rank, standing.traded_value, f"ranked below the top {rule.top_n}"
        )
    return ComparedSymbol(symbol, None, standing.traded_value, standing.excluded or "")


def _rank_order(row: ComparedSymbol) -> tuple[int, int, str]:
    """Ranked symbols first, best rank first; then the unranked by symbol."""
    return (row.rank is None, row.rank or 0, row.symbol)
