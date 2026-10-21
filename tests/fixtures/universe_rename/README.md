# Universe rename fixtures

Real NSE responses recorded on 2026-10-21 through the NSE session
(`NseSession` over a `RecordingTransport` around `BrowserTransport`), then
trimmed. The kept rows are as served; only whole rows were removed.

- `GET_nsearchives.nseindia.com_content_cm_BhavCopy_NSE_CM_0_0_0_2025*`: NSE's
  end-of-day files for every calendar day from 17 March to 30 April 2025, each
  trimmed to the rows of RELIANCE, INFY, TCS, ZOMATO, and ETERNAL and re-zipped
  under the file's own name. ZOMATO trades to 8 April and ETERNAL from 9 April
  (NSE's rename, effective 9 April 2025). Weekends and the holidays (31 March,
  10, 14, and 18 April) are NSE's 404 as served.
- `GET_nsearchives.nseindia.com_content_equities_EQUITY_L.csv_*`: NSE's equity
  master trimmed to ETERNAL (listed 23-JUL-2021, ZOMATO's listing), INFY,
  RELIANCE, and TCS.

The master was served gzip-encoded; the body is stored decoded and the
`Content-Encoding` header dropped. `Content-Length` was set to each trimmed
body's length. NSE's symbol-change list comes from `../instruments/` and
Kite's instrument list from `../ingest_eod/`.

To re-record, run the NSE session over a `RecordingTransport` for
`EQUITY_L.csv` (`tradeline.sources.nse.EQUITY_MASTER_URL`) and
`fetch_bhavcopy` for each day from 17 March to 30 April 2025 (with
`TRADELINE_ENV=development`), then trim as above.
