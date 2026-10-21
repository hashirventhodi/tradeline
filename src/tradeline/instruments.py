"""The link builder (ADR 0016): Instruments from the stored evidence, a pure function.

``build_instruments`` takes the evidence and returns the Instruments, with no
I/O; the same evidence in any order gives the same output. Reading the
evidence from the database and writing the output is
``tradeline.rebuild_instruments``'s job.

The evidence:

- the Raw Series as spans: each a run of consecutive end-of-day rows of one
  Symbol in one series under one ISIN;
- NSE's symbol-change records, as published;
- the Corporate Actions, of which the splits and bonuses (a consolidation is a
  split with a factor above one) can explain an ISIN change;
- NSE's listing dates, each for a (Symbol, ISIN) of the equity master.

A **stretch** is every span of one Symbol under one ISIN: the same Symbol with
the same ISIN is one Instrument whatever the gap between its spans, and an
Instrument spans series. Stretches are joined into an Instrument only by an
Instrument Link with a date and a source:

- ``corporate_action``: the same Symbol with a new ISIN where a split or bonus
  of that Symbol has its Ex-Date at the change, from the old ISIN's last row to
  the new ISIN's first, give or take a week (``EX_DATE_SLACK``). NSE's files
  do not switch ISIN on the Ex-Date exactly: on the #13 data the new ISIN
  first appears on the Ex-Date (200 changes), the next trading day (176;
  HDFCBANK 2019-09-19, BEL, EICHERMOT, TATASTEEL), two days after (10;
  IRCON 2020-04-03), or the day before (1; AHCL 2026-04-24). The nearest
  action that does not explain a change is months away;
- ``symbol_change``: an NSE record (old Symbol, new Symbol, effective date)
  joins the old Symbol's stretch that ended before the effective date to the
  new Symbol's stretch that started on or after it. Chains follow, since each
  record is one link. A record with no stretch on either side (a rename before
  the Raw Series starts) joins nothing.

Anything else splits the history. An ISIN change under one Symbol with no
split or bonus to explain it is never joined and is recorded as an anomaly.

An Instrument's key is ``SYMBOL@YYYY-MM-DD``: its first Symbol and the earliest
NSE listing date of any of its (Symbol, ISIN) pairs, or its first Raw Series
row where NSE gives none, or where the listing date already keys an earlier
Instrument of the same Symbol (an unexplained ISIN change splits a history,
and NSE's master lists the original listing date under today's ISIN). Which
date was used is recorded.

Extending the builder (later rules, refusals, manual links, candidates) means
adding to the evidence, adding a rule that proposes links (``_propose``), or
refusing a proposal before it is joined (``_join``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from tradeline.adjust import ADJUSTED_KINDS, CorporateAction
from tradeline.sources.nse import SymbolChange

LinkKind = Literal["corporate_action", "symbol_change"]
KeyFrom = Literal["listing_date", "first_row"]
AnomalyKind = Literal["unexplained_isin_change"]

# How far a split's or bonus's Ex-Date may fall outside the change from the old
# ISIN's last row to the new one's first and still explain it. NSE's files switch
# ISIN from two trading days after the Ex-Date to one before it (IRCON 2020,
# AHCL 2026); a week of calendar days covers that across a weekend and a holiday.
EX_DATE_SLACK = timedelta(days=7)


@dataclass(frozen=True, order=True)
class Span:
    """A run of consecutive Raw Series rows of one Symbol in one series under one ISIN."""

    symbol: str
    series: str
    isin: str | None
    first: date
    last: date


@dataclass(frozen=True)
class Listing:
    """NSE's listing date for a Symbol under an ISIN, from the equity master."""

    symbol: str
    isin: str | None
    listing_date: date


@dataclass(frozen=True)
class Evidence:
    spans: Sequence[Span]
    symbol_changes: Sequence[SymbolChange] = ()
    corporate_actions: Sequence[CorporateAction] = ()
    listings: Sequence[Listing] = ()


@dataclass(frozen=True)
class Interval:
    """A dated stretch of an Instrument's history under one Symbol, series, and ISIN."""

    symbol: str
    series: str
    isin: str | None
    first: date
    last: date


@dataclass(frozen=True)
class Link:
    """An Instrument Link: from ``on``, the history under ``from_symbol`` and ``from_isin``
    continues under ``to_symbol`` and ``to_isin``."""

    kind: LinkKind
    on: date
    from_symbol: str
    from_isin: str | None
    to_symbol: str
    to_isin: str | None
    # The evidence, readable: the NSE record or Corporate Action that made the link.
    source: str


