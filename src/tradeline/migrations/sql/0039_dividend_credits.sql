-- Dividends credited in cash (ADR 0020, #92). When its Protocol credits them, a
-- Position held at the close before a dividend's Ex-Date is credited on the
-- Ex-Date (`day`) its `shares` then times the amount per share the NSE subject
-- states (`per_share`, every dividend the subject names summed): `cash`, in
-- rupees, part of the account's cash (`queries.account_cash_flows`). A subject
-- that cannot be read credits nothing and is recorded with its `reason`
-- (`tradeline.dividends`), never dropped. Shaped as 0030's: tagged by Engine
-- Mode, a backtest id exactly in backtest mode, the dry-run marker, `day` and
-- `seq` ordering it among the account's events; partitioned by mode.
--
-- Append-only: the application role may read and insert through the parent
-- table and nothing else; the partitions grant it nothing.
CREATE TABLE dividend_credits (
    mode        text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id uuid,
    dry_run     boolean NOT NULL DEFAULT false,
    day         date NOT NULL,
    seq         integer NOT NULL,
    position_id uuid NOT NULL,
    instrument  text NOT NULL,
    symbol      text NOT NULL,
    ex_date     date NOT NULL,
    subject     text NOT NULL,
    shares      integer NOT NULL CHECK (shares > 0),
    per_share   numeric CHECK (per_share > 0),
    cash        numeric CHECK (cash > 0),
    reason      text,
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    CHECK (NOT dry_run OR mode = 'paper'),
    CHECK ((reason IS NULL) = (per_share IS NOT NULL) AND (per_share IS NULL) = (cash IS NULL)),
    PRIMARY KEY (mode, position_id, ex_date, subject)
) PARTITION BY LIST (mode);

CREATE TABLE dividend_credits_backtest PARTITION OF dividend_credits FOR VALUES IN ('backtest');
CREATE TABLE dividend_credits_paper PARTITION OF dividend_credits FOR VALUES IN ('paper');
CREATE TABLE dividend_credits_live PARTITION OF dividend_credits FOR VALUES IN ('live');

CREATE INDEX dividend_credits_account_idx ON dividend_credits (mode, backtest_id, day, seq);

GRANT SELECT, INSERT ON dividend_credits TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON dividend_credits FROM tradeline_app;
