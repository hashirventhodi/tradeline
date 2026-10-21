-- NSE's list of delisted securities, one row per (Symbol, delisting date) as
-- NSE publishes it: the company name and the kind of delisting NSE names
-- (voluntary, compulsory, or on liquidation). The official source behind an
-- Instrument's end reason "delisted" (#26). Append-only, like the other
-- sources: NSE republishes the whole list, a record is inserted the first time
-- it is seen, and the application role cannot update or delete it.
CREATE TABLE delistings (
    symbol       text NOT NULL,
    delisted_on  date NOT NULL,
    company_name text NOT NULL,
    kind         text NOT NULL,
    PRIMARY KEY (symbol, delisted_on)
);

GRANT SELECT, INSERT ON delistings TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON delistings FROM tradeline_app;
