"""The pure link builder: Instruments from the evidence, with no database.

The evidence is written out here as the builder receives it, with the real
Symbols, ISINs, and dates of NSE's records and end-of-day files (the #13 data):
ZOMATO's last row is 2025-04-08 and ETERNAL's first 2025-04-09, the effective
date of NSE's symbol-change record. HDFCBANK's old ISIN trades through its
split's Ex-Date (2019-09-19) and the new one from the next day.
"""

from datetime import date
from fractions import Fraction

from tradeline.adjust import CorporateAction
from tradeline.instruments import (
    Evidence,
    Instrument,
    Listing,
    ManualLink,
    Span,
    build_instruments,
)
from tradeline.sources.nse import SymbolChange


def span(symbol: str, isin: str, first: date, last: date, series: str = "EQ") -> Span:
    return Span(symbol=symbol, series=series, isin=isin, first=first, last=last)


def rename(old: str, new: str, effective: date) -> SymbolChange:
    return SymbolChange(company_name=None, old_symbol=old, new_symbol=new, effective_date=effective)


def named(instruments: tuple[Instrument, ...], symbol: str, day: date) -> Instrument:
    """The one Instrument with an interval under ``symbol`` covering ``day``."""
    found = [
        i
        for i in instruments
        if any(v.symbol == symbol and v.first <= day <= v.last for v in i.intervals)
    ]
    assert len(found) == 1, found
    return found[0]


ZOMATO_ISIN = "INE758T01015"


def test_a_rename_record_joins_the_old_and_new_symbol_at_its_effective_date() -> None:
    evidence = Evidence(
        spans=[
            span("ZOMATO", ZOMATO_ISIN, date(2021, 7, 23), date(2025, 4, 8)),
            span("ETERNAL", ZOMATO_ISIN, date(2025, 4, 9), date(2026, 10, 19)),
        ],
        symbol_changes=[rename("ZOMATO", "ETERNAL", date(2025, 4, 9))],
    )

    (instrument,) = build_instruments(evidence).instruments

    assert [(v.symbol, v.first, v.last) for v in instrument.intervals] == [
        ("ZOMATO", date(2021, 7, 23), date(2025, 4, 8)),
        ("ETERNAL", date(2025, 4, 9), date(2026, 10, 19)),
    ]
    (link,) = instrument.links
    assert (link.kind, link.on, link.from_symbol, link.to_symbol) == (
        "symbol_change",
        date(2025, 4, 9),
        "ZOMATO",
        "ETERNAL",
    )
    assert link.source == "NSE symbol change ZOMATO to ETERNAL effective 2025-04-09"


LTI_ISIN = "INE214T01019"


def test_a_chain_of_rename_records_is_one_instrument() -> None:
    evidence = Evidence(
        spans=[
            span("LTI", LTI_ISIN, date(2016, 7, 21), date(2022, 12, 2)),
            span("LTIM", LTI_ISIN, date(2022, 12, 5), date(2026, 2, 26)),
            span("LTM", LTI_ISIN, date(2026, 2, 27), date(2026, 10, 19)),
        ],
        symbol_changes=[
            rename("LTIM", "LTM", date(2026, 2, 27)),
            rename("LTI", "LTIM", date(2022, 12, 5)),
        ],
    )

    (instrument,) = build_instruments(evidence).instruments

    assert [v.symbol for v in instrument.intervals] == ["LTI", "LTIM", "LTM"]
    assert [(link.from_symbol, link.to_symbol, link.on) for link in instrument.links] == [
        ("LTI", "LTIM", date(2022, 12, 5)),
        ("LTIM", "LTM", date(2026, 2, 27)),
    ]


HDFCBANK_OLD, HDFCBANK_NEW = "INE040A01026", "INE040A01034"
HDFCBANK_SPLIT = "Face Value Split (Sub-Division) - From Rs 2 Per Share To Rs 1 Per Share"


def split(symbol: str, ex_date: date, subject: str) -> CorporateAction:
    return CorporateAction(
        symbol=symbol,
        series="EQ",
        ex_date=ex_date,
        subject=subject,
        kind="split",
        factor=Fraction(1, 2),
    )


