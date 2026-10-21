# Kite daily candle fixtures

Real Kite Connect historical responses, recorded on 2026-10-21 through
`ingest candles --record-fixtures` for the five symbols of `../ingest_eod/`
over 2024-07-01 to 2024-08-30, one request per symbol. `BAFNAPH` is the
`BE` series instrument (`BAFNAPH-BE`, token 4540161).

Kite's values differ from the NSE end-of-day files for the same days
because Kite adjusts candles for corporate actions (see
`docs/research.md`): RELIANCE is halved by its October 2024 bonus, INFY
carries a further factor of about 0.979 from a 2026 action. The tests
assert the values as Kite returned them.

A resend with a changed close is not a fixture: the test plays the INFY
response back narrowed to 2024-07-01 with the close moved from 1557.1 to
1558.1 (`tests/playback.py`).

To re-record after `tradeline login`:

```sh
rm tests/fixtures/ingest_candles/*.json
uv run tradeline ingest candles --interval day \
  --symbol BAFNAPH --symbol BAJAJ-AUTO --symbol INFY --symbol RELIANCE --symbol TCS \
  --from 2024-07-01 --to 2024-08-30 --record-fixtures tests/fixtures/ingest_candles
```

A later recording may carry a different adjustment factor if another
corporate action has happened since; update the asserted literals from
the new file rather than from NSE.
