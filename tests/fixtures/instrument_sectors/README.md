# Sectors through the Instrument fixtures

Real NSE responses recorded on 2026-10-21 through the NSE session
(`ingest eod --record-fixtures` and `sectors --record-fixtures`), then trimmed.
The kept lines are as served; only whole lines were removed.

- `GET_nsearchives.nseindia.com_content_cm_BhavCopy_NSE_CM_0_0_0_202503*`:
  NSE's end-of-day files for 1 to 31 March 2025, each trimmed to ZOMATO's
  row (one EQ row a day) and re-zipped under the file's own name. The
  weekends and the two holidays (14 and 31 March) are NSE's 404 as served.
  With `../instruments/` (7 to 11 April, ZOMATO to ETERNAL on the 9th), they
  give a Universe for April 2025 whose member is ZOMATO.
- `GET_nsearchives.nseindia.com_content_indices_ind_niftytotalmarket_list.csv_*`:
  NSE's NIFTY Total Market list, 755 rows, trimmed to ETERNAL's
  (`Consumer Services`). It was served gzip-encoded; the body is stored
  decoded, the `Content-Encoding` header dropped, and `Content-Length` set to
  the trimmed body's length.

The equity master and Kite's instrument list are not kept here: the tests
play `../instruments/` and `../ingest_eod/` back after this directory, and
`../sectors/` for the NIFTY 200 list.

To re-record, load into a scratch database outside the test environment, then
keep only those lines:

```sh
uv run tradeline ingest eod --from 2025-03-01 --to 2025-03-31 --record-fixtures DIR
uv run tradeline sectors --record-fixtures DIR
```
