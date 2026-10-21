-- NSE's sector classification, each row in force from its date: one row per
-- symbol per date on which its sector was first seen as stated. A load that
-- finds a symbol's sector unchanged writes nothing; one that finds it changed
-- writes a new row from the load's date, and the earlier row stays in force
-- for earlier dates. Append-only: the application role cannot update or
-- delete a row, so a classification once recorded is never rewritten.
CREATE TABLE sectors (
    symbol         text NOT NULL,
    effective_from date NOT NULL,
    sector         text NOT NULL CHECK (sector <> ''),
    isin           text,
    PRIMARY KEY (symbol, effective_from)
);

GRANT SELECT, INSERT ON sectors TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON sectors FROM tradeline_app;
