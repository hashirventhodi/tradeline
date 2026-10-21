# RSS news fixtures

Real responses from two Economic Times feeds, recorded on 2026-10-21 at
about 14:18 IST with `RecordingTransport` around the live transport
(`tradeline.sources.rss.fetch_feed`, the same call `ingest news` makes):

- `et_markets`: https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms
- `et_stocks`: https://economictimes.indiatimes.com/markets/stocks/rssfeeds/2146842.cms

Each feed carried 50 items. The bodies were trimmed by deleting whole
`<item>` elements, keeping five per feed and leaving the channel header and
every kept item byte-for-byte as served. The `set-cookie` response header
was removed because it carried the recording machine's geolocation; nothing
else in the headers was changed.

Kept items, chosen for the tests:

- In both feeds, same title and link (the cross-feed duplicates, real, not
  derived): "Reliance Industries shares jump 3%…", "Missed reversal in IT
  stocks? Jefferies…" (summary names Infosys and TCS), "Bajaj Finance
  shares fall 3%…".
- Only in `et_markets`: "Jio IPO: Nithin Kamath…" (summary says only
  "Reliance Jio", a vague name), "Carlsberg said to near public filing…".
- Only in `et_stocks` (as kept here): "TCS Share Price Live Updates…" and
  "Bajaj Finance Share Price Live Updates…".

To re-record, run with a database and the live transport:

```sh
uv run tradeline ingest news --feed et_markets --feed et_stocks \
  --record-fixtures tests/fixtures/ingest_news
```

then trim as above and update the literals in `tests/test_ingest_news.py`
from the new files.
