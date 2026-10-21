# NSE and BSE announcement fixtures

Real responses for 23 July 2024, recorded on 2026-10-21 through
`ingest announcements --record-fixtures` (live transport with browser TLS),
then trimmed. The records themselves are byte-for-byte as served; only
whole records were removed.

- `GET_www.nseindia.com_api_corporate-announcements_*`: NSE listed 545
  filings that day; kept are the 8 for INFY, RELIANCE, BAJAJ-AUTO, AJRINFRA,
  and the "Outcome of Board Meeting" filing of each of JISLJALEQS and
  JISLDVREQS.
- `GET_www.nseindia.com_<hash>`: the session warm-up. Its body (NSE's home
  page HTML) is dropped; only the status and the cookies it sets matter.
- `GET_api.bseindia.com_*`: BSE listed 940 filings over 19 pages of 50. Nine
  are kept, in BSE's newest-first order, re-packed as pages 1 (five records)
  and 2 (four), with `Table1[0].ROWCNT` set to 9 to match, so paging is
  still exercised. Kept: Infosys Ltd (2), Reliance Industries Ltd, Bajaj
  Auto Ltd, Jain Irrigation Systems Ltd (500219) and Jain Irrigation
  Systems Ltd_DVR (570004), two filings of scrip 132541 whose company name
  BSE leaves blank, and BSE's own clarification notice to Oceanic Foods Ltd,
  which has no submission time. The other 17 pages are not kept.

`Content-Length` headers were dropped from trimmed responses.

The ambiguous-name case is real, not constructed: NSE's equity master names
both JISLJALEQS and JISLDVREQS "Jain Irrigation Systems Limited", so BSE's
"Jain Irrigation Systems Ltd", which carries no NSE symbol, matches two
symbols and is stored unmatched.

`../ingest_announcements_master/` is `ingest eod --from 2024-07-23` recorded
the same day, trimmed to BAFNAPH, BAJAJ-AUTO, INFY, RELIANCE, TCS,
JISLJALEQS, and JISLDVREQS: the equity master (lines kept), Kite's
instrument list (lines kept), and the 23 July end-of-day file (rows kept,
re-zipped under the original member name).

To re-record (needs a scratch database at `DATABASE_URL`):

```sh
uv run tradeline ingest eod --from 2024-07-23 --record-fixtures /tmp/rec/master
uv run tradeline ingest announcements --from 2024-07-23 --record-fixtures /tmp/rec/ann
```

then trim as above.