@dataclass(frozen=True)
class Instrument:
    key: str
    first_symbol: str
    listed_on: date
    key_from: KeyFrom
    intervals: tuple[Interval, ...]
    links: tuple[Link, ...]


@dataclass(frozen=True)
class Anomaly:
    kind: AnomalyKind
    symbol: str
    on: date
    detail: str


@dataclass(frozen=True)
class InstrumentBuild:
    instruments: tuple[Instrument, ...]
    anomalies: tuple[Anomaly, ...]


def build_instruments(evidence: Evidence) -> InstrumentBuild:
    stretches = _stretches(evidence.spans)
    anomalies: list[Anomaly] = []
    proposals = _propose(stretches, evidence, anomalies)
    groups = _join(stretches, proposals)
    instruments = _keyed(groups, evidence.listings)
    return InstrumentBuild(
        instruments=tuple(sorted(instruments, key=lambda i: i.key)),
        anomalies=tuple(sorted(anomalies, key=lambda a: (a.on, a.symbol, a.kind, a.detail))),
    )


# --- Stretches --------------------------------------------------------------


@dataclass(frozen=True)
class _Stretch:
    """Every span of one Symbol under one ISIN."""

    symbol: str
    isin: str | None
    spans: tuple[Span, ...]

    @property
    def first(self) -> date:
        return self.spans[0].first

    @property
    def last(self) -> date:
        return max(s.last for s in self.spans)

    @property
    def id(self) -> tuple[str, str]:
        return (self.symbol, self.isin or "")


def _stretches(spans: Iterable[Span]) -> list[_Stretch]:
    by_pair: dict[tuple[str, str | None], list[Span]] = defaultdict(list)
    for s in set(spans):
        by_pair[(s.symbol, s.isin)].append(s)
    stretches = [
        _Stretch(symbol, isin, tuple(sorted(found, key=_span_order)))
        for (symbol, isin), found in by_pair.items()
    ]
    return sorted(stretches, key=lambda s: (s.first, s.id))


def _span_order(s: Span) -> tuple[date, date, str, str, str]:
    return (s.first, s.last, s.symbol, s.series, s.isin or "")


# --- Links ------------------------------------------------------------------


@dataclass(frozen=True)
class _Proposal:
    link: Link
    joins: tuple[_Stretch, _Stretch]


def _propose(
    stretches: list[_Stretch], evidence: Evidence, anomalies: list[Anomaly]
) -> list[_Proposal]:
    """Every link the rules propose, in a fixed order."""
    proposals = [
        *_isin_changes(stretches, evidence.corporate_actions, anomalies),
        *_symbol_changes(stretches, evidence.symbol_changes),
    ]
    return sorted(proposals, key=lambda p: (p.link.on, p.link.kind, p.joins[0].id, p.joins[1].id))


def _isin_changes(
    stretches: list[_Stretch],
    actions: Sequence[CorporateAction],
    anomalies: list[Anomaly],
) -> list[_Proposal]:
    """Successive ISINs of one Symbol: joined on a split's or bonus's Ex-Date, else an anomaly."""
    adjusting: dict[str, list[CorporateAction]] = defaultdict(list)
    for action in set(actions):
        if action.kind in ADJUSTED_KINDS:
            adjusting[action.symbol].append(action)
    proposals: list[_Proposal] = []
    for symbol, found in sorted(_by_symbol(stretches).items()):
        for old, new in zip(found, found[1:], strict=False):
            explaining = sorted(
                (
                    a
                    for a in adjusting.get(symbol, [])
                    if old.last - EX_DATE_SLACK <= a.ex_date <= new.first + EX_DATE_SLACK
                ),
                key=lambda a: (a.ex_date, a.series, a.subject),
            )
            if not explaining:
                anomalies.append(
                    Anomaly(
                        kind="unexplained_isin_change",
                        symbol=symbol,
                        on=new.first,
                        detail=(
                            f"{symbol} changed ISIN from {old.isin} to {new.isin}"
                            " with no split, bonus, or consolidation to explain it"
                        ),
                    )
                )
                continue
            action = explaining[0]
            link = Link(
                kind="corporate_action",
                on=action.ex_date,
                from_symbol=symbol,
                from_isin=old.isin,
                to_symbol=symbol,
                to_isin=new.isin,
                source=(
                    f"NSE corporate action {action.symbol} ex-date {action.ex_date.isoformat()}:"
                    f" {action.subject}"
                ),
            )
            proposals.append(_Proposal(link, (old, new)))
    return proposals


