-- Holdout attempts (ADR 0007, 2026-10-24 amendment): one row each time a
-- Strategy version asks to run the Holdout. A version is its Protocol, so
-- `protocol` names it; `family` is the Strategy Family its Protocol states.
--
-- outcome 'ran': the Holdout run, from a fresh account under `backtest_id` at
-- the Protocol's starting Equity, at `parameters` (the last Window's chosen
-- cell). `consumes` is true for the family's first run: it consumes the
-- family's Holdout, once. `corrected_since` lists the earlier runs (any
-- family) whose Universe or Adjusted Series hashes over the Holdout no longer
-- match the data this run verified: a rerun on a seen period after a data
-- correction. The run is independent only when it consumes the Holdout and
-- nothing was corrected since an earlier run; otherwise it is Not Independent.
--
-- outcome 'refused': a second Holdout run by the same version, refused by the
-- engine and recorded with `reason`.
--
-- Like `protocols` and `walk_forwards`, these rows carry no Engine Mode: the
-- Holdout is a backtest's alone, and its Runs sit in the mode-tagged Ledger
-- tables under `backtest_id`. Append-only.
CREATE TABLE holdout_attempts (
    id              uuid PRIMARY KEY,
    protocol        text NOT NULL REFERENCES protocols (hash),
    family          text NOT NULL,
    attempted_at    timestamptz NOT NULL DEFAULT clock_timestamp(),
    outcome         text NOT NULL CHECK (outcome IN ('ran', 'refused')),
    backtest_id     uuid UNIQUE,
    parameters      jsonb,
    consumes        boolean,
    corrected_since uuid[],
    independent     boolean GENERATED ALWAYS AS (
        consumes AND cardinality(corrected_since) = 0
    ) STORED,
    reason          text,
    CHECK ((outcome = 'ran') = (backtest_id IS NOT NULL)),
    CHECK ((outcome = 'ran') = (parameters IS NOT NULL)),
    CHECK ((outcome = 'ran') = (consumes IS NOT NULL)),
    CHECK ((outcome = 'ran') = (corrected_since IS NOT NULL)),
    CHECK ((outcome = 'refused') = (reason IS NOT NULL))
);

-- A version runs the Holdout once; a family's Holdout is consumed once.
CREATE UNIQUE INDEX holdout_attempts_one_run ON holdout_attempts (protocol)
    WHERE outcome = 'ran';
CREATE UNIQUE INDEX holdout_attempts_one_consumer ON holdout_attempts (family)
    WHERE consumes;

GRANT SELECT, INSERT ON holdout_attempts TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON holdout_attempts FROM tradeline_app;
