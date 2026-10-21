"""The ``instruments`` command: the Operator's review of identity, read only.

The fixture database is built from recordings with two deliberate changes to
NSE's symbol-change list, played back through ``playback.lines``:

- the ZOMATO to ETERNAL record is left out, so ETERNAL, under ZOMATO's ISIN
  from 9 April 2025, is a link candidate and ZOMATO's Instrument ends on the 8th;
- INTERISE's record (old and new Symbol the same) is rewritten as INFY to TCS
  effective 1 August 2024, a record the builder must refuse: both trade
  throughout July and August 2024.

The end-of-day rows are ``fixtures/ingest_eod/`` (July and August 2024:
BAFNAPH in BE, BAJAJ-AUTO, INFY, RELIANCE, TCS) and ``fixtures/instruments/``
(ZOMATO and ETERNAL, 7 to 11 April 2025). The Raw Series so runs to 11 April
2025, and every 2024 Instrument ended on 30 August 2024, the last day recorded.
The keys' listing dates are the equity masters' (``ingest_eod``'s lists the
five 2024 Symbols; ``instruments``' only ETERNAL, so ZOMATO's Instrument, cut
from ETERNAL's, is keyed by its first row).
"""

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import psycopg
import pytest

from playback import Playback, lines
from tradeline import queries
from tradeline.cli import main
from tradeline.db import app_connection
from tradeline.ingest.eod import ingest_eod
from tradeline.ingest.symbol_changes import ingest_symbol_changes
from tradeline.instruments_report import instruments_report
from tradeline.rebuild_instruments import rebuild_instruments
from tradeline.transport import PlaybackTransport

FIXTURES_DIR = Path(__file__).parent / "fixtures"
FIXTURES = FIXTURES_DIR / "instruments"
EOD_FIXTURES = FIXTURES_DIR / "ingest_eod"
REPO_ROOT = Path(__file__).resolve().parents[1]


def json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.splitlines()]


def without_zomato_and_with_infy_to_tcs(rows: list[str]) -> list[str]:
    kept = [row for row in rows if ",ZOMATO,ETERNAL," not in row]
    return [
        "Infosys Limited,INFY,TCS,01-AUG-2024" if ",INTERISE,INTERISE," in row else row
        for row in kept
    ]


@pytest.fixture(scope="module")
def reviewed(database_url: str) -> str:
    with app_connection(database_url) as conn:
        ingest_eod(
            conn, PlaybackTransport(EOD_FIXTURES), start=date(2024, 7, 1), end=date(2024, 8, 30)
        )
        ingest_eod(
            conn, Playback(FIXTURES, EOD_FIXTURES), start=date(2025, 4, 7), end=date(2025, 4, 11)
        )
        ingest_symbol_changes(
            conn, Playback(FIXTURES, edit=lines(without_zomato_and_with_infy_to_tcs))
        )
        rebuild_instruments(conn)
    return database_url


@pytest.fixture
def conn(reviewed: str) -> Iterator[psycopg.Connection]:
    with app_connection(reviewed) as conn:
        yield conn


def test_the_report_lists_the_refused_link_the_ended_instruments_and_the_candidate(
    conn: psycopg.Connection,
) -> None:
    report = instruments_report(conn)

    (anomaly,) = report.anomalies
    assert (anomaly.kind, anomaly.symbol, anomaly.on) == (
        "conflicting_link",
        "INFY",
        date(2024, 8, 1),
    )
    assert anomaly.detail.startswith("refused NSE symbol change INFY to TCS effective 2024-08-01")
    assert report.last_day == date(2025, 4, 11)
    assert [(e.key, e.last_day, e.last_symbol) for e in report.ended] == [
        ("BAFNAPH@2020-02-27", date(2024, 8, 30), "BAFNAPH"),
        ("BAJAJ-AUTO@2008-05-26", date(2024, 8, 30), "BAJAJ-AUTO"),
        ("INFY@1995-02-08", date(2024, 8, 30), "INFY"),
        ("RELIANCE@1995-11-29", date(2024, 8, 30), "RELIANCE"),
        ("TCS@2004-08-25", date(2024, 8, 30), "TCS"),
        ("ZOMATO@2025-04-07", date(2025, 4, 8), "ZOMATO"),
    ]
    (candidate,) = report.candidates
    assert (
        candidate.old_symbol,
        candidate.new_symbol,
        candidate.isin,
        candidate.old_last,
        candidate.new_first,
    ) == ("ZOMATO", "ETERNAL", "INE758T01015", date(2025, 4, 8), date(2025, 4, 9))


def test_the_rebuild_recorded_its_counts_in_the_run_details(conn: psycopg.Connection) -> None:
    (run,) = queries.ingest_runs(conn, source="instrument_links")

    assert run.details == {
        "spans": 7,
        "symbol_changes": 10,
        "manual_links": 0,
        "instruments": 7,
        "links": {},
        "keys_from": {"first_row": 1, "listing_date": 6},
        "anomalies": {"conflicting_link": 1},
        "candidates": 1,
    }


def test_the_instruments_command_prints_the_report_and_writes_nothing(
    reviewed: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("TRADELINE_CONFIG", str(REPO_ROOT / "tradeline.toml"))
    monkeypatch.setenv("DATABASE_URL", reviewed)
    with app_connection(reviewed) as conn:
        before = repr((queries.ingest_runs(conn), instruments_report(conn)))

    assert main(["instruments"]) == 0
    out, err = capsys.readouterr()

    finished = json_lines(out)[-1]
    assert (finished["command"], finished["msg"]) == ("instruments", "instruments finished")
    assert finished["report"] == {
        "last_day": "2025-04-11",
        "anomalies": {"conflicting_link": 1},
        "ended": 6,
        "candidates": 1,
    }
    assert "refused NSE symbol change INFY to TCS effective 2024-08-01" in err
    assert "ZOMATO@2025-04-07  2025-04-08  ZOMATO" in err
    assert "ZOMATO to ETERNAL  INE758T01015  2025-04-08  2025-04-09" in err
    with app_connection(reviewed) as conn:
        assert repr((queries.ingest_runs(conn), instruments_report(conn))) == before
