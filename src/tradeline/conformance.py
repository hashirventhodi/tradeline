"""The Conformance Check (#18): each stored Universe month is what the Universe Rule selects.

A month conforms when:

- every member is eligible under the rule;
- the members are ranked 1 to N by descending traded value, ties broken by
  Symbol, with N the rule's ``top_n`` or every eligible Instrument if fewer;
- each member's stored traded value equals the recomputed one exactly;
- every non-member with more traded value than the last member is ineligible
  for a reason the rule states. With fewer members than ``top_n``, that is
  every non-member.

The traded values are recomputed here, independently of the rule's ranking
query: one pass sums each Instrument's (or, under a rule that ranks Symbols,
each Symbol's) traded value in the rule's series over every month's window,
from NSE's rows and the stored Instrument intervals. The rule's own filters
then judge only the members and the non-members above the cut
(``universe.eligibility``), which is what makes 120 months a matter of
minutes. A non-member level with the last member is a tie at the cut, reported
whether or not it passes; it passes only if the rule's tie-break by Symbol
put the member first.

Nothing is written. A stored key a later rebuild retired is read as the key
now current, as ``universe.check_membership`` does.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

import psycopg

from tradeline import queries
from tradeline.config import UniverseRule
from tradeline.universe import Exclusion, ExclusionReason, eligibility


@dataclass(frozen=True)
class AboveTheCut:
    """A non-member with at least the last member's traded value, and why the rule left it out."""

    key: str
    traded_value: Decimal
    # None when the rule gives no reason: a failure, unless it lost a tie.
    excluded: Exclusion | None


@dataclass(frozen=True)
class MonthConformance:
    month: date
    members: int
    last_traded_value: Decimal | None
    above_the_cut: tuple[AboveTheCut, ...]
    # The non-members level with the last member.
    ties: tuple[str, ...]
    failures: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failures

    def reasons(self) -> Counter[ExclusionReason]:
        return Counter(a.excluded.reason for a in self.above_the_cut if a.excluded is not None)


@dataclass(frozen=True)
class ConformanceReport:
    rule_version: str
    months: tuple[MonthConformance, ...]

    @property
    def passed(self) -> bool:
        return all(m.passed for m in self.months)

    def reasons(self) -> Counter[ExclusionReason]:
        total: Counter[ExclusionReason] = Counter()
        for month in self.months:
            total.update(month.reasons())
        return total

    def summary(self) -> dict[str, Any]:
        return {
            "rule_version": self.rule_version,
            "months": len(self.months),
            "passed": self.passed,
            "failed_months": [m.month.isoformat() for m in self.months if not m.passed],
            "excluded_above_the_cut": dict(sorted(self.reasons().items())),
            "ties_at_the_cut": {m.month.isoformat(): list(m.ties) for m in self.months if m.ties},
        }

    def text(self) -> str:
        failed = [m for m in self.months if not m.passed]
        lines = [
            f"Conformance Check, rule {self.rule_version[:12]}: {len(self.months)} months,"
            f" {len(self.months) - len(failed)} conform, {len(failed)} fail",
            "Non-members above the last member, by reason: "
            + (
                ", ".join(f"{reason} {n}" for reason, n in sorted(self.reasons().items())) or "none"
            ),
        ]
        for month in self.months:
            if month.ties:
                lines.append(f"{month.month:%Y-%m} tie at the cut: {', '.join(month.ties)}")
        for month in failed:
            lines.append(f"{month.month:%Y-%m} fails:")
            lines.extend(f"  {failure}" for failure in month.failures)
        return "\n".join(lines) + "\n"


def check_conformance(
    conn: psycopg.Connection, rule: UniverseRule, *, months: Sequence[date]
) -> ConformanceReport:
    """The Conformance Check of each stored month under ``rule``'s version."""
    for month in months:
        if month.day != 1:
            raise ValueError(f"a Universe month is given by its first day, not {month}")
    recomputed = _window_traded_values(conn, rule, months)
    return ConformanceReport(
        rule_version=rule.version,
        months=tuple(_check_month(conn, rule, m, recomputed.get(m, {})) for m in months),
    )


