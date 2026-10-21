# collect-daily announcement fixtures

Real NSE and BSE announcement responses for 1 August 2024, recorded on
2026-10-21 through `ingest announcements --record-fixtures` (live transport
with browser TLS), then trimmed. The kept records are as served; only whole
records were removed, and each body was re-serialised as compact JSON.

- `GET_www.nseindia.com_api_corporate-announcements_*`: NSE listed 1045
  filings that day; kept are the 5 of INFY (2), TCS, RELIANCE, and
  BAJAJ-AUTO, the August 2024 Universe in `tests/test_collect_daily.py`.
- NSE's session warm-up (`GET https://www.nseindia.com/`) is not kept
  here: it is the same request as the one in `../ingest_announcements/`,
  and the test plays that directory back after this one.
- `GET_api.bseindia.com_*`: BSE listed 1647 filings over 33 pages of 50. The
  5 filings of Infosys Ltd (2), Tata Consultancy Services Ltd, Reliance
  Industries Ltd, and Bajaj Auto Ltd, found on pages 2, 6, 13, 21, and 33,
  are re-packed newest first as page 1 with `Table1[0].ROWCNT` set to 5.
  The other pages are not kept.

`Content-Length` headers were dropped from trimmed responses.

To re-record (needs a scratch database at `DATABASE_URL`):

```sh
uv run tradeline ingest announcements --from 2024-08-01 --record-fixtures /tmp/rec/ann
```

then trim as above.
