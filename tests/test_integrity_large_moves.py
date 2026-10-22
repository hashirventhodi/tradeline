"""``integrity``: the Large Moves of ever-members, the Large Move records, and gaps (ADR 0018).

The end-of-day fixtures (``ingest_eod``, 2024-07-01 to 2024-08-30) hold
RELIANCE, INFY, TCS, BAJAJ-AUTO (EQ) and BAFNAPH (BE), with no close-to-close
move beyond 1.5x. Moves are made by playing NSE's recorded files back with
closes changed (``playback.bhavcopy_rows``), and a split by playing NSE's
recorded Corporate Action list back with KPIGREEN's 2 for 1 split of
2024-07-18 filed under TCS on 2024-08-05. No fixture file is edited.

The ever-members come from two stored Universe months under two rule
versions: August, ranking Symbols (RELIANCE, INFY, TCS by July's traded
value), and September, ranking Instruments (RELIANCE alone). BAJAJ-AUTO is
never a member. Each test writes its Trading Calendar and Large Move records
into a temporary directory.
"""

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from playback import Playback, bhavcopy_rows
from tradeline import queries
from tradeline.config import Config, UniverseRule
from tradeline.db import app_connection
from tradeline.ingest.corporate_actions import ingest_corporate_actions
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.integrity import Gap, IntegrityReport, LargeMove, integrity
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.transport import PlaybackTransport, Request
from tradeline.universe import compute_universe

FIXTURES = Path(__file__).parent / "fixtures"
START, END = date(2024, 7, 1), date(2024, 8, 30)
AUGUST, SEPTEMBER = date(2024, 8, 1), date(2024, 9, 1)
SPLIT = date(2024, 8, 5)  # KPIGREEN's split, filed under TCS
SERIES_MOVE = date(2024, 8, 26)  # INFY trades in BE from here, its closes doubled
RELIANCE, INFY, TCS = "RELIANCE@1995-11-29", "INFY@1995-02-08", "TCS@2004-08-25"
CALENDAR = (
    "covers,2024-01-01,2024-12-31\n"
    "date,kind,citation\n"
    "2024-07-17,holiday,NSE/CMTR/59722\n"
    "2024-08-15,holiday,NSE/CMTR/59722\n"
)
RECORDS_HEADER = "symbol,trade_date,reason,citation\n"
BY_SYMBOL = UniverseRule(
    top_n=3, lookback_trading_days=15, min_listing_trading_days=1, min_presence_percent=50
)
BY_INSTRUMENT = replace(BY_SYMBOL, top_n=1, rank_by="instrument")
D = Decimal

# (day, Symbol) -> the close NSE's file is played back with.
CLOSES = {
    (date(2024, 8, 30), "RELIANCE"): "6038.50",  # 3019.25 doubled
    (date(2024, 8, 30), "TCS"): "1517.90",  # 4553.75 cut to a third
    (date(2024, 8, 30), "BAJAJ-AUTO"): "32674.65",  # 10891.55 tripled; never a member
}
INFY_BE_CLOSES = {
    date(2024, 8, 26): "3752.30",
    date(2024, 8, 27): "3818.40",
    date(2024, 8, 28): "3843.60",
    date(2024, 8, 29): "3866.70",
    date(2024, 8, 30): "3887.40",
}


def moved(day: date, rows: list[str]) -> list[str]:
    """NSE's file with the closes of CLOSES, and INFY in BE from SERIES_MOVE, its closes doubled."""
    edited = []
    for row in rows:
        fields = row.split(",")
        if len(fields) > 17 and fields[0] != "TradDt":
            symbol = fields[7]
            if (day, symbol) in CLOSES:
                fields[17] = CLOSES[day, symbol]
            if symbol == "INFY" and day in INFY_BE_CLOSES:
                fields[8], fields[17] = "BE", INFY_BE_CLOSES[day]
        edited.append(",".join(fields))
    return edited


def split_filed_under_tcs(request: Request, body: bytes) -> bytes:
    if "corporates-corporateActions" not in request.url:
        return body
    records = json.loads(body)
    for record in records:
        if record["symbol"] == "KPIGREEN" and "Split" in record["subject"]:
            record.update(symbol="TCS", isin="INE467B01029", exDate="05-Aug-2024")
    return json.dumps(records).encode()


@pytest.fixture(scope="module")
def moves(database_url: str) -> str:
    with app_connection(database_url) as conn:
        eod = Playback(FIXTURES / "ingest_eod", edit=bhavcopy_rows(moved))
        ingest_eod(conn, eod, start=START, end=END)
        actions = Playback(FIXTURES / "ingest_corporate_actions", edit=split_filed_under_tcs)
        ingest_corporate_actions(conn, actions, start=START, end=date(2024, 12, 31))
        rebuild_instruments(conn)
        compute_universe(conn, BY_SYMBOL, month=AUGUST)
        compute_universe(conn, BY_INSTRUMENT, month=SEPTEMBER)
    return database_url