def test_a_split_that_issues_a_new_isin_keeps_one_instrument() -> None:
    evidence = Evidence(
        spans=[
            span("HDFCBANK", HDFCBANK_OLD, date(2015, 1, 1), date(2019, 9, 19)),
            span("HDFCBANK", HDFCBANK_NEW, date(2019, 9, 20), date(2026, 10, 19)),
        ],
        corporate_actions=[split("HDFCBANK", date(2019, 9, 19), HDFCBANK_SPLIT)],
    )

    built = build_instruments(evidence)

    (instrument,) = built.instruments
    assert [(v.isin, v.first, v.last) for v in instrument.intervals] == [
        (HDFCBANK_OLD, date(2015, 1, 1), date(2019, 9, 19)),
        (HDFCBANK_NEW, date(2019, 9, 20), date(2026, 10, 19)),
    ]
    (link,) = instrument.links
    assert (link.kind, link.on, link.from_isin, link.to_isin) == (
        "corporate_action",
        date(2019, 9, 19),
        HDFCBANK_OLD,
        HDFCBANK_NEW,
    )
    assert link.source == f"NSE corporate action HDFCBANK ex-date 2019-09-19: {HDFCBANK_SPLIT}"
    assert built.anomalies == ()


GUJGAS_OLD, GUJGAS_NEW = "INE844O01022", "INE844O01030"
GUJGAS_SPLIT = "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"


def test_a_split_nse_now_files_under_the_symbol_of_today_explains_the_isin_change_before_it() -> (
    None
):
    # NSE lists Gujarat Gas's 2019 split under GUJENERGY, its Symbol since 2026,
    # with the ISIN it issued.
    refiled = CorporateAction(
        symbol="GUJENERGY",
        series="EQ",
        ex_date=date(2019, 1, 15),
        subject=GUJGAS_SPLIT,
        kind="split",
        factor=Fraction(1, 5),
        isin=GUJGAS_NEW,
    )
    evidence = Evidence(
        spans=[
            span("GUJGASLTD", GUJGAS_OLD, date(2015, 10, 1), date(2019, 1, 15)),
            span("GUJGASLTD", GUJGAS_NEW, date(2019, 1, 16), date(2026, 6, 30)),
            span("GUJENERGY", GUJGAS_NEW, date(2026, 7, 1), date(2026, 10, 19)),
        ],
        symbol_changes=[rename("GUJGASLTD", "GUJENERGY", date(2026, 7, 1))],
        corporate_actions=[refiled],
    )

    built = build_instruments(evidence)

    (instrument,) = built.instruments
    assert [(link.kind, link.from_isin, link.to_symbol) for link in instrument.links] == [
        ("corporate_action", GUJGAS_OLD, "GUJGASLTD"),
        ("symbol_change", GUJGAS_NEW, "GUJENERGY"),
    ]
    assert instrument.links[0].source == (
        f"NSE corporate action GUJENERGY ex-date 2019-01-15: {GUJGAS_SPLIT}"
    )
    assert built.anomalies == ()


def test_an_isin_change_no_corporate_action_explains_gives_two_instruments_and_an_anomaly() -> None:
    evidence = Evidence(
        spans=[
            span("HDFCBANK", HDFCBANK_OLD, date(2015, 1, 1), date(2019, 9, 19)),
            span("HDFCBANK", HDFCBANK_NEW, date(2019, 9, 20), date(2026, 10, 19)),
        ],
        # A split a year earlier explains nothing on 2019-09-20.
        corporate_actions=[split("HDFCBANK", date(2018, 9, 19), HDFCBANK_SPLIT)],
    )

    built = build_instruments(evidence)

    old = named(built.instruments, "HDFCBANK", date(2019, 9, 19))
    new = named(built.instruments, "HDFCBANK", date(2019, 9, 20))
    assert old != new
    assert (old.links, new.links) == ((), ())
    (anomaly,) = built.anomalies
    assert (anomaly.kind, anomaly.symbol, anomaly.on) == (
        "unexplained_isin_change",
        "HDFCBANK",
        date(2019, 9, 20),
    )
    assert anomaly.detail == (
        f"HDFCBANK changed ISIN from {HDFCBANK_OLD} to {HDFCBANK_NEW}"
        " with no split, bonus, or consolidation to explain it"
    )


def test_a_move_from_eq_to_be_and_back_stays_one_instrument() -> None:
    isin = "INE001A01036"
    evidence = Evidence(
        spans=[
            span("HFCL", isin, date(2018, 1, 1), date(2019, 3, 29), series="EQ"),
            span("HFCL", isin, date(2019, 4, 1), date(2019, 6, 28), series="BE"),
            span("HFCL", isin, date(2019, 7, 1), date(2026, 10, 19), series="EQ"),
        ],
    )

    (instrument,) = build_instruments(evidence).instruments

    assert [(v.series, v.first) for v in instrument.intervals] == [
        ("EQ", date(2018, 1, 1)),
        ("BE", date(2019, 4, 1)),
        ("EQ", date(2019, 7, 1)),
    ]
    assert instrument.links == ()


