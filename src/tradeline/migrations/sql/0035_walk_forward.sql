-- Walk-Forward over a frozen Protocol (#82). Like the Protocol itself these
-- rows carry no Engine Mode: Walk-Forward is a backtest's alone, and its
-- Stitched Curve's Runs, Orders, Fills and Positions sit in the mode-tagged
-- Ledger tables under the backtest id named here.
--
-- Append-only: the application role may read and insert, nothing else.

-- One Walk-Forward per Protocol: the backtest id its Stitched Curve's account
-- is written under. A run interrupted and started again continues it.
CREATE TABLE walk_forwards (
    protocol    text PRIMARY KEY REFERENCES protocols (hash),
    backtest_id uuid NOT NULL UNIQUE,
    started_at  timestamptz NOT NULL DEFAULT now()
);

-- One grid cell's In-Sample run in one Window, as a summary only: its Runs,
-- Orders, Fills and Positions are never kept. `parameters` are the Strategy's
-- (decimal strings by name, as `runs.parameters`); `trades` its closed trades;
-- `sharpe` the Sharpe of its daily Equity curve after costs (NULL when the curve
-- gives none); `runs` its trading days.
CREATE TABLE walk_forward_cells (
    protocol      text NOT NULL REFERENCES walk_forwards (protocol),
    window_number integer NOT NULL CHECK (window_number >= 1),
    parameters    jsonb NOT NULL,
    runs          integer NOT NULL CHECK (runs > 0),
    trades        integer NOT NULL CHECK (trades >= 0),
    sharpe        numeric,
    max_drawdown  numeric NOT NULL CHECK (max_drawdown >= 0),
    ending_equity numeric NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (protocol, window_number, parameters)
);

-- One Window once its Out-of-Sample stretch of the Stitched Curve is written:
-- how many cells ran and how many were eligible, the chosen cell (its
-- parameters, neighbour-averaged objective, in-sample Sharpe and trades; all
-- NULL when no cell was eligible, a Window that makes no selection), and its
-- out-of-sample result: the Stitched Curve's Runs in the Window, the trades it
-- closed there, and Equity before its first day and after its last (NULL with
-- no Runs).
CREATE TABLE walk_forward_windows (
    protocol             text NOT NULL REFERENCES walk_forwards (protocol),
    window_number        integer NOT NULL CHECK (window_number >= 1),
    cells                integer NOT NULL CHECK (cells > 0),
    eligible_cells       integer NOT NULL CHECK (eligible_cells BETWEEN 0 AND cells),
    parameters           jsonb,
    objective            numeric,
    in_sample_sharpe     numeric,
    in_sample_trades     integer,
    out_of_sample_runs   integer NOT NULL CHECK (out_of_sample_runs >= 0),
    out_of_sample_trades integer NOT NULL CHECK (out_of_sample_trades >= 0),
    start_equity         numeric,
    end_equity           numeric,
    recorded_at          timestamptz NOT NULL DEFAULT now(),
    CHECK ((parameters IS NULL) = (eligible_cells = 0)),
    CHECK ((parameters IS NULL) = (objective IS NULL)),
    CHECK ((parameters IS NULL) = (in_sample_sharpe IS NULL)),
    CHECK ((parameters IS NULL) = (in_sample_trades IS NULL)),
    CHECK ((out_of_sample_runs = 0) = (start_equity IS NULL)),
    CHECK ((start_equity IS NULL) = (end_equity IS NULL)),
    PRIMARY KEY (protocol, window_number)
);

GRANT SELECT, INSERT ON walk_forwards, walk_forward_cells, walk_forward_windows TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON walk_forwards, walk_forward_cells, walk_forward_windows
    FROM tradeline_app;

-- The Boundary Exit: at the last out-of-sample day's close every open Position
-- is sold, an Order of purpose `boundary` that fills at the close the day it is
-- placed (`session` is that day), and the Position closes with reason
-- `boundary`. Like the target placed with a Fill, it belongs to the Run of the
-- Pass its Position came from. A resting target it replaces is cancelled with
-- reason `boundary`.
ALTER TABLE orders DROP CONSTRAINT orders_purpose_check;
ALTER TABLE orders ADD CONSTRAINT orders_purpose_check
    CHECK (purpose IN ('entry', 'target', 'stop', 'time', 'boundary'));
ALTER TABLE positions DROP CONSTRAINT positions_check1;
ALTER TABLE positions ADD CONSTRAINT positions_exit_reason_check CHECK (
    (event = 'closed') = (exit_reason IS NOT NULL)
    AND exit_reason IN ('stop', 'target', 'time', 'boundary')
);
