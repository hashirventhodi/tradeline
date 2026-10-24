-- The paper dry-run's marker (ADR 0019, #80). Phase 2's paper Runs are dry
-- runs: one Run over the data `collect-daily` loaded, its Orders filled the
-- next day against the end-of-day open. They prove the engine runs unchanged
-- in paper mode and are not paper trading, so every row they write carries
-- `dry_run` and the Paper Gate never counts it. Only a paper row can be one.
-- A dry run is an account of its own: the paper account's Orders, Fills and
-- Positions are those with `dry_run` false. A Strategy version is not marked:
-- the version is the same whether its Runs are dry or not.
ALTER TABLE runs
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT runs_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE candidates
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT candidates_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE plans
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT plans_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE passes
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT passes_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE blocks
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT blocks_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE verdicts
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT verdicts_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE orders
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT orders_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE fills
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT fills_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');
ALTER TABLE positions
    ADD COLUMN dry_run boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT positions_dry_run_is_paper CHECK (NOT dry_run OR mode = 'paper');

-- Each Position as it stands: its latest event's row (now with the marker).
DROP VIEW current_positions;
CREATE VIEW current_positions AS
SELECT DISTINCT ON (mode, position_id) *
FROM positions
ORDER BY mode, position_id, day DESC, seq DESC;
GRANT SELECT ON current_positions TO tradeline_app;
