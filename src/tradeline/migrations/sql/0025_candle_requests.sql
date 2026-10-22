-- Each range candle ingest asked Kite for (#53, ADR 0018): one row per request,
-- per (Symbol, series, interval) and chunk, written whether or not Kite
-- returned bars, and again on every re-run. Kite Parity's "not fetched" reads
-- it, so a day is never counted as Kite missing a bar we never asked for.
-- The Symbol is the one fetched, the Instrument's current name. Append-only.
-- The run's ingest_runs row is written when the run finishes, after these
-- are committed, so the id is not a foreign key.
CREATE TABLE candle_requests (
    ingest_run_id uuid NOT NULL,
    symbol        text NOT NULL,
    series        text NOT NULL,
    interval      text NOT NULL,
    range_start   date NOT NULL,
    range_end     date NOT NULL,
    requested_at  timestamptz NOT NULL DEFAULT now(),
    CHECK (range_end >= range_start)
);

CREATE INDEX candle_requests_symbol ON candle_requests (symbol, series, interval);

GRANT SELECT, INSERT ON candle_requests TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON candle_requests FROM tradeline_app;
