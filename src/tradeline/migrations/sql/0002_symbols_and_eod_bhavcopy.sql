-- Counts beyond rows_written: how many rows the source files held, and
-- source-specific notes (for end-of-day files, the trading days that had no
-- file) so a run with zero new rows is distinguishable from one that saw nothing.
ALTER TABLE ingest_runs ADD COLUMN rows_seen integer NOT NULL DEFAULT 0;
ALTER TABLE ingest_runs ADD COLUMN details jsonb NOT NULL DEFAULT '{}'::jsonb;

-- The instrument master. One row per NSE (symbol, series) instrument: a symbol
-- that moves between series (EQ to BE and back) keeps a row per series. Fields
-- fill in from whichever source knows them: the equity master (name, ISIN,
-- listing date), end-of-day files (ISIN; the only source for a delisted
-- symbol), and the Kite instrument dump (token). Each source updates only the
-- fields it knows and never blanks one, so the table is upserted, not append-only.
CREATE TABLE symbols (
    symbol                text NOT NULL,
    series                text NOT NULL,
    isin                  text,
    name                  text,
    listing_date          date,
    kite_instrument_token bigint,
    PRIMARY KEY (symbol, series)
);

GRANT SELECT, INSERT, UPDATE ON symbols TO tradeline_app;
REVOKE DELETE, TRUNCATE ON symbols FROM tradeline_app;

-- NSE end-of-day file rows, one per (day, symbol, series), stored as the file
-- states them: numerics are kept at the file's precision and nothing is derived.
-- NSE publishes each day's file once as final, so rows are inserted once and
-- never modified; the application role cannot update or delete them.
CREATE TABLE eod_bhavcopy (
    trade_date   date NOT NULL,
    symbol       text NOT NULL,
    series       text NOT NULL,
    isin         text,
    open         numeric,
    high         numeric,
    low          numeric,
    close        numeric,
    last         numeric,
    prev_close   numeric,
    traded_qty   bigint NOT NULL,
    traded_value numeric NOT NULL,
    trades       integer NOT NULL,
    PRIMARY KEY (trade_date, symbol, series)
);

-- The query layer reads one symbol across a date range.
CREATE INDEX eod_bhavcopy_symbol_date_idx ON eod_bhavcopy (symbol, trade_date);

GRANT SELECT, INSERT ON eod_bhavcopy TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON eod_bhavcopy FROM tradeline_app;
