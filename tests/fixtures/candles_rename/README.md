# Kite fixtures across a rename

Real Kite responses recorded on 2026-10-21 through `kite.fetch_instruments`
and `kite.fetch_candles` over a `RecordingTransport` (live, after the day's
`tradeline login`), for the ZOMATO to ETERNAL rename of 9 April 2025. The NSE
side of the same week is `../instruments/`.

- `GET_api.kite.trade_instruments_NSE_*`: Kite's NSE instrument dump, 10227
  rows, trimmed to its header and ETERNAL's line (token 1304833). The dump
  lists no ZOMATO: Kite lists an instrument under its current name only.
  `Content-Length` was set to the trimmed body's length. Its request is the
  one `../ingest_eod/` holds, so a test plays this directory first.
- `GET_api.kite.trade_instruments_historical_1304833_day_*`: ETERNAL's daily
  candles from 2025-04-07 to 2025-04-11, as served (four bars: the 7th and
  8th, when NSE still named the stock ZOMATO, the 9th, and the 11th; the
  10th was a holiday).

Kite's dump from before the rename (ZOMATO under the same token) is not a
fixture: the tests play this dump back with the line relabelled
(`tests/playback.py`).

To re-record after `tradeline login`, run `kite.fetch_instruments` and
`kite.fetch_candles(session, 1304833, interval="day", start=2025-04-07,
end=2025-04-11)` over a `RecordingTransport`, then trim the dump as above.
A later recording may carry another adjustment factor if Kite has adjusted
for a corporate action since; update the asserted closes from the new file.