def test_a_key_is_the_first_symbol_and_nse_listing_date_of_any_of_its_symbols() -> None:
    evidence = Evidence(
        spans=[
            span("ZOMATO", ZOMATO_ISIN, date(2021, 7, 23), date(2025, 4, 8)),
            span("ETERNAL", ZOMATO_ISIN, date(2025, 4, 9), date(2026, 10, 19)),
        ],
        symbol_changes=[rename("ZOMATO", "ETERNAL", date(2025, 4, 9))],
        # The equity master lists only the Symbol of today.
        listings=[Listing("ETERNAL", ZOMATO_ISIN, date(2021, 7, 23))],
    )

    (instrument,) = build_instruments(evidence).instruments

    assert (instrument.key, instrument.first_symbol, instrument.listed_on) == (
        "ZOMATO@2021-07-23",
        "ZOMATO",
        date(2021, 7, 23),
    )
    assert instrument.key_from == "listing_date"


def test_without_a_listing_date_the_key_falls_back_to_the_first_raw_series_row() -> None:
    evidence = Evidence(
        spans=[span("HDFCBANK", HDFCBANK_OLD, date(2015, 1, 1), date(2019, 9, 19))],
        # A listing of another ISIN under the Symbol is not this Instrument's.
        listings=[Listing("HDFCBANK", HDFCBANK_NEW, date(1995, 11, 8))],
    )

    (instrument,) = build_instruments(evidence).instruments

    assert (instrument.key, instrument.listed_on, instrument.key_from) == (
        "HDFCBANK@2015-01-01",
        date(2015, 1, 1),
        "first_row",
    )


def test_the_same_evidence_in_any_order_gives_identical_output() -> None:
    spans = [
        span("LTI", LTI_ISIN, date(2016, 7, 21), date(2022, 12, 2)),
        span("LTIM", LTI_ISIN, date(2022, 12, 5), date(2026, 2, 26)),
        span("LTM", LTI_ISIN, date(2026, 2, 27), date(2026, 10, 19)),
        span("HDFCBANK", HDFCBANK_OLD, date(2015, 1, 1), date(2019, 9, 19)),
        span("HDFCBANK", HDFCBANK_NEW, date(2019, 9, 20), date(2026, 10, 19)),
        span("HFCL", "INE001A01036", date(2018, 1, 1), date(2019, 3, 29)),
        span("HFCL", "INE001A01036", date(2019, 4, 1), date(2019, 6, 28), series="BE"),
    ]
    changes = [rename("LTI", "LTIM", date(2022, 12, 5)), rename("LTIM", "LTM", date(2026, 2, 27))]
    listings = [
        Listing("LTM", LTI_ISIN, date(2016, 7, 21)),
        Listing("HDFCBANK", HDFCBANK_NEW, date(1995, 11, 8)),
    ]
    actions = [
        split("HDFCBANK", date(2019, 9, 19), HDFCBANK_SPLIT),
        split("HDFCBANK", date(2010, 7, 14), "Face Value Split From Rs 10 To Rs 2"),
    ]
    forward = Evidence(spans, changes, actions, listings)
    backward = Evidence(spans[::-1], changes[::-1], actions[::-1], listings[::-1])

    built = build_instruments(forward)

    assert built == build_instruments(forward)
    assert built == build_instruments(backward)
    assert [i.key for i in built.instruments] == [
        "HDFCBANK@1995-11-08",
        "HFCL@2018-01-01",
        "LTI@2016-07-21",
    ]


IRCON_OLD, IRCON_NEW = "INE962Y01013", "INE962Y01021"
IRCON_SPLIT = "Face Value Split (Sub-Division) - From Rs 10/- Per Share To Rs 2/- Per Share"


def test_a_new_isin_that_nse_files_a_few_trading_days_after_the_ex_date_is_still_joined() -> None:
    # IRCON's split went ex on Friday 3 April 2020; NSE's files carried the old
    # ISIN to the 7th (the 6th a holiday) and the new one from the 8th.
    evidence = Evidence(
        spans=[
            span("IRCON", IRCON_OLD, date(2018, 9, 28), date(2020, 4, 7)),
            span("IRCON", IRCON_NEW, date(2020, 4, 8), date(2026, 10, 19)),
        ],
        corporate_actions=[split("IRCON", date(2020, 4, 3), IRCON_SPLIT)],
    )

    built = build_instruments(evidence)

    (instrument,) = built.instruments
    assert [(link.kind, link.on) for link in instrument.links] == [
        ("corporate_action", date(2020, 4, 3))
    ]
    assert built.anomalies == ()


