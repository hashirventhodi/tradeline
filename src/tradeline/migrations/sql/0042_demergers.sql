-- The demerger table and the Demerger Credits it gives a holder (ADR 0020, #95).
--
-- `demergers` holds the reviewed rows of the checked-in table
-- (`tradeline.demergers`), each `ingest demergers` run storing every row under
-- its ingest run id. The table in force is the latest successful run's rows
-- (`queries.demergers`), so an edit is a new run, never an update; a Protocol
-- crediting demergers freezes their hash. `ratio` is child shares per parent
-- share; the basis columns are the announced apportionment of the parent's
-- cost, in percent (100 and 0 when none was announced). Append-only.
CREATE TABLE demergers (
    ingest_run_id     uuid NOT NULL,
    line              integer NOT NULL,
    parent            text NOT NULL,
    child             text NOT NULL,
    ratio_numerator   integer NOT NULL CHECK (ratio_numerator > 0),
    ratio_denominator integer NOT NULL CHECK (ratio_denominator > 0),
    ex_date           date NOT NULL,
    listed_on         date NOT NULL CHECK (listed_on >= ex_date),
    parent_basis      numeric NOT NULL CHECK (parent_basis >= 0),
    child_basis       numeric NOT NULL CHECK (child_basis >= 0),
    citation          text NOT NULL CHECK (citation <> ''),
    CHECK (parent_basis + child_basis = 100),
    PRIMARY KEY (ingest_run_id, parent, child, ex_date)
);

GRANT SELECT, INSERT ON demergers TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON demergers FROM tradeline_app;

-- A Demerger Credit: when its Protocol credits demergers, a Position held at the
-- close before the Ex-Date (`shares` then, the parent's `parent` Symbol and
-- `instrument`) is credited on the child's first listing day (`day`) its
-- entitlement (`shares` times the ratio) times the child's close that day in the
-- Raw Series (`close`): `cash`, in rupees, part of the account's cash
-- (`queries.account_cash_flows`). `basis` is the child's share of the Position's
-- basis, moved from the Position to the credit (`queries.position_bases`); the
-- tax ledger takes the credit as a sale against it, held since `bought`, the
-- Position's opening day. Shaped as 0039's: tagged by Engine Mode, a backtest id
-- exactly in backtest mode, the dry-run marker, `day` and `seq` ordering it among
-- the account's events; partitioned by mode. Append-only.
CREATE TABLE demerger_credits (
    mode              text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id       uuid,
    dry_run           boolean NOT NULL DEFAULT false,
    day               date NOT NULL,
    seq               integer NOT NULL,
    position_id       uuid NOT NULL,
    instrument        text NOT NULL,
    parent            text NOT NULL,
    child             text NOT NULL,
    ex_date           date NOT NULL,
    bought            date NOT NULL,
    shares            integer NOT NULL CHECK (shares > 0),
    ratio_numerator   integer NOT NULL CHECK (ratio_numerator > 0),
    ratio_denominator integer NOT NULL CHECK (ratio_denominator > 0),
    close             numeric NOT NULL CHECK (close > 0),
    cash              numeric NOT NULL CHECK (cash >= 0),
    basis             numeric NOT NULL CHECK (basis >= 0),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    CHECK (NOT dry_run OR mode = 'paper'),
    CHECK (day >= ex_date),
    PRIMARY KEY (mode, position_id, ex_date, child)
) PARTITION BY LIST (mode);

CREATE TABLE demerger_credits_backtest PARTITION OF demerger_credits FOR VALUES IN ('backtest');
CREATE TABLE demerger_credits_paper PARTITION OF demerger_credits FOR VALUES IN ('paper');
CREATE TABLE demerger_credits_live PARTITION OF demerger_credits FOR VALUES IN ('live');

CREATE INDEX demerger_credits_account_idx ON demerger_credits (mode, backtest_id, day, seq);

GRANT SELECT, INSERT ON demerger_credits TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON demerger_credits FROM tradeline_app;
