# Adjusted Series per Instrument fixtures

Real NSE responses recorded on 2026-10-21 through the NSE session
(`NseSession` over a `RecordingTransport`, browser TLS), then trimmed. The
kept rows are as served; only whole rows were removed.

Gujarat Gas split its shares 5 for 1 (Rs 10 to Rs 2) with Ex-Date 15 January
2019 and a new ISIN (INE844O01022 to INE844O01030, in NSE's files from the
16th), and was renamed GUJGASLTD to GUJENERGY effective 1 July 2026.

- `GET_nsearchives.nseindia.com_content_historical_EQUITIES_2019_JAN_*`:
  NSE's legacy end-of-day files for 14 to 16 January 2019, each trimmed to
  GUJGASLTD's one EQ row and re-zipped under the file's own member name.
- `GET_nsearchives.nseindia.com_content_cm_BhavCopy_NSE_CM_0_0_0_2026*`:
  NSE's end-of-day files for 29 June to 1 July 2026, trimmed to the one EQ
  row of GUJGASLTD (to the 30th) or GUJENERGY (the 1st), re-zipped likewise.
  The days after the rename are left out: from 2 July the stock trades in BE.
- `GET_nsearchives.nseindia.com_content_equities_EQUITY_L.csv_*`: NSE's
  equity master trimmed to GUJENERGY (listed 15-SEP-2015, ISIN INE844O01030).
- `GET_nsearchives.nseindia.com_content_equities_symbolchange.csv_*`: NSE's
  symbol-change list trimmed to the GUJGASLTD to GUJENERGY record.
- `GET_www.nseindia.com_api_corporates-corporateActions_*`: NSE's Corporate
  Action list for GUJENERGY, ex-dates 14 to 16 January 2019, as served: the
  one split, which NSE now files under GUJENERGY with the new ISIN.

The symbol-change list and the equity master were served gzip-encoded and the
Corporate Action list brotli-encoded; the bodies are stored decoded and the
`Content-Encoding` header dropped. `Content-Length` was set to each stored
body's length. The www warm-up comes from `../ingest_corporate_actions/` and
Kite's instrument list from `../ingest_eod/`.

To re-record, run the NSE session over a `RecordingTransport` for
`EQUITY_L.csv`, `symbolchange.csv`, the six days' end-of-day files, and
`corporate_actions_url(date(2019, 1, 14), date(2019, 1, 16),
symbol="GUJENERGY")` (`tradeline.sources.nse`), then trim as above.
