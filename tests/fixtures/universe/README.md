# Universe fixtures

Real NSE end-of-day files, equity master, and Kite instrument dump, recorded
on 2026-10-21 through `ingest eod --record-fixtures` for 2024-07-01 to
2024-08-02 and trimmed to eight symbols, every kept line byte-for-byte as
recorded (each zip re-packed with only those lines, `content-length`
updated):

- RELIANCE, INFY, TCS, BAJAJ-AUTO: listed for decades, EQ series.
- ABDL: listed 2024-07-02, so 21 trading days before August.
- EMCURE: listed 2024-07-10, 15 trading days before August.
- NIFTYBEES: an ETF. It trades in series EQ and the UDiFF file marks it
  `STK` like a share; only its `INF` ISIN tells it apart.
- BAFNAPH: series BE (trade-for-trade).

To re-record, load into a scratch database outside the test environment,
then keep only those symbols' lines in each file:

```sh
uv run tradeline ingest eod --from 2024-07-01 --to 2024-08-02 --record-fixtures DIR
```
