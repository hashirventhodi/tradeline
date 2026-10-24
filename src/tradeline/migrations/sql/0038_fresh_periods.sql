-- Fresh Periods (ADR 0007, 2026-10-24 amendment): one row for each Strategy version
-- whose Not Independent Holdout run is scored on a Fresh Period. `protocol` names
-- the version; its account, `backtest_id`, starts fresh at the Protocol's starting
-- Equity on `first`, the first trading day after both the consumed Holdout's last
-- day and the version's freeze day, and runs at `parameters` (the ones its Holdout
-- ran at) to the last day with data, extended as more is collected. Its Runs sit in
-- the mode-tagged Ledger tables under `backtest_id`; like `holdout_attempts`, this
-- row carries no Engine Mode. Append-only.
CREATE TABLE fresh_periods (
    protocol    text PRIMARY KEY REFERENCES protocols (hash),
    backtest_id uuid NOT NULL UNIQUE,
    first       date NOT NULL,
    parameters  jsonb NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT clock_timestamp()
);

GRANT SELECT, INSERT ON fresh_periods TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON fresh_periods FROM tradeline_app;
