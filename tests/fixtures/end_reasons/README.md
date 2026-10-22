# End-reason fixtures

Real NSE responses recorded on 2026-10-21 through the NSE session
(`NseSession` over a `RecordingTransport`, browser TLS), then trimmed. The
kept rows are as served; only whole rows (or records) were removed.

- `GET_nsearchives.nseindia.com_content_historical_EQUITIES_2019_*`: NSE's
  legacy end-of-day files for every calendar day from 13 May to 10 June 2019,
  each trimmed to the rows of four Symbols and re-zipped under the file's own
  name:
  - IVRCLINFRA: EQ to 6 June, BE from 7 June (a series move);
  - PENPEBS: EQ to 6 June, its last row;
  - ANGIND: BZ, trading on some days only, last on 4 June;
  - BILPOWER: BZ, its last row on 13 May.
  Weekends and 5 June (a holiday) are NSE's 404 as served.
- `GET_www.nseindia.com_api_corporates-corporateActions_*`: NSE's corporate
  actions with an ex-date from 13 May to 10 June 2019, trimmed to the four
  Symbols' records: one, PENPEBS's "Scheme Of Amalgamation" ex 7 June 2019
  (record date 10 June). The SME and fund-unit lists for the same dates,
  recorded on 2026-10-22, hold none of the four Symbols' records (SME 2,
  fund units 91), so both are kept as `[]`. The session's warm-up request is
  `../ingest_corporate_actions/`'s.
- `GET_nsearchives.nseindia.com_content_equities_delisted.csv_*`: NSE's list
  of delisted securities (`Symbol,Company,Delisted Date,Type of Delisting`
  and five empty columns), 328 records, trimmed to five: the first
  (CABOTINDIA), one compulsory delisting (ANIKSHIP), BENTOLCHEM (its company
  name holds a 0xA0 byte, a non-breaking space in Windows encoding, which is
  not UTF-8), ANGIND (delisted on liquidation on 10 July 2020), and the last
  (HEXAWARE, 9 November 2020). It is served with CRLF line ends, so the body
  is stored base64-encoded.

Bodies served gzip-encoded are stored decoded with the `Content-Encoding`
header dropped; `Content-Length` was set to each trimmed body's length. NSE's
equity master and Kite's instrument dump are not kept here: the tests play
`../ingest_eod/` back after this directory, and neither lists these Symbols.

To re-record, run the NSE session over a `RecordingTransport` for
`bhavcopy_url(day)` for each day, `corporate_actions_url(2019-05-13,
2019-06-10, nse_list=...)` for each of `CORPORATE_ACTION_LISTS`, and `DELISTINGS_URL` (`tradeline.sources.nse`), then trim as
above.
