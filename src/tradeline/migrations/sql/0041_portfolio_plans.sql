-- A portfolio Strategy's Plans, Positions, Rebalances and Initial Buy in the
-- Ledger (ADR 0020, #93).
--
-- A portfolio Plan has an entry price and a size only: no stop, target or time
-- stop. Its Plan, Pass or Block, and the Position it opens, leave all three
-- NULL; a Plan with a stop still has all three.
ALTER TABLE plans
    ALTER COLUMN stop DROP NOT NULL,
    ALTER COLUMN target DROP NOT NULL,
    ALTER COLUMN time_stop_days DROP NOT NULL,
    ADD CONSTRAINT plans_exits_together CHECK (num_nulls(stop, target, time_stop_days) IN (0, 3));
ALTER TABLE passes
    ALTER COLUMN stop DROP NOT NULL,
    ALTER COLUMN target DROP NOT NULL,
    ALTER COLUMN time_stop_days DROP NOT NULL,
    ADD CONSTRAINT passes_exits_together CHECK (num_nulls(stop, target, time_stop_days) IN (0, 3));
ALTER TABLE blocks
    ALTER COLUMN stop DROP NOT NULL,
    ALTER COLUMN target DROP NOT NULL,
    ALTER COLUMN time_stop_days DROP NOT NULL,
    ADD CONSTRAINT blocks_exits_together CHECK (num_nulls(stop, target, time_stop_days) IN (0, 3));
ALTER TABLE positions
    ALTER COLUMN stop DROP NOT NULL,
    ALTER COLUMN target DROP NOT NULL,
    ALTER COLUMN time_stop_days DROP NOT NULL,
    ADD CONSTRAINT positions_exits_together
        CHECK (num_nulls(stop, target, time_stop_days) IN (0, 3));

-- A Rebalance's leaver is sold by an Order of purpose `rebalance`, an
-- after-market limit sell at the close less the band, sent again each day the
-- open does not fill it; its Position closes with reason `rebalance`.
ALTER TABLE orders DROP CONSTRAINT orders_purpose_check;
ALTER TABLE orders ADD CONSTRAINT orders_purpose_check
    CHECK (purpose IN ('entry', 'target', 'stop', 'time', 'boundary', 'rebalance'));
ALTER TABLE positions DROP CONSTRAINT positions_exit_reason_check;
ALTER TABLE positions ADD CONSTRAINT positions_exit_reason_check CHECK (
    (event = 'closed') = (exit_reason IS NOT NULL)
    AND exit_reason IN ('stop', 'target', 'time', 'boundary', 'rebalance')
);

-- What a portfolio Strategy's Run did, NULL for every other Run:
-- `initial_buy`, the account's Initial Buy, decided on the trading day before
-- its first and bought at that day's open; `rebalance`, a Rebalance decided on
-- the day (its entrants are the Run's Candidates, its leavers sold at the next
-- open); `rebalance_buys`, the day after a Rebalance, when the entrants are
-- sized from the cash then held and bought at the next open. An Initial Buy is
-- never a Rebalance.
ALTER TABLE runs ADD COLUMN portfolio_step text
    CHECK (portfolio_step IN ('initial_buy', 'rebalance', 'rebalance_buys'));