def _check_month(
    conn: psycopg.Connection, rule: UniverseRule, month: date, traded: dict[str, Decimal]
) -> MonthConformance:
    stored = queries.universe_membership(conn, month, rule_version=rule.version, as_of=month)
    if not stored:
        return MonthConformance(month, 0, None, (), (), ("no Universe is stored for the month",))
    failures: list[str] = []
    keys = _member_keys(conn, rule, month, stored, failures)
    members = sorted(zip(stored, keys, strict=True), key=lambda pair: pair[0].rank)

    if [m.rank for m, _ in members] != list(range(1, len(members) + 1)):
        failures.append(f"ranks are {[m.rank for m, _ in members]}, not 1 to {len(members)}")
    if len(members) > rule.top_n:
        failures.append(f"{len(members)} members, more than the rule's {rule.top_n}")
    for member, key in members:
        value = traded.get(key)
        if value is None:
            failures.append(f"{member.symbol}: no {rule.series} traded value in the window")
        elif value != member.traded_value:
            failures.append(
                f"{member.symbol}: stored traded value {member.traded_value}, recomputed {value}"
            )
    for (higher, higher_key), (lower, lower_key) in zip(members, members[1:], strict=False):
        if (higher_key in traded and lower_key in traded) and (
            -traded[higher_key],
            higher.symbol,
        ) > (-traded[lower_key], lower.symbol):
            failures.append(
                f"{higher.symbol} ranks {higher.rank} above {lower.symbol},"
                " which has more traded value or wins the tie-break"
            )

    last, last_key = members[-1]
    last_value = traded.get(last_key)
    member_keys = {key for _, key in members}
    if len(members) < rule.top_n:
        above = [k for k in traded if k not in member_keys]
    elif last_value is None:
        above = []
    else:
        above = [k for k, v in traded.items() if k not in member_keys and v >= last_value]
    judged = eligibility(conn, rule, month=month, keys=sorted(member_keys | set(above)))

    for member, key in members:
        judgement = judged.get(key)
        if judgement is not None and judgement.excluded is not None:
            failures.append(f"{member.symbol} is a member but not eligible: {judgement.excluded}")

    above_the_cut: list[AboveTheCut] = []
    ties: list[str] = []
    for key in sorted(above, key=lambda k: (-traded[k], k)):
        judgement = judged.get(key)
        excluded = None if judgement is None else judgement.excluded
        symbol = key if judgement is None else judgement.symbol
        tied = len(members) == rule.top_n and traded[key] == last_value
        if tied:
            ties.append(symbol)
        above_the_cut.append(AboveTheCut(key, traded[key], excluded))
        if excluded is not None:
            continue
        if not tied:
            failures.append(
                f"{symbol} has {traded[key]} traded value, more than the last member"
                f" {last.symbol}, and the rule gives no reason to leave it out"
            )
        elif symbol < last.symbol:
            failures.append(
                f"{symbol} ties the last member {last.symbol} and wins the tie-break by Symbol,"
                " but is left out"
            )
    return MonthConformance(
        month=month,
        members=len(members),
        last_traded_value=last_value,
        above_the_cut=tuple(a for a in above_the_cut if a.excluded is not None),
        ties=tuple(ties),
        failures=tuple(failures),
    )


def _member_keys(
    conn: psycopg.Connection,
    rule: UniverseRule,
    month: date,
    stored: Sequence[queries.UniverseMember],
    failures: list[str],
) -> list[str]:
    """Each stored member's key: its Symbol, or under a rule that ranks Instruments, its
    Instrument, read through any key a later rebuild retired."""
    if rule.rank_by != "instrument":
        return [m.symbol for m in stored]
    current = {
        (o.symbol, o.stored_key): o.current_key
        for o in queries.obsolete_universe_keys(conn, month=month, rule_version=rule.version)
    }
    keys = []
    for m in stored:
        key = None if m.instrument is None else current.get((m.symbol, m.instrument), m.instrument)
        if key is None:
            failures.append(f"{m.symbol}: stored Instrument {m.instrument} names no Instrument")
            key = f"{m.symbol}@{m.instrument}"
        keys.append(key)
    return keys


def _window_traded_values(
    conn: psycopg.Connection, rule: UniverseRule, months: Sequence[date]
) -> dict[date, dict[str, Decimal]]:
    """Each month's window traded value in the rule's series, by Instrument or Symbol.

    The window is the rule's ``lookback_trading_days`` days with end-of-day
    files before the month. Rather than join every window to every row, the
    days are cut at every window's ends into segments, each key's traded value
    is summed once per segment, and each window adds up its segments.
    """
    if not months:
        return {}
    if rule.rank_by == "instrument":
        rows = (
            "SELECT i.instrument_key AS key, e.trade_date, e.traded_value"
            " FROM eod_bhavcopy e JOIN instrument_intervals i"
            " ON i.symbol = e.symbol AND i.series = e.series"
            " AND e.trade_date BETWEEN i.first_day AND i.last_day"
            " WHERE e.series = %(series)s"
        )
    else:
        rows = (
            "SELECT symbol AS key, trade_date, traded_value FROM eod_bhavcopy"
            " WHERE series = %(series)s"
        )
    result = conn.execute(
        "WITH calendar AS MATERIALIZED ("
        "  SELECT trade_date, row_number() OVER (ORDER BY trade_date) AS n"
        "  FROM (SELECT DISTINCT trade_date FROM eod_bhavcopy) d),"
        " months AS MATERIALIZED ("
        "  SELECT month, last_n - %(lookback)s + 1 AS first_n, last_n FROM ("
        "   SELECT m.month, (SELECT max(n) FROM calendar c WHERE c.trade_date < m.month) AS last_n"
        "   FROM unnest(%(months)s::date[]) m(month)) t),"
        " cuts AS (SELECT first_n AS n FROM months UNION SELECT last_n + 1 FROM months),"
        " segments AS (SELECT n AS first_n, lead(n) OVER (ORDER BY n) - 1 AS last_n FROM cuts),"
        " segment_days AS MATERIALIZED ("
        "  SELECT c.trade_date, s.first_n FROM calendar c"
        "  JOIN segments s ON c.n BETWEEN s.first_n AND s.last_n),"
        f" segment_sums AS MATERIALIZED ("
        f"  SELECT r.key, d.first_n, sum(r.traded_value) AS traded_value"
        f"  FROM ({rows}) r JOIN segment_days d USING (trade_date) GROUP BY 1, 2)"
        " SELECT m.month, s.key, sum(s.traded_value)"
        " FROM months m JOIN segment_sums s ON s.first_n BETWEEN m.first_n AND m.last_n"
        " GROUP BY 1, 2",
        {"months": list(months), "lookback": rule.lookback_trading_days, "series": rule.series},
    ).fetchall()
    traded: dict[date, dict[str, Decimal]] = {}
    for month, key, value in result:
        traded.setdefault(month, {})[key] = value
    return traded
