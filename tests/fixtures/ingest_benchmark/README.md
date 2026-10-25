# AMFI fixtures

Real responses from AMFI, recorded on 2026-10-25 at about 12:24 IST with
`RecordingTransport` around the live transport, through
`tradeline.sources.amfi` (the calls `ingest benchmark` makes), about 4 s apart:

- the scheme master, https://portal.amfiindia.com/spages/NAVAll.txt (NAVs of
  2026-10-23);
- the NAV history of 147625 (Motilal Oswal Nifty 500 Index Fund, Direct Plan,
  Growth) and 141596 (ICICI Prudential Nifty 100 Low Volatility 30 ETF), from
  2019-09-11 to 2026-10-23, in the two four-year windows
  2019-09-11 to 2023-09-10 and 2023-09-11 to 2026-10-23
  (https://www.amfiindia.com/api/nav-history?query_type=historical_period&...).

Trimmed by deleting whole lines and records only:

- the master kept its first six lines (the header and the first headings),
  the lines of 147625, 147626 (the Regular Plan) and 141596, and the fund-house
  heading before each, with its CRLF line endings;
- each history window kept its first and last 25 records (of 989, 767 or 771),
  re-serialised compactly, which reproduces AMFI's body byte for byte before
  trimming.

The `content-length` header was dropped, since the bodies changed; the recorded
`content-encoding: gzip` is the live response's (the transport stores the
decoded body). Nothing else was edited.

To re-record, with a database and the live transport:

```sh
uv run tradeline ingest benchmark --to 2026-10-23 \
  --record-fixtures tests/fixtures/ingest_benchmark
```

then trim as above and update the literals in `tests/test_ingest_benchmark.py`.
