-- The Backtest Gate's evaluations and the report's c sensitivity (#84). Like
-- `protocols`, `walk_forwards` and `holdout_attempts` these rows carry no
-- Engine Mode: the Backtest Gate is a backtest's alone, and the accounts it
-- reads sit in the mode-tagged Ledger tables under the backtest ids named here.
--
-- Append-only: the application role may read and insert, nothing else.

-- One evaluation of the Backtest Gate on a Protocol's Stitched Curve
-- (`stitched_backtest_id`) and Holdout run (`holdout_attempt`), each time it is
-- evaluated. `thresholds` are the Protocol's Gate thresholds as evaluated;
-- `inputs` each curve's closed trades, Sharpe, max drawdown and profit factor,
-- exact (decimal strings, null where the curve gives none); `standing` whether
-- the Holdout run was independent, whether it may pass, why, and its Fresh
-- Period when it is Not Independent; `checks` every threshold on every curve
-- with the value read and whether it held. `contract_notes` is how many real
-- contract notes the cost model was checked against then. `outcome` is pass,
-- fail, or cannot pass (a Not Independent run without a qualifying Fresh
-- Period, whatever its metrics), and `reason` says why.
CREATE TABLE gate_evaluations (
    id                   uuid PRIMARY KEY,
    gate                 text NOT NULL CHECK (gate = 'backtest'),
    protocol             text NOT NULL REFERENCES protocols (hash),
    evaluated_at         timestamptz NOT NULL DEFAULT clock_timestamp(),
    stitched_backtest_id uuid NOT NULL REFERENCES walk_forwards (backtest_id),
    holdout_attempt      uuid NOT NULL REFERENCES holdout_attempts (id),
    thresholds           jsonb NOT NULL,
    inputs               jsonb NOT NULL,
    standing             jsonb NOT NULL,
    checks               jsonb NOT NULL,
    contract_notes       integer NOT NULL CHECK (contract_notes >= 0),
    outcome              text NOT NULL CHECK (outcome IN ('pass', 'fail', 'cannot pass')),
    reason               text NOT NULL
);

-- The report's c sensitivity: a Protocol's Stitched Curve or Holdout run again
-- in its own account (`backtest_id`) with the Slippage impact c at `impact` in
-- place of the Protocol's, at the parameters each Window chose (the Stitched
-- Curve) or the Holdout ran at: information, never a Protocol result. Its Runs
-- record the impact too.
CREATE TABLE cost_sensitivities (
    protocol    text NOT NULL REFERENCES protocols (hash),
    curve       text NOT NULL CHECK (curve IN ('stitched_curve', 'holdout')),
    impact      numeric NOT NULL CHECK (impact >= 0),
    backtest_id uuid NOT NULL UNIQUE,
    started_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (protocol, curve, impact)
);

GRANT SELECT, INSERT ON gate_evaluations, cost_sensitivities TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON gate_evaluations, cost_sensitivities FROM tradeline_app;
