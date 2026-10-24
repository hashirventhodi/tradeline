-- The first Ledger tables (ADR 0019): Strategy versions, Runs, the Candidates a
-- Run's screen surfaced, and the Plans written for them. There is one set of
-- tables for every Engine Mode: each row carries its mode (backtest, paper,
-- live) and, in backtest mode, the id of the backtest it belongs to. A CHECK
-- requires a backtest id exactly when the mode is backtest. Each table is
-- partitioned by mode so a full Protocol's backtest volume sits apart from the
-- paper and live rows.
--
-- Append-only: the application role may read and insert through the parent
-- tables and nothing else. The partitions grant it nothing at all, so no row can
-- be changed by naming a partition either.

-- A Strategy version as a mode's rows name it: family, name, the code revision
-- (a hash of the `strategy` package's parsed code, tradeline.revision), and the
-- Protocol it runs under, absent until Protocols exist (#81). The ranking rule
-- is stated so a Candidate's rank can be read without the code.
CREATE TABLE strategies (
    mode          text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id   uuid,
    id            uuid NOT NULL DEFAULT gen_random_uuid(),
    family        text NOT NULL,
    name          text NOT NULL,
    code_revision text NOT NULL,
    protocol      text,
    ranking       text NOT NULL,
    recorded_at   timestamptz NOT NULL DEFAULT now(),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, id),
    UNIQUE NULLS NOT DISTINCT (mode, backtest_id, name, code_revision, protocol)
) PARTITION BY LIST (mode);

-- One Run: a Strategy version's decision cycle as of one trading day over that
-- month's Universe under one rule version, with the parameters it ran at.
CREATE TABLE runs (
    mode                  text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id           uuid,
    id                    uuid NOT NULL DEFAULT gen_random_uuid(),
    strategy_id           uuid NOT NULL,
    as_of                 date NOT NULL,
    universe_month        date NOT NULL,
    universe_rule_version text NOT NULL,
    universe_size         integer NOT NULL,
    parameters            jsonb NOT NULL,
    recorded_at           timestamptz NOT NULL DEFAULT now(),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, id),
    FOREIGN KEY (mode, strategy_id) REFERENCES strategies (mode, id)
) PARTITION BY LIST (mode);

-- A Candidate: an Instrument the Run's screen surfaced, at its rank (1 first)
-- with the score it was ranked by and the indicator values the screen read.
CREATE TABLE candidates (
    mode        text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id uuid,
    run_id      uuid NOT NULL,
    rank        integer NOT NULL CHECK (rank > 0),
    instrument  text NOT NULL,
    symbol      text NOT NULL,
    score       numeric NOT NULL,
    facts       jsonb NOT NULL,
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, run_id, rank),
    UNIQUE (mode, run_id, instrument),
    FOREIGN KEY (mode, run_id) REFERENCES runs (mode, id)
) PARTITION BY LIST (mode);

-- A Plan for one Candidate: entry, stop, target and the time stop in trading
-- days, as the Strategy priced them (unrounded; Plan pricing to the tick and
-- sizing are the Risk Engine's, #76).
CREATE TABLE plans (
    mode           text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id    uuid,
    run_id         uuid NOT NULL,
    rank           integer NOT NULL,
    instrument     text NOT NULL,
    symbol         text NOT NULL,
    entry          numeric NOT NULL,
    stop           numeric NOT NULL,
    target         numeric NOT NULL,
    time_stop_days integer NOT NULL CHECK (time_stop_days > 0),
    facts          jsonb NOT NULL,
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, run_id, rank),
    FOREIGN KEY (mode, run_id, rank) REFERENCES candidates (mode, run_id, rank)
) PARTITION BY LIST (mode);

CREATE TABLE strategies_backtest PARTITION OF strategies FOR VALUES IN ('backtest');
CREATE TABLE strategies_paper PARTITION OF strategies FOR VALUES IN ('paper');
CREATE TABLE strategies_live PARTITION OF strategies FOR VALUES IN ('live');
CREATE TABLE runs_backtest PARTITION OF runs FOR VALUES IN ('backtest');
CREATE TABLE runs_paper PARTITION OF runs FOR VALUES IN ('paper');
CREATE TABLE runs_live PARTITION OF runs FOR VALUES IN ('live');
CREATE TABLE candidates_backtest PARTITION OF candidates FOR VALUES IN ('backtest');
CREATE TABLE candidates_paper PARTITION OF candidates FOR VALUES IN ('paper');
CREATE TABLE candidates_live PARTITION OF candidates FOR VALUES IN ('live');
CREATE TABLE plans_backtest PARTITION OF plans FOR VALUES IN ('backtest');
CREATE TABLE plans_paper PARTITION OF plans FOR VALUES IN ('paper');
CREATE TABLE plans_live PARTITION OF plans FOR VALUES IN ('live');

GRANT SELECT, INSERT ON strategies, runs, candidates, plans TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON strategies, runs, candidates, plans FROM tradeline_app;
