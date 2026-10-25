# niftyindices.com Total Return Index fixtures

Real responses from niftyindices.com, recorded on 2026-10-25 at about 13:45
IST with `RecordingTransport` around the live transport, through
`tradeline.sources.niftyindices` (the calls `ingest index-history` makes),
about 4 s apart: the Total Return Index history of NIFTY100 LOW VOLATILITY 30
and NIFTY 500 from 2005-04-01 to 2019-09-30, posted to
https://www.niftyindices.com/BackPage/getTotalReturnIndexString in fifteen
April-to-March windows each (the site's historical-data page refuses more than
365 days).

Not trimmed: the Relative Drawdown Halt's threshold is computed from every
close. Each index has 3,595 closes, on the same days. Worked from them: the
ratio of the NIFTY100 Low Volatility 30 TRI to the NIFTY 500 TRI peaked on
2006-08-18 and fell 25.2946 percent to its trough on 2008-01-14, the worst over
the range; rounded up to the next 5 points, the threshold is 30 percent.

To re-record, with a database and the live transport:

```sh
uv run tradeline ingest index-history --record-fixtures tests/fixtures/index_history
```

then update the literals in `tests/test_lowvol.py` and
`tests/test_relative_drawdown_threshold.py`.