def run(
    url: str,
    tmp_path: Path,
    records: str = "",
    *,
    calendar: str = CALENDAR,
    start: date = START,
    end: date = END,
) -> IntegrityReport:
    calendar_file = tmp_path / "trading_calendar.csv"
    calendar_file.write_text(calendar)
    large_moves = tmp_path / "large_moves.csv"
    large_moves.write_text(RECORDS_HEADER + records)
    settings = Config.Integrity(trading_calendar=str(calendar_file), large_moves=str(large_moves))
    with app_connection(url) as conn:
        return integrity(conn, settings, start=start, end=end)


def test_a_large_move_with_no_split_bonus_or_record_fails_and_is_listed(
    moves: str, tmp_path: Path
) -> None:
    report = run(moves, tmp_path)

    assert not report.passed
    assert report.unexplained_large_moves == (
        LargeMove(INFY, "INFY", "BE", SERIES_MOVE, D("1862.10"), D("3752.30")),
        LargeMove(RELIANCE, "RELIANCE", "EQ", END, D("3041.85"), D("6038.50")),
        LargeMove(TCS, "TCS", "EQ", END, D("4511.80"), D("1517.90")),
    )
    assert "large move 2024-08-30 RELIANCE@1995-11-29 (RELIANCE EQ): 3041.85 to 6038.50" in (
        report.text()
    )
    assert report.summary()["listed_unexplained_large_moves"][0] == {
        "instrument": INFY,
        "symbol": "INFY",
        "series": "BE",
        "trade_date": "2024-08-26",
        "previous_close": "1862.10",
        "close": "3752.30",
        "explained_by": None,
    }


def test_a_move_on_a_splits_ex_date_is_explained_by_the_split(moves: str, tmp_path: Path) -> None:
    report = run(moves, tmp_path)

    (on_split,) = [m for m in report.large_moves if m.trade_date == SPLIT]
    # 4283.05 halved, to the 5 paisa tick, then TCS's close on the Ex-Date.
    assert on_split == LargeMove(TCS, "TCS", "EQ", SPLIT, D("2141.50"), D("4155.05"), "split")
    assert report.summary()["large_moves_explained"] == {"split": 1}


def test_a_move_is_measured_across_a_series_change_from_the_previous_row_in_any_series(
    moves: str, tmp_path: Path
) -> None:
    report = run(moves, tmp_path)

    infy = [m for m in report.large_moves if m.instrument == INFY]
    # From INFY's last EQ close to its first BE close; its later BE closes move little.
    assert infy == [LargeMove(INFY, "INFY", "BE", SERIES_MOVE, D("1862.10"), D("3752.30"))]


def test_an_instrument_never_in_a_universe_month_is_not_checked(moves: str, tmp_path: Path) -> None:
    report = run(moves, tmp_path)

    assert report.ever_members == 3
    assert not [m for m in report.large_moves if m.symbol == "BAJAJ-AUTO"]


def test_a_matching_record_explains_a_large_move_and_others_are_reported(
    moves: str, tmp_path: Path
) -> None:
    records = (
        "RELIANCE,2024-08-30,market,reviewer: no filing found\n"
        "INFY,2024-08-26,resumed,NSE/SURV/12345\n"
        "TCS,2024-08-29,dividend,NSE/CML/12345\n"
        "NOSUCH,2024-08-29,rights,NSE/CML/12345\n"
        "TCS,2024-06-28,demerger,outside the range: not judged\n"
    )

    report = run(moves, tmp_path, records)

    assert report.unexplained_large_moves == (
        LargeMove(TCS, "TCS", "EQ", END, D("4511.80"), D("1517.90")),
    )
    assert report.summary()["large_moves_explained"] == {
        "market": 1,
        "resumed": 1,
        "split": 1,
    }
    assert [(r.line, r.symbol) for r in report.unmatched_records] == [(4, "TCS")]
    assert [(r.line, r.symbol) for r in report.unresolved_records] == [(5, "NOSUCH")]
    assert "record line 4 (TCS 2024-08-29): matches no Large Move" in report.text()
    assert not report.passed


