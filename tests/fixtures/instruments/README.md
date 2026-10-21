# Instrument fixtures

Real NSE responses recorded on 2026-10-21 through the NSE session
(`NseSession` over a `RecordingTransport`, browser TLS), then trimmed. The
kept rows are as served; only whole rows were removed.

- `GET_nsearchives.nseindia.com_content_equities_symbolchange.csv_*`: NSE's
  symbol-change list (`NAME OF THE COMPANY,OLD SYMBOL,NEW SYMBOL,EFFECTIVE
  DATE`), 1068 records, trimmed to 11: the renames of the #13 acceptance run
  (ZOMATO to ETERNAL; LTI to LTIM; LTIM to LTM; TELCO to TATAMOTORS;
  TATAMOTORS to TMPV; MCDOWELL-N to UNITDSPR; L&TFH to LTF; CADILAHC to
  ZYDUSLIFE), a record with no company name (780LTFL30), one whose name
  starts with a space (RDAXEDG), and one whose old and new Symbol are the
  same (INTERISE).
- `GET_nsearchives.nseindia.com_content_cm_BhavCopy_NSE_CM_0_0_0_2025040*`
  and `..._20250411_*`: NSE's end-of-day files for 7 to 11 April 2025, each
  trimmed to the rows of ZOMATO and ETERNAL (one EQ row a day: ZOMATO to the
  8th, ETERNAL from the 9th) and re-zipped under the file's own name. The
  10th, a holiday, is NSE's 404 as served.
- `GET_nsearchives.nseindia.com_content_equities_EQUITY_L.csv_*`: NSE's
  equity master trimmed to ETERNAL (listed 23-JUL-2021, ZOMATO's listing).

The symbol-change list was served gzip-encoded; the body is stored decoded
and the `Content-Encoding` header dropped. `Content-Length` was set to the
trimmed body's length. Kite's instrument list is not kept here: the tests
play `../ingest_eod/` back after this directory.

To re-record, run the NSE session over a `RecordingTransport` for
`symbolchange.csv`, `EQUITY_L.csv`, and the five days' end-of-day files
(`tradeline.sources.nse`: `SYMBOL_CHANGES_URL`, `EQUITY_MASTER_URL`,
`bhavcopy_url`), then trim as above.
