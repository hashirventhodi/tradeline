-- NSE and BSE corporate announcements, one row per filing as each exchange
-- lists it, keyed by the exchange's own id (NSE's seq_id, BSE's NEWSID). The
-- same filing made to both exchanges is two rows. ``disseminated_at`` is the
-- official timestamp, when the exchange made the filing public; the query
-- layer's as-of cut is on it. ``symbol`` is the NSE symbol matched at
-- ingestion by tradeline.matching, NULL when there was no certain match.
-- Append-only: a filing is inserted once and the application role cannot
-- update or delete it.
CREATE TABLE announcements (
    source          text NOT NULL CHECK (source IN ('nse', 'bse')),
    source_id       text NOT NULL,
    disseminated_at timestamptz NOT NULL,
    received_at     timestamptz,
    exchange_code   text NOT NULL,
    company_name    text,
    symbol          text,
    subject         text NOT NULL,
    body            text,
    attachment_url  text,
    PRIMARY KEY (source, source_id)
);

-- The query layer reads one symbol across a time range.
CREATE INDEX announcements_symbol_time_idx ON announcements (symbol, disseminated_at);

GRANT SELECT, INSERT ON announcements TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON announcements FROM tradeline_app;