def _symbol_changes(stretches: list[_Stretch], records: Sequence[SymbolChange]) -> list[_Proposal]:
    by_symbol = _by_symbol(stretches)
    proposals: list[_Proposal] = []
    for record in sorted(set(records), key=_record_order):
        if record.old_symbol == record.new_symbol:
            continue
        effective = record.effective_date
        before = [s for s in by_symbol.get(record.old_symbol, []) if s.last < effective]
        after = [s for s in by_symbol.get(record.new_symbol, []) if s.first >= effective]
        if not before or not after:
            continue
        old = max(before, key=lambda s: (s.last, s.id))
        new = min(after, key=lambda s: (s.first, s.id))
        link = Link(
            kind="symbol_change",
            on=effective,
            from_symbol=old.symbol,
            from_isin=old.isin,
            to_symbol=new.symbol,
            to_isin=new.isin,
            source=(
                f"NSE symbol change {record.old_symbol} to {record.new_symbol}"
                f" effective {effective.isoformat()}"
            ),
        )
        proposals.append(_Proposal(link, (old, new)))
    return proposals


def _record_order(record: SymbolChange) -> tuple[date, str, str, str]:
    return (record.effective_date, record.old_symbol, record.new_symbol, record.company_name or "")


def _by_symbol(stretches: list[_Stretch]) -> dict[str, list[_Stretch]]:
    found: dict[str, list[_Stretch]] = defaultdict(list)
    for s in stretches:
        found[s.symbol].append(s)
    return found


# --- Joining ----------------------------------------------------------------


@dataclass
class _Group:
    stretches: list[_Stretch]
    links: list[Link] = field(default_factory=list)


def _join(stretches: list[_Stretch], proposals: list[_Proposal]) -> list[_Group]:
    """The stretches joined by each proposed link in turn."""
    parent = {s.id: s.id for s in stretches}

    def root(node: tuple[str, str]) -> tuple[str, str]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    accepted: list[_Proposal] = []
    for proposal in proposals:
        a, b = (root(s.id) for s in proposal.joins)
        if a != b:
            parent[max(a, b)] = min(a, b)
        accepted.append(proposal)
    groups: dict[tuple[str, str], _Group] = {}
    for s in stretches:
        groups.setdefault(root(s.id), _Group([])).stretches.append(s)
    for proposal in accepted:
        groups[root(proposal.joins[0].id)].links.append(proposal.link)
    return list(groups.values())


# --- Instruments ------------------------------------------------------------


def _keyed(groups: list[_Group], listings: Sequence[Listing]) -> list[Instrument]:
    """Each group as an Instrument with its key, earliest history first.

    A listing date already keying an Instrument of the same first Symbol that
    starts earlier belongs to that one (an ISIN change split the history, and
    the master lists the listing under today's ISIN), so the later Instrument
    is keyed by its first row.
    """
    ordered = sorted(groups, key=lambda g: min((s.first, s.id) for s in g.stretches))
    instruments: list[Instrument] = []
    keys: set[str] = set()
    for group in ordered:
        instrument = _instrument(group, listings)
        if instrument.key in keys:
            instrument = _instrument(group, ())
        if instrument.key in keys:
            raise ValueError(f"two Instruments would share the key {instrument.key}")
        keys.add(instrument.key)
        instruments.append(instrument)
    return instruments


def _instrument(group: _Group, listings: Sequence[Listing]) -> Instrument:
    spans = sorted((s for stretch in group.stretches for s in stretch.spans), key=_span_order)
    intervals = tuple(Interval(s.symbol, s.series, s.isin, s.first, s.last) for s in spans)
    pairs = {(s.symbol, s.isin) for s in group.stretches}
    listed = sorted(x.listing_date for x in listings if (x.symbol, x.isin) in pairs)
    first_symbol = spans[0].symbol
    key_from: KeyFrom = "listing_date" if listed else "first_row"
    listed_on = listed[0] if listed else spans[0].first
    return Instrument(
        key=f"{first_symbol}@{listed_on.isoformat()}",
        first_symbol=first_symbol,
        listed_on=listed_on,
        key_from=key_from,
        intervals=intervals,
        links=tuple(sorted(group.links, key=_link_order)),
    )


def _link_order(link: Link) -> tuple[date, str, str, str, str, str]:
    return (
        link.on,
        link.kind,
        link.from_symbol,
        link.from_isin or "",
        link.to_symbol,
        link.to_isin or "",
    )
