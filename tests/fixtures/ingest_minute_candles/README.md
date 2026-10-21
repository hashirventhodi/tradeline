# Kite minute candle fixtures

Real Kite Connect historical responses, recorded on 2026-10-21 through the
transport's recording mode (`RecordingTransport` around the live transport,
the same request `ingest candles --interval minute` makes) for INFY (token
408065) and RELIANCE (token 738561), the two-member August 2024 Universe of
`../universe/`, over 2024-07-31 to 2024-08-01: one request per symbol, 375
bars a day. The two days straddle a month boundary so one run fills two
monthly partitions.

Kite's minute bars are adjusted for corporate actions like its daily bars
(see `docs/research.md`); the tests assert the values as Kite returned them.

A resend with a changed close is not a fixture: the test plays the INFY
response back narrowed to 2024-07-31 with the 09:15 close moved from 1842.6
to 1843.6 (`tests/playback.py`).

To re-record after `tradeline login`, with the master and August 2024's
Universe (top 2, 20-day window, 21-day listing) loaded in a scratch database:

```sh
rm tests/fixtures/ingest_minute_candles/*.json
uv run tradeline ingest candles --interval minute --universe-month 2024-08 \
  --from 2024-07-31 --to 2024-08-01 --record-fixtures tests/fixtures/ingest_minute_candles
```

A later recording may carry a different adjustment factor if another
corporate action has happened since; update the asserted literals from the
new file.
