-- Every read of an account (``tradeline.queries``: its Runs, Orders, Fills and
-- Positions) filters by mode and backtest id. Without these indexes each read
-- scans every backtest's rows in the mode's partition, so a backtest slows as the
-- Ledger fills with others (#79). The partitions inherit each index.
CREATE INDEX runs_account_idx ON runs (mode, backtest_id, as_of);
CREATE INDEX orders_account_idx ON orders (mode, backtest_id, day, seq);
CREATE INDEX fills_account_idx ON fills (mode, backtest_id, day, seq);
CREATE INDEX positions_account_idx ON positions (mode, backtest_id, position_id);
