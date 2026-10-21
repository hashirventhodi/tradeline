"""Kite Connect: the instrument dump.

``GET /instruments/NSE`` is a public CSV (no access token) listing every NSE
instrument Kite can trade with its ``instrument_token``, the identifier the
historical and streaming APIs key on. Equities are ``segment == "NSE"`` and
``instrument_type == "EQ"`` (indices sit in segment ``INDICES``).

Kite's ``tradingsymbol`` is NSE's symbol for the ``EQ`` series and
``SYMBOL-SERIES`` for every other series. The reverse is ambiguous because NSE
symbols may themselves contain hyphens (``BAJAJ-AUTO``), so this module never
splits a trading symbol; callers build the expected trading symbol from a known
(symbol, series) with ``trading_symbol`` and look it up.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass

from tradeline.transport import Request, Transport

INSTRUMENTS_URL = "https://api.kite.trade/instruments/NSE"
KITE_HEADERS: dict[str, str] = {"X-Kite-Version": "3"}


class KiteError(RuntimeError):
    pass


@dataclass(frozen=True)
class KiteInstrument:
    instrument_token: int
    trading_symbol: str


def trading_symbol(symbol: str, series: str) -> str:
    return symbol if series == "EQ" else f"{symbol}-{series}"


def fetch_instruments(transport: Transport) -> list[KiteInstrument]:
    """Every NSE equity instrument in Kite's dump."""
    response = transport.send(Request("GET", INSTRUMENTS_URL, headers=KITE_HEADERS))
    if response.status != 200:
        raise KiteError(f"HTTP {response.status} fetching {INSTRUMENTS_URL}")
    return parse_instruments(response.body)


def parse_instruments(body: bytes) -> list[KiteInstrument]:
    reader = csv.DictReader(io.StringIO(body.decode("utf-8")))
    return [
        KiteInstrument(
            instrument_token=int(record["instrument_token"]),
            trading_symbol=record["tradingsymbol"].strip(),
        )
        for record in reader
        if record["segment"] == "NSE" and record["instrument_type"] == "EQ"
    ]
