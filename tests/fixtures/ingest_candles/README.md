# Kite daily candle fixtures

These are **not Kite recordings**. They are Kite's documented historical
response shape (`{"status": "success", "data": {"candles": [[ts, o, h, l, c, v], ...]}}`)
filled with the real NSE end-of-day values from `../ingest_eod/` for the same
five symbols and range (2024-07-01 to 2024-08-30), one request per symbol. A
real recording needs the Operator's daily access token, which the implementing
session could not obtain. `../ingest_candles_resend/` is INFY for 2024-07-01
with the close changed by hand from 1590.8 to 1591.8.

To replace them with real responses, after `tradeline login`:

```sh
rm tests/fixtures/ingest_candles/*.json
uv run tradeline ingest candles --interval day \
  --symbol BAFNAPH --symbol BAJAJ-AUTO --symbol INFY --symbol RELIANCE --symbol TCS \
  --from 2024-07-01 --to 2024-08-30 --record-fixtures tests/fixtures/ingest_candles
```

Then re-derive the resend fixture from the real INFY file (copy it to
`../ingest_candles_resend/`, narrow the request to 2024-07-01, change the close)
and update any value the tests assert on if Kite's bars differ from NSE's.