def test_a_split_more_than_a_week_from_the_isin_change_explains_nothing() -> None:
    evidence = Evidence(
        spans=[
            span("IRCON", IRCON_OLD, date(2018, 9, 28), date(2020, 4, 7)),
            span("IRCON", IRCON_NEW, date(2020, 4, 8), date(2026, 10, 19)),
        ],
        corporate_actions=[split("IRCON", date(2020, 3, 27), IRCON_SPLIT)],
    )

    built = build_instruments(evidence)

    assert len(built.instruments) == 2
    assert [a.kind for a in built.anomalies] == ["unexplained_isin_change"]


def test_a_listing_date_another_instrument_of_the_symbol_already_keys_is_not_used() -> None:
    # ALANKIT's ISIN changed on 16 December 2016 with no action NSE lists; the
    # equity master gives today's ISIN the listing date of 30 June 2015, the
    # day the earlier ISIN's history starts.
    evidence = Evidence(
        spans=[
            span("ALANKIT", "INE914E01032", date(2015, 6, 30), date(2016, 12, 15)),
            span("ALANKIT", "INE914E01040", date(2016, 12, 16), date(2026, 10, 19)),
        ],
        listings=[Listing("ALANKIT", "INE914E01040", date(2015, 6, 30))],
    )

    built = build_instruments(evidence)

    assert [(i.key, i.key_from) for i in built.instruments] == [
        ("ALANKIT@2015-06-30", "first_row"),
        ("ALANKIT@2016-12-16", "first_row"),
    ]


def test_a_rename_record_whose_old_symbol_keeps_trading_is_refused_as_a_conflict() -> None:
    # As if NSE's record were right about ETERNAL but ZOMATO went on trading:
    # joining them would give one Instrument two live Symbols on 9 April to 8 May.
    evidence = Evidence(
        spans=[
            span("ZOMATO", ZOMATO_ISIN, date(2021, 7, 23), date(2025, 5, 8)),
            span("ETERNAL", ZOMATO_ISIN, date(2025, 4, 9), date(2026, 10, 19)),
        ],
        symbol_changes=[rename("ZOMATO", "ETERNAL", date(2025, 4, 9))],
    )

    built = build_instruments(evidence)

    zomato = named(built.instruments, "ZOMATO", date(2025, 5, 8))
    eternal = named(built.instruments, "ETERNAL", date(2025, 4, 9))
    assert zomato != eternal
    assert (zomato.links, eternal.links) == ((), ())
    (anomaly,) = built.anomalies
    assert (anomaly.kind, anomaly.symbol, anomaly.on) == (
        "conflicting_link",
        "ZOMATO",
        date(2025, 4, 9),
    )
    assert anomaly.detail == (
        "refused NSE symbol change ZOMATO to ETERNAL effective 2025-04-09:"
        " ZOMATO (2021-07-23 to 2025-05-08) and ETERNAL (2025-04-09 to 2026-10-19)"
        " would both be live on 2025-04-09"
    )


HDFC_ISIN, MINDTREE_ISIN = "INE001A01036", "INE018I01017"


def test_a_merger_into_a_company_that_carries_on_stays_two_instruments() -> None:
    # HDFC merged into HDFCBANK: HDFC's last row is 12 July 2023, and HDFCBANK
    # trades on. Even a record naming the pair cannot join them, since HDFCBANK
    # was live beside HDFC for years.
    evidence = Evidence(
        spans=[
            span("HDFC", HDFC_ISIN, date(2015, 1, 1), date(2023, 7, 12)),
            span("HDFCBANK", HDFCBANK_NEW, date(2019, 9, 20), date(2026, 10, 19)),
        ],
        symbol_changes=[rename("HDFC", "HDFCBANK", date(2023, 7, 13))],
    )

    built = build_instruments(evidence)

    assert [(i.key, i.links) for i in built.instruments] == [
        ("HDFC@2015-01-01", ()),
        ("HDFCBANK@2019-09-20", ()),
    ]
    assert [(a.kind, a.symbol, a.on) for a in built.anomalies] == [
        ("conflicting_link", "HDFC", date(2023, 7, 13))
    ]


