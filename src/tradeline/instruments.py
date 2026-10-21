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
- NSE's listing dates, each for a (Symbol, ISIN) of the equity master;
- the manual links, each citing an official source.

A **stretch** is every span of one Symbol under one ISIN: the same Symbol with
the same ISIN is one Instrument whatever the gap between its spans, and an
Instrument spans series. Stretches are joined into an Instrument only by an
Instrument Link with a date and a source:

- ``corporate_action``: the same Symbol with a new ISIN where a split or bonus
  of that Symbol (or filed under another Symbol with the old or new ISIN, as
  NSE files past actions under the Symbol of today) has its Ex-Date at the
  change: within a week (``EX_DATE_SLACK``) of both the old ISIN's last row
  and the new ISIN's first, so an action in a long gap between the two ISINs
  explains nothing. NSE's files
  do not switch ISIN on the Ex-Date exactly: on the #13 data the new ISIN
  first appears on the Ex-Date (200 changes), the next trading day (176;
  HDFCBANK 2019-09-19, BEL, EICHERMOT, TATASTEEL), two days after (10;
  IRCON 2020-04-03), or the day before (1; AHCL 2026-04-24). The nearest
  action that does not explain a change is months away;
- ``symbol_change``: an NSE record (old Symbol, new Symbol, effective date)
  joins the old Symbol's latest stretch that started before the effective date
  to the new Symbol's earliest stretch live on or after it. Chains follow, since each
  record is one link. A record with no stretch on either side (a rename before
  the Raw Series starts) joins nothing;
- ``manual``: a reviewed row of the checked-in manual-links file
  (``tradeline.manual_links``), for a rename NSE's list omits (RUCHI to
  PATANJALI). It joins by the same rule as an NSE record, and its source
  carries the row's citation. A row naming one Symbol on both sides joins
  that Symbol's ISIN change where no split or bonus does, if its effective
  date is the new ISIN's first day or falls in the gap before it (YESBANK
  2020-03-16, the depositories applying its reconstruction scheme's lock-in).

Anything else splits the history. An ISIN change under one Symbol with no
split, bonus, or manual link to explain it is never joined and is recorded as
an anomaly.

A proposed link, from any source, is refused when it would give one
Instrument two live Symbols on a day (a span of one overlapping a span of the
other); the refusal is recorded as a ``conflicting_link`` anomaly. So a rename
record whose old Symbol keeps trading after its effective date joins nothing,
and a merger never joins: the absorbed and the surviving company traded side
by side, whether the absorbed company's ISIN ends (MINDTREE into LTI) or the
surviving company carries on (HDFC into HDFCBANK).

A new Symbol that takes up an ISIN an earlier Symbol ended under, with no
NSE record or manual link for the pair, is not joined either: it is a **Link
Candidate**, for the Operator to cite in a manual link or leave split (ADR 0016).

An Instrument's key is ``SYMBOL@YYYY-MM-DD``: its first Symbol and the earliest
NSE listing date of any of its (Symbol, ISIN) pairs, or its first Raw Series
row where NSE gives none, or where the listing date already keys an earlier
Instrument of the same Symbol (an unexplained ISIN change splits a history,
and NSE's master lists the original listing date under today's ISIN). Which
date was used is recorded.

