import io
import json
import logging

import pytest

from tradeline.logging import configure_logging, log_context


@pytest.fixture
def stream() -> io.StringIO:
    stream = io.StringIO()
    configure_logging(command="migrate", level="DEBUG", stream=stream)
    return stream


def lines(stream: io.StringIO) -> list[dict[str, object]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_every_line_is_json_and_carries_the_command_name(stream: io.StringIO) -> None:
    logging.getLogger("tradeline.migrations").info("applied %s", "0001")
    logging.getLogger("some.library").warning("noisy")
    try:
        raise RuntimeError("boom")
    except RuntimeError:
        logging.getLogger("tradeline").exception("failed")

    records = lines(stream)
    assert len(records) == 3
    assert all(r["command"] == "migrate" for r in records)
    assert records[0]["msg"] == "applied 0001"
    assert records[0]["level"] == "INFO"
    assert records[0]["logger"] == "tradeline.migrations"
    assert "RuntimeError: boom" in str(records[2]["exc_info"])


def test_bound_context_appears_on_lines_logged_inside_it(stream: io.StringIO) -> None:
    log = logging.getLogger("tradeline.ingest")
    with log_context(source="kite", run_id="run-1"):
        log.info("inside")
    log.info("outside")

    inside, outside = lines(stream)
    assert inside["source"] == "kite"
    assert inside["run_id"] == "run-1"
    assert "source" not in outside


def test_extra_fields_on_a_record_are_included(stream: io.StringIO) -> None:
    logging.getLogger("tradeline").info("row", extra={"symbol": "INFY", "rows": 3})

    (record,) = lines(stream)
    assert record["symbol"] == "INFY"
    assert record["rows"] == 3


def test_lines_carry_a_utc_timestamp(stream: io.StringIO) -> None:
    logging.getLogger("tradeline").info("now")

    (record,) = lines(stream)
    assert str(record["ts"]).endswith("+00:00")
