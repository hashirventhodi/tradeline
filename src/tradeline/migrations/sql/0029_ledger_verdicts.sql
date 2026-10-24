-- The Risk Engine's Ledger tables (ADR 0019, #76): a Pass or a Block for each
-- Plan of a Run, and the Verdict on each Candidate. Shaped as 0028's: every
-- row carries its Engine Mode and, in backtest mode alone, a backtest id;
-- each table is partitioned by mode.
--
-- Append-only: the application role may read and insert through the parent
-- tables and nothing else; the partitions grant it nothing.

-- A Pass: the Risk Engine's approval of one Plan, priced on the tick and sized,
-- bound to the Plan's content hash and the Run. `sector` is the one the sector
-- Limit read (`unclassified` when none was recorded); `facts` holds Equity,
-- the tick, the 20-day average traded value, and the shares each sizing Limit
-- allowed.
CREATE TABLE passes (
    mode           text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id    uuid,
    run_id         uuid NOT NULL,
    rank           integer NOT NULL,
    plan_hash      text NOT NULL,
    entry          numeric NOT NULL,
    stop           numeric NOT NULL,
    target         numeric NOT NULL,
    time_stop_days integer NOT NULL,
    shares         integer NOT NULL CHECK (shares > 0),
    sector         text NOT NULL,
    facts          jsonb NOT NULL,
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, run_id, rank),
    FOREIGN KEY (mode, run_id, rank) REFERENCES plans (mode, run_id, rank)
) PARTITION BY LIST (mode);

-- A Block: the Risk Engine's refusal of one Plan, naming the Limit, with the
-- Plan as far as it was priced and sized.
CREATE TABLE blocks (
    mode           text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id    uuid,
    run_id         uuid NOT NULL,
    rank           integer NOT NULL,
    limit_name     text NOT NULL CHECK (limit_name IN (
        'stop_required', 'min_reward_to_risk', 'max_new_entries', 'max_open_positions',
        'max_position', 'max_sector', 'max_gross', 'participation_cap', 'min_order_value'
    )),
    entry          numeric NOT NULL,
    stop           numeric NOT NULL,
    target         numeric NOT NULL,
    time_stop_days integer NOT NULL,
    shares         integer NOT NULL CHECK (shares >= 0),
    sector         text NOT NULL,
    facts          jsonb NOT NULL,
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, run_id, rank),
    FOREIGN KEY (mode, run_id, rank) REFERENCES plans (mode, run_id, rank)
) PARTITION BY LIST (mode);

-- The Verdict on one Candidate of a Run: BUY (its Plan has a Pass) or SKIP (a
-- Block), with the reason: `pass`, or the Limit named. WAIT and EXIT are later
-- phases' Verdicts.
CREATE TABLE verdicts (
    mode        text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id uuid,
    run_id      uuid NOT NULL,
    rank        integer NOT NULL,
    verdict     text NOT NULL CHECK (verdict IN ('BUY', 'SKIP', 'WAIT', 'EXIT')),
    reason      text NOT NULL,
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, run_id, rank),
    FOREIGN KEY (mode, run_id, rank) REFERENCES candidates (mode, run_id, rank)
) PARTITION BY LIST (mode);

CREATE TABLE passes_backtest PARTITION OF passes FOR VALUES IN ('backtest');
CREATE TABLE passes_paper PARTITION OF passes FOR VALUES IN ('paper');
CREATE TABLE passes_live PARTITION OF passes FOR VALUES IN ('live');
CREATE TABLE blocks_backtest PARTITION OF blocks FOR VALUES IN ('backtest');
CREATE TABLE blocks_paper PARTITION OF blocks FOR VALUES IN ('paper');
CREATE TABLE blocks_live PARTITION OF blocks FOR VALUES IN ('live');
CREATE TABLE verdicts_backtest PARTITION OF verdicts FOR VALUES IN ('backtest');
CREATE TABLE verdicts_paper PARTITION OF verdicts FOR VALUES IN ('paper');
CREATE TABLE verdicts_live PARTITION OF verdicts FOR VALUES IN ('live');

GRANT SELECT, INSERT ON passes, blocks, verdicts TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON passes, blocks, verdicts FROM tradeline_app;