Extending the builder (later rules and refusals) means adding to
the evidence, adding a rule that proposes links (``_propose``), or refusing a
proposal before it is joined (``_join``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Literal

from tradeline.adjust import ADJUSTED_KINDS, CorporateAction
from tradeline.sources.nse import SymbolChange

LinkKind = Literal["corporate_action", "symbol_change", "manual"]
KeyFrom = Literal["listing_date", "first_row"]
AnomalyKind = Literal["unexplained_isin_change", "conflicting_link"]

# NSE's equity listing series, the Raw Series an Instrument is built from: EQ; BE
# and BZ, trade-for-trade; SM and ST, SME. An Instrument spans moves between them.
LISTING_SERIES = ("BE", "BZ", "EQ", "SM", "ST")

# How far a split's or bonus's Ex-Date may fall from the old ISIN's last row, and
# from the new one's first, and still explain the change. NSE's files switch
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
class ManualLink:
    """A reviewed rename NSE's symbol-change list omits: from ``effective_date`` the old
    Symbol trades under the new one, as the official source ``citation`` says."""

    old_symbol: str
    new_symbol: str
    effective_date: date
    # An official URL or circular number.
    citation: str


@dataclass(frozen=True)
class Evidence:
    spans: Sequence[Span]
    symbol_changes: Sequence[SymbolChange] = ()
    corporate_actions: Sequence[CorporateAction] = ()
    listings: Sequence[Listing] = ()
    manual_links: Sequence[ManualLink] = ()


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
class LinkCandidate:
    """A new Symbol under an ISIN an earlier Symbol had, with no record joining them:
    a rename NSE's list may omit, for the Operator to cite in a manual link or leave."""

    old_symbol: str
    new_symbol: str
    isin: str
    # The old Symbol's last day under the ISIN and the new Symbol's first.
    old_last: date
    new_first: date


@dataclass(frozen=True)
class InstrumentBuild:
    instruments: tuple[Instrument, ...]
    anomalies: tuple[Anomaly, ...]
    link_candidates: tuple[LinkCandidate, ...]


def build_instruments(evidence: Evidence) -> InstrumentBuild:
    stretches = _stretches(evidence.spans)
    anomalies: list[Anomaly] = []
    proposals = _propose(stretches, evidence, anomalies)
    groups = _join(stretches, proposals, anomalies)
    instruments = _keyed(groups, evidence.listings)
    recorded = {(r.old_symbol, r.new_symbol) for r in evidence.symbol_changes} | {
        (m.old_symbol, m.new_symbol) for m in evidence.manual_links
    }
    candidates = _link_candidates(stretches, groups, recorded)
    return InstrumentBuild(
        instruments=tuple(sorted(instruments, key=lambda i: i.key)),
        anomalies=tuple(sorted(anomalies, key=lambda a: (a.on, a.symbol, a.kind, a.detail))),
        link_candidates=tuple(
            sorted(candidates, key=lambda c: (c.new_first, c.new_symbol, c.old_symbol, c.isin))
        ),
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
        *_isin_changes(stretches, evidence.corporate_actions, evidence.manual_links, anomalies),
        *_renames(stretches, evidence.symbol_changes, evidence.manual_links),
    ]
    return sorted(proposals, key=lambda p: (p.link.on, p.link.kind, p.joins[0].id, p.joins[1].id))


def _isin_changes(
    stretches: list[_Stretch],
    actions: Sequence[CorporateAction],
    manual_links: Sequence[ManualLink],
    anomalies: list[Anomaly],
) -> list[_Proposal]:
    """Successive ISINs of one Symbol: joined on a split's or bonus's Ex-Date, else by a
    manual link under the Symbol dated at the change, else an anomaly.

    The action is the Symbol's, or one filed under another Symbol with the old
    or the new ISIN: NSE files an Instrument's past actions under its Symbol of
    today (Gujarat Gas's 2019 split, under GUJENERGY since its 2026 rename).
    """
    by_symbol: dict[str, set[CorporateAction]] = defaultdict(set)
    by_isin: dict[str, set[CorporateAction]] = defaultdict(set)
    for action in set(actions):
        if action.kind in ADJUSTED_KINDS:
            by_symbol[action.symbol].add(action)
            if action.isin is not None:
                by_isin[action.isin].add(action)
    manual_by_symbol: dict[str, set[ManualLink]] = defaultdict(set)
    for m in manual_links:
        if m.old_symbol == m.new_symbol:
            manual_by_symbol[m.old_symbol].add(m)
    proposals: list[_Proposal] = []
    for symbol, found in sorted(_by_symbol(stretches).items()):
        for old, new in zip(found, found[1:], strict=False):
            filed = by_symbol.get(symbol, set()).union(
                *(by_isin.get(isin, set()) for isin in (old.isin, new.isin) if isin is not None)
            )
            explaining = sorted(
                (a for a in filed if _at_change(a.ex_date, old.last, new.first)),
                key=lambda a: (a.ex_date, a.symbol != symbol, a.symbol, a.series, a.subject),
            )
            if not explaining:
                manual = sorted(
                    (
                        m
                        for m in manual_by_symbol.get(symbol, set())
                        if old.last < m.effective_date <= new.first
                    ),
                    key=_manual_order,
                )
                if manual:
                    link = _manual_link(manual[0], from_isin=old.isin, to_isin=new.isin)
                    proposals.append(_Proposal(link, (old, new)))
                    continue
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


def _at_change(ex_date: date, old_last: date, new_first: date) -> bool:
    """Whether an Ex-Date is within ``EX_DATE_SLACK`` of both the old ISIN's last row and the
    new one's first: the change happens around the Ex-Date, so a long gap between the two
    ISINs is explained by nothing in it."""
    return all(abs(ex_date - day) <= EX_DATE_SLACK for day in (old_last, new_first))


def _renames(
    stretches: list[_Stretch], records: Sequence[SymbolChange], rows: Sequence[ManualLink]
) -> list[_Proposal]:
    """Each NSE record and manual link, joined by the same rule (``_rename``); a manual
    link's source is its citation."""
    by_symbol = _by_symbol(stretches)
    renames: list[tuple[str, str, date, LinkKind, str]] = [
        (
            r.old_symbol,
            r.new_symbol,
            r.effective_date,
            "symbol_change",
            f"NSE symbol change {r.old_symbol} to {r.new_symbol}"
            f" effective {r.effective_date.isoformat()}",
        )
        for r in sorted(set(records), key=_record_order)
    ] + [
        (m.old_symbol, m.new_symbol, m.effective_date, "manual", _manual_source(m))
        for m in sorted(set(rows), key=_manual_order)
    ]
    proposals = (
        _rename(by_symbol, old, new, effective, kind=kind, source=source)
        for old, new, effective, kind, source in renames
    )
    return [p for p in proposals if p is not None]


def _rename(
    by_symbol: dict[str, list[_Stretch]],
    old_symbol: str,
    new_symbol: str,
    effective: date,
    *,
    kind: LinkKind,
    source: str,
) -> _Proposal | None:
    """The old Symbol's history from before ``effective`` joined to the new one's live on or
    after it; whether the two may be joined (neither live beside the other) is ``_join``'s
    to decide."""
    if old_symbol == new_symbol:
        return None
    before = [s for s in by_symbol.get(old_symbol, []) if s.first < effective]
    after = [s for s in by_symbol.get(new_symbol, []) if s.last >= effective]
    if not before or not after:
        return None
    old = max(before, key=lambda s: (s.last, s.id))
    new = min(after, key=lambda s: (s.first, s.id))
    link = Link(
        kind=kind,
        on=effective,
        from_symbol=old.symbol,
        from_isin=old.isin,
        to_symbol=new.symbol,
        to_isin=new.isin,
        source=source,
    )
    return _Proposal(link, (old, new))


def _record_order(record: SymbolChange) -> tuple[date, str, str, str]:
    return (record.effective_date, record.old_symbol, record.new_symbol, record.company_name or "")


def _manual_order(row: ManualLink) -> tuple[date, str, str, str]:
    return (row.effective_date, row.old_symbol, row.new_symbol, row.citation)


def _manual_source(row: ManualLink) -> str:
    return (
        f"manual link {row.old_symbol} to {row.new_symbol}"
        f" effective {row.effective_date.isoformat()}: {row.citation}"
    )


def _manual_link(row: ManualLink, *, from_isin: str | None, to_isin: str | None) -> Link:
    """A manual link under one Symbol, across its ISIN change."""
    return Link(
        kind="manual",
        on=row.effective_date,
        from_symbol=row.old_symbol,
        from_isin=from_isin,
        to_symbol=row.new_symbol,
        to_isin=to_isin,
        source=_manual_source(row),
    )


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


def _join(
    stretches: list[_Stretch], proposals: list[_Proposal], anomalies: list[Anomaly]
) -> list[_Group]:
    """The stretches joined by each proposed link in turn, earliest first.

    A link that would give the joined Instrument two live Symbols on a day,
    whatever its source, is refused and recorded as a conflict: a Symbol's
    history and another's that overlap are two companies, or a record that is
    wrong, and either way not one Instrument.
    """
    parent = {s.id: s.id for s in stretches}
    spans: dict[tuple[str, str], list[Span]] = {s.id: list(s.spans) for s in stretches}

    def root(node: tuple[str, str]) -> tuple[str, str]:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    accepted: list[_Proposal] = []
    for proposal in proposals:
        a, b = (root(s.id) for s in proposal.joins)
        if a != b:
            clash = _two_live_symbols(spans[a], spans[b])
            if clash is not None:
                anomalies.append(_conflict(proposal.link, *clash))
                continue
            kept, gone = min(a, b), max(a, b)
            parent[gone] = kept
            spans[kept].extend(spans.pop(gone))
        accepted.append(proposal)
    groups: dict[tuple[str, str], _Group] = {}
    for s in stretches:
        groups.setdefault(root(s.id), _Group([])).stretches.append(s)
    for proposal in accepted:
        groups[root(proposal.joins[0].id)].links.append(proposal.link)
    return list(groups.values())


def _two_live_symbols(ours: list[Span], theirs: list[Span]) -> tuple[Span, Span] | None:
    """The earliest pair of spans, one from each side, of two Symbols live on one day."""
    clashes = [
        (max(x.first, y.first), _span_order(x), _span_order(y), x, y)
        for x in ours
        for y in theirs
        if x.symbol != y.symbol and x.first <= y.last and y.first <= x.last
    ]
    if not clashes:
        return None
    *_, x, y = min(clashes)
    return (x, y) if (x.first, x.symbol) <= (y.first, y.symbol) else (y, x)


def _conflict(link: Link, earlier: Span, later: Span) -> Anomaly:
    return Anomaly(
        kind="conflicting_link",
        symbol=link.from_symbol,
        on=link.on,
        detail=(
            f"refused {link.source}: {earlier.symbol}"
            f" ({earlier.first.isoformat()} to {earlier.last.isoformat()}) and {later.symbol}"
            f" ({later.first.isoformat()} to {later.last.isoformat()})"
            f" would both be live on {later.first.isoformat()}"
        ),
    )


# --- Link Candidates --------------------------------------------------------


def _link_candidates(
    stretches: list[_Stretch], groups: list[_Group], recorded: set[tuple[str, str]]
) -> list[LinkCandidate]:
    """Each Symbol's stretch that takes up an ISIN an earlier Symbol's ended under, unjoined.

    The earlier Symbol is the last one under the ISIN before the new Symbol's
    first row. A pair named by an NSE record or a manual link (``recorded``, as
    old and new Symbol) is never a candidate: the link joined them, or was
    refused and reported as a conflict.
    """
    group_of = {s.id: n for n, g in enumerate(groups) for s in g.stretches}
    found: list[LinkCandidate] = []
    for new in stretches:
        if new.isin is None:
            continue
        earlier = [
            s
            for s in stretches
            if s.isin == new.isin and s.symbol != new.symbol and s.last < new.first
        ]
        if not earlier:
            continue
        old = max(earlier, key=lambda s: (s.last, s.id))
        if group_of[old.id] == group_of[new.id] or (old.symbol, new.symbol) in recorded:
            continue
        found.append(LinkCandidate(old.symbol, new.symbol, new.isin, old.last, new.first))
    return found


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