def test_gaps_are_listed_and_counted_and_do_not_fail_the_check(
    fresh_database_url: str, tmp_path: Path
) -> None:
    lost = {date(2024, 7, 22), date(2024, 7, 23)}

    def without_reliance(day: date, rows: list[str]) -> list[str]:
        return [r for r in rows if not (day in lost and ",RELIANCE,EQ," in r)]

    with app_connection(fresh_database_url) as conn:
        eod = Playback(FIXTURES / "ingest_eod", edit=bhavcopy_rows(without_reliance))
        ingest_eod(conn, eod, start=START, end=END)
        rebuild_instruments(conn)
        compute_universe(conn, BY_SYMBOL, month=AUGUST)

    report = run(fresh_database_url, tmp_path)

    assert report.passed
    assert report.gaps == (Gap(RELIANCE, date(2024, 7, 22)), Gap(RELIANCE, date(2024, 7, 23)))
    assert report.summary()["gaps"] == 2
    assert report.summary()["gaps_by_instrument"] == {RELIANCE: 2}
    assert "gap 2024-07-22 RELIANCE@1995-11-29" in report.text()


def test_a_member_whose_symbol_names_no_instrument_fails_the_check(
    fresh_database_url: str, tmp_path: Path
) -> None:
    with app_connection(fresh_database_url) as conn:
        ingest_eod(conn, Playback(FIXTURES / "ingest_eod"), start=START, end=END)
        compute_universe(conn, BY_SYMBOL, month=AUGUST)  # before any rebuild

    report = run(fresh_database_url, tmp_path)

    assert not report.passed
    assert report.unresolved_members == ("INFY", "RELIANCE", "TCS")
    assert report.ever_members == 0


def test_a_second_record_naming_the_same_instrument_and_day_is_reported_and_explains_nothing(
    fresh_database_url: str, tmp_path: Path
) -> None:
    # The records file refuses a second row for one Symbol and day; two Symbols can still
    # name one Instrument on a day. ZOMATO trades to 2025-04-08 and ETERNAL from the 9th
    # (``fixtures/instruments``), and on the 9th each names the one Instrument.
    with app_connection(fresh_database_url) as conn:
        nse = Playback(FIXTURES / "candles_rename", FIXTURES / "instruments")
        ingest_eod(conn, nse, start=date(2025, 4, 7), end=date(2025, 4, 11))
        ingest_symbol_changes(conn, PlaybackTransport(FIXTURES / "instruments"))
        rebuild_instruments(conn)
    records = (
        "ETERNAL,2025-04-09,market,reviewer: no filing found\n"
        "ZOMATO,2025-04-09,demerger,NSE/CML/12345\n"
    )

    report = run(
        fresh_database_url,
        tmp_path,
        records,
        calendar=(
            "covers,2025-01-01,2025-12-31\ndate,kind,citation\n2025-04-10,holiday,NSE/CMTR/65587\n"
        ),
        start=date(2025, 4, 7),
        end=date(2025, 4, 11),
    )

    assert [(r.line, r.symbol) for r in report.unmatched_records] == [(2, "ETERNAL")]
    assert [(r.line, r.symbol) for r in report.duplicate_records] == [(3, "ZOMATO")]
    assert report.passed
    assert report.summary()["duplicate_records"] == [
        {"line": 3, "symbol": "ZOMATO", "trade_date": "2025-04-09", "reason": "demerger"}
    ]
    assert (
        "record line 3 (ZOMATO 2025-04-09): names the Instrument and day of an earlier record;"
        " not read" in report.text()
    )


def test_a_day_with_rows_in_two_listing_series_is_read_in_eq_and_not_compared_within_itself(
    fresh_database_url: str, tmp_path: Path
) -> None:
    # RELIANCE has a BE row beside its EQ row on 2024-07-15, at three times the price: the
    # day's EQ row is the one read, so neither the BE row nor the next day moves.
    day = date(2024, 7, 15)

    def also_in_be(on: date, rows: list[str]) -> list[str]:
        extra = []
        for row in rows:
            fields = row.split(",")
            if on == day and len(fields) > 17 and fields[7] == "RELIANCE" and fields[8] == "EQ":
                fields[8], fields[17] = "BE", str(Decimal(fields[17]) * 3)
                extra.append(",".join(fields))
        return rows + extra

    with app_connection(fresh_database_url) as conn:
        eod = Playback(FIXTURES / "ingest_eod", edit=bhavcopy_rows(also_in_be))
        ingest_eod(conn, eod, start=START, end=END)
        rebuild_instruments(conn)
        compute_universe(conn, BY_SYMBOL, month=AUGUST)
        (be,) = queries.end_of_day(conn, RELIANCE, start=day, end=day, series="BE")

    report = run(fresh_database_url, tmp_path)

    assert be.close == D("3194.45") * 3  # the BE row is stored beside the EQ one
    assert report.large_moves == ()
    assert report.passed
