-- Drift Replays (ADR 0007, 2026-10-25 amendment): one row each time a Drift
-- Replay of a frozen Protocol is requested at a commit (`git_commit`) other
-- than the one it was frozen at (`frozen_commit`), naming the curves it
-- replays (`curves`: `stitched_curve`, `fresh_period`, as asked).
--
-- outcome 'pass' or 'fail': each curve rerun at `git_commit` in an account of
-- its own (`stitched_backtest_id`, `fresh_backtest_id`; the Fresh Period through
-- `fresh_through`, the frozen account's last day then) and compared with the
-- frozen run: `differences` lists every difference in holdings, Fills, costs,
-- dividends, Demerger Credits and the tax ledger, ids and timestamps left out;
-- 'pass' when there is none. A later request at the same commit reads this row.
--
-- outcome 'refused': a request naming the Holdout, which a Drift Replay never
-- runs, recorded with `reason`; nothing ran.
--
-- The accounts' Runs sit in the mode-tagged Ledger tables under their backtest
-- ids, which no Window, Holdout attempt, Fresh Period or Gate evaluation names:
-- a Drift Replay is never a Window run and never read by a Gate. Append-only.
CREATE TABLE drift_replays (
    id                   uuid PRIMARY KEY,
    protocol             text NOT NULL REFERENCES protocols (hash),
    git_commit           text NOT NULL,
    frozen_commit        text NOT NULL,
    requested_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    curves               text[] NOT NULL CHECK (cardinality(curves) > 0),
    outcome              text NOT NULL CHECK (outcome IN ('pass', 'fail', 'refused')),
    stitched_backtest_id uuid UNIQUE,
    fresh_backtest_id    uuid UNIQUE,
    fresh_through        date,
    differences          jsonb,
    reason               text,
    CHECK (git_commit <> frozen_commit),
    CHECK ((outcome = 'refused') = (reason IS NOT NULL)),
    CHECK ((outcome = 'refused') = (differences IS NULL)),
    CHECK (outcome <> 'refused' OR (stitched_backtest_id IS NULL AND fresh_backtest_id IS NULL)),
    CHECK ((fresh_backtest_id IS NULL) = (fresh_through IS NULL)),
    CHECK (outcome <> 'pass' OR differences = '[]'::jsonb)
);

CREATE INDEX drift_replays_by_commit ON drift_replays (protocol, git_commit);

GRANT SELECT, INSERT ON drift_replays TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON drift_replays FROM tradeline_app;
