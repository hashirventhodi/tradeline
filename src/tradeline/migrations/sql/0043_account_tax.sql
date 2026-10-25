-- Tax in a backtest account, and its Liquidation Value against the Benchmark
-- (ADR 0015 amendment, #96). Written only under a Protocol that keeps tax.
--
-- `tax_debits`: each Indian financial year's net tax (`financial_year`, its
-- first calendar year), debited from cash on the first trading day after its
-- March 31, or, `settled`, on the day a Boundary Exit liquidates the account,
-- which settles the final partial year. The year's gains after every set-off and
-- its dividends are kept beside the tax; a settlement keeps the losses it left
-- unused, which are dropped. Part of the account's cash
-- (`queries.account_cash_flows`).
--
-- `liquidation_values`: the account's Liquidation Value on each day it Runs, after
-- the session: its cash before the day's tax debits, plus its open Positions sold
-- at the day's close after costs, less every tax owed to then; and the
-- Benchmark's, bought on the account's first day for its starting Equity
-- (`tradeline.benchmark`), NULL on an Initial Buy's day, before it is bought.
--
-- Shaped as 0039's: tagged by Engine Mode, a backtest id exactly in backtest
-- mode, the dry-run marker; partitioned by mode. Append-only: the application
-- role may read and insert through the parent tables and nothing else.
CREATE TABLE tax_debits (
    mode                     text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id              uuid,
    dry_run                  boolean NOT NULL DEFAULT false,
    id                       uuid NOT NULL,
    day                      date NOT NULL,
    financial_year           integer NOT NULL,
    short_term_gain          numeric NOT NULL CHECK (short_term_gain >= 0),
    long_term_gain           numeric NOT NULL CHECK (long_term_gain >= 0),
    dividends                numeric NOT NULL CHECK (dividends >= 0),
    tax                      numeric NOT NULL CHECK (tax >= 0),
    settled                  boolean NOT NULL,
    dropped_short_term_loss  numeric CHECK (dropped_short_term_loss >= 0),
    dropped_long_term_loss   numeric CHECK (dropped_long_term_loss >= 0),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    CHECK (NOT dry_run OR mode = 'paper'),
    CHECK (settled = (dropped_short_term_loss IS NOT NULL)
        AND settled = (dropped_long_term_loss IS NOT NULL)),
    PRIMARY KEY (mode, id)
) PARTITION BY LIST (mode);

CREATE TABLE tax_debits_backtest PARTITION OF tax_debits FOR VALUES IN ('backtest');
CREATE TABLE tax_debits_paper PARTITION OF tax_debits FOR VALUES IN ('paper');
CREATE TABLE tax_debits_live PARTITION OF tax_debits FOR VALUES IN ('live');

CREATE INDEX tax_debits_account_idx ON tax_debits (mode, backtest_id, day);

GRANT SELECT, INSERT ON tax_debits TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON tax_debits FROM tradeline_app;

CREATE TABLE liquidation_values (
    mode                   text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id            uuid,
    dry_run                boolean NOT NULL DEFAULT false,
    id                     uuid NOT NULL,
    day                    date NOT NULL,
    value                  numeric NOT NULL,
    tax_due                numeric NOT NULL CHECK (tax_due >= 0),
    dropped_short_term_loss numeric NOT NULL CHECK (dropped_short_term_loss >= 0),
    dropped_long_term_loss numeric NOT NULL CHECK (dropped_long_term_loss >= 0),
    benchmark_scheme       integer,
    benchmark_bought_on    date,
    benchmark_bought_nav   numeric CHECK (benchmark_bought_nav > 0),
    benchmark_nav          numeric CHECK (benchmark_nav > 0),
    benchmark_value        numeric,
    benchmark_tax_due      numeric CHECK (benchmark_tax_due >= 0),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    CHECK (NOT dry_run OR mode = 'paper'),
    CHECK (num_nulls(benchmark_scheme, benchmark_bought_on, benchmark_bought_nav,
        benchmark_nav, benchmark_value, benchmark_tax_due) IN (0, 6)),
    PRIMARY KEY (mode, id)
) PARTITION BY LIST (mode);

CREATE TABLE liquidation_values_backtest PARTITION OF liquidation_values
    FOR VALUES IN ('backtest');
CREATE TABLE liquidation_values_paper PARTITION OF liquidation_values FOR VALUES IN ('paper');
CREATE TABLE liquidation_values_live PARTITION OF liquidation_values FOR VALUES IN ('live');

CREATE INDEX liquidation_values_account_idx ON liquidation_values (mode, backtest_id, day);

GRANT SELECT, INSERT ON liquidation_values TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON liquidation_values FROM tradeline_app;
