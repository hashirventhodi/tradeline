-- NSE Indices' Total Return Index history, as niftyindices.com publishes it, for the
-- Relative Drawdown Halt's threshold (ADR 0020, #97; tradeline.ingest.index_history).
--
-- One row per (index, day, value) as served. A value NSE later revises is a
-- second row for the day, the later `fetched_at` the one read. The threshold a
-- Protocol freezes is computed from these rows, and the Protocol records their
-- hash, so a revision after a freeze is detectable.
CREATE TABLE index_values (
    index_name text NOT NULL,
    day        date NOT NULL,
    value      numeric NOT NULL CHECK (value > 0),
    fetched_at timestamptz NOT NULL,
    PRIMARY KEY (index_name, day, value)
);

GRANT SELECT, INSERT ON index_values TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON index_values FROM tradeline_app;
