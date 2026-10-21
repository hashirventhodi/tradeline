-- News items from the configured RSS feeds, one row per story. A story is
-- identified by ``content_hash``, the SHA-256 of its normalised title and link
-- (see tradeline.ingest.news), so the same story carried by two feeds, or seen
-- again on the next run, is stored once, under the feed it was first seen in.
-- ``published_at`` is the feed's pubDate (NULL when the feed gave none or an
-- unreadable one); ``fetched_at`` is when this collector first saw the story.
-- ``symbols`` are the NSE symbols matched deterministically when the row was
-- written (tradeline.matching); empty means unmatched. Append-only: the
-- application role cannot update or delete a row.
CREATE TABLE news_items (
    content_hash text PRIMARY KEY,
    feed         text NOT NULL,
    title        text NOT NULL,
    link         text NOT NULL,
    summary      text,
    published_at timestamptz,
    fetched_at   timestamptz NOT NULL,
    symbols      text[] NOT NULL DEFAULT '{}'
);

-- The query layer reads a time range, optionally for one symbol.
CREATE INDEX news_items_known_at_idx ON news_items ((coalesce(published_at, fetched_at)));
CREATE INDEX news_items_symbols_idx ON news_items USING gin (symbols);

GRANT SELECT, INSERT ON news_items TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON news_items FROM tradeline_app;
