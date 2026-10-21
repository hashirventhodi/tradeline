"""A recomputed Universe month against the stored one, by Instrument identity.

Pure: the stored and recomputed members are written out as ``compute_universe``
and ``queries.universe_membership`` give them. The case is #29's: the SUBEX
manual link renamed the Instrument ``SUBEXLTD@2020-11-05`` to
``SUBEX@2020-11-05`` after SUBEXLTD's months were stored, without regrouping
any row in their windows.
"""

from dataclasses import replace
from datetime import date
from decimal import Decimal

from tradeline.queries import ObsoleteUniverseKey, UniverseMember
from tradeline.universe import check_membership

JULY = date(2021, 7, 1)
VERSION = "ded30b1014c4"


def member(rank: int, symbol: str, traded_value: str, instrument: str) -> UniverseMember:
    return UniverseMember(
        month=JULY,
        rule_version=VERSION,
        rank=rank,
        symbol=symbol,
        series="EQ",
        traded_value=Decimal(traded_value),
        instrument=instrument,
    )


STORED = [
    member(193, "IEX", "70700000000.00", "IEX@2017-10-23"),
    member(194, "SUBEXLTD", "70646276764.40", "SUBEXLTD@2020-11-05"),
]
RECOMPUTED = [
    member(193, "IEX", "70700000000.00", "IEX@2017-10-23"),
    member(194, "SUBEXLTD", "70646276764.40", "SUBEX@2020-11-05"),
]
RENAMED = ObsoleteUniverseKey(JULY, VERSION, "SUBEXLTD", "SUBEXLTD@2020-11-05", "SUBEX@2020-11-05")


def test_a_key_a_later_rebuild_renamed_is_reported_as_obsolete_not_as_a_difference() -> None:
    check = check_membership(STORED, RECOMPUTED, obsolete=[RENAMED])

    assert not check.differs
    assert check.obsolete == (RENAMED,)


def test_without_the_rename_known_the_keys_differ() -> None:
    assert check_membership(STORED, RECOMPUTED, obsolete=[]).differs


def test_a_rank_that_changed_is_a_difference_whatever_the_keys() -> None:
    swapped = [replace(RECOMPUTED[0], rank=194), replace(RECOMPUTED[1], rank=193)]

    assert check_membership(STORED, swapped, obsolete=[RENAMED]).differs


def test_a_traded_value_that_changed_is_a_difference() -> None:
    more = [RECOMPUTED[0], replace(RECOMPUTED[1], traded_value=Decimal("70646276765.40"))]

    assert check_membership(STORED, more, obsolete=[RENAMED]).differs


def test_a_member_missing_from_the_recompute_is_a_difference() -> None:
    assert check_membership(STORED, RECOMPUTED[:1], obsolete=[RENAMED]).differs


def test_an_obsolete_key_no_instrument_now_answers_for_is_a_difference() -> None:
    unresolved = replace(RENAMED, current_key=None)

    check = check_membership(STORED, RECOMPUTED, obsolete=[unresolved])

    assert check.differs
    assert check.obsolete == (unresolved,)