def test_a_merger_whose_absorbed_isin_ends_at_a_rename_stays_two_instruments() -> None:
    # MINDTREE merged into LTI, which was renamed LTIM on 5 December 2022:
    # MINDTREE's ISIN ends days before LTIM's first row. LTI and LTIM are one
    # Instrument; MINDTREE, live beside LTI until it ended, is not joined to it.
    evidence = Evidence(
        spans=[
            span("MINDTREE", MINDTREE_ISIN, date(2015, 1, 1), date(2022, 11, 23)),
            span("LTI", LTI_ISIN, date(2016, 7, 21), date(2022, 12, 2)),
            span("LTIM", LTI_ISIN, date(2022, 12, 5), date(2026, 10, 19)),
        ],
        symbol_changes=[
            rename("LTI", "LTIM", date(2022, 12, 5)),
            rename("MINDTREE", "LTIM", date(2022, 12, 5)),
        ],
    )

    built = build_instruments(evidence)

    lti = named(built.instruments, "LTIM", date(2022, 12, 5))
    mindtree = named(built.instruments, "MINDTREE", date(2022, 11, 23))
    assert [v.symbol for v in lti.intervals] == ["LTI", "LTIM"]
    assert [v.symbol for v in mindtree.intervals] == ["MINDTREE"]
    assert mindtree.links == ()
    assert [(a.kind, a.symbol) for a in built.anomalies] == [("conflicting_link", "MINDTREE")]


def test_a_new_symbol_under_an_existing_isin_with_no_record_is_a_link_candidate() -> None:
    # As if NSE's list omitted the LTIM to LTM rename: LTM is a candidate for a
    # cited manual link, never a link.
    evidence = Evidence(
        spans=[
            span("LTI", LTI_ISIN, date(2016, 7, 21), date(2022, 12, 2)),
            span("LTIM", LTI_ISIN, date(2022, 12, 5), date(2026, 2, 26)),
            span("LTM", LTI_ISIN, date(2026, 2, 27), date(2026, 10, 19)),
        ],
        symbol_changes=[rename("LTI", "LTIM", date(2022, 12, 5))],
    )

    built = build_instruments(evidence)

    assert [[v.symbol for v in i.intervals] for i in built.instruments] == [
        ["LTI", "LTIM"],
        ["LTM"],
    ]
    (candidate,) = built.candidates
    assert (
        candidate.old_symbol,
        candidate.new_symbol,
        candidate.isin,
        candidate.old_last,
        candidate.new_first,
    ) == ("LTIM", "LTM", LTI_ISIN, date(2026, 2, 26), date(2026, 2, 27))
    assert built.anomalies == ()


RUCHI_ISIN = "INE619A01035"
RUCHI_CIRCULAR = "https://archives.nseindia.com/content/circulars/CML52890.pdf"


def test_a_manual_link_joins_a_rename_nse_list_omits_and_carries_its_citation() -> None:
    evidence = Evidence(
        spans=[
            span("RUCHI", RUCHI_ISIN, date(2020, 1, 27), date(2022, 7, 12), series="BE"),
            span("PATANJALI", RUCHI_ISIN, date(2022, 7, 13), date(2026, 10, 19), series="BE"),
        ],
        manual_links=[
            ManualLink(
                old_symbol="RUCHI",
                new_symbol="PATANJALI",
                effective_date=date(2022, 7, 13),
                citation=RUCHI_CIRCULAR,
            )
        ],
    )

    (instrument,) = build_instruments(evidence).instruments

    assert [v.symbol for v in instrument.intervals] == ["RUCHI", "PATANJALI"]
    (link,) = instrument.links
    assert (link.kind, link.on, link.from_symbol, link.to_symbol) == (
        "manual",
        date(2022, 7, 13),
        "RUCHI",
        "PATANJALI",
    )
    assert link.source == f"manual link RUCHI to PATANJALI effective 2022-07-13: {RUCHI_CIRCULAR}"


def test_a_bonus_between_two_isins_years_apart_explains_neither_end_of_the_gap() -> None:
    # The old ISIN's last row is in 2017 and the new one's first in 2020: a
    # bonus in 2018 is within a week of neither, so the change is not at its
    # Ex-Date.
    bonus = CorporateAction(
        symbol="HDFCBANK",
        series="EQ",
        ex_date=date(2018, 5, 2),
        subject="Bonus 1:1",
        kind="bonus",
        factor=Fraction(1, 2),
    )
    evidence = Evidence(
        spans=[
            span("HDFCBANK", HDFCBANK_OLD, date(2015, 1, 1), date(2017, 6, 30)),
            span("HDFCBANK", HDFCBANK_NEW, date(2020, 1, 1), date(2026, 10, 19)),
        ],
        corporate_actions=[bonus],
    )

    built = build_instruments(evidence)

    assert len(built.instruments) == 2
    assert [(a.kind, a.on) for a in built.anomalies] == [
        ("unexplained_isin_change", date(2020, 1, 1))
    ]
