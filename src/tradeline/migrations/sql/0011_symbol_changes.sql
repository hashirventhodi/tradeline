-- NSE's symbol-change list, one row per (old Symbol, new Symbol, effective
-- date) as NSE publishes it, with the company name NSE gives (NULL where it
-- gives none). The evidence behind every rename link (ADR 0016). Append-only,
-- like the other sources: NSE republishes the whole list as it grows, a record
-- is inserted the first time it is seen, and the application role cannot
-- update or delete it.
CREATE TABLE symbol_changes (
    old_symbol     text NOT NULL,
    new_symbol     text NOT NULL,
    effective_date date NOT NULL,
    company_name   text,
    PRIMARY KEY (old_symbol, new_symbol, effective_date)
);

CREATE INDEX symbol_changes_new_symbol_idx ON symbol_changes (new_symbol);

GRANT SELECT, INSERT ON symbol_changes TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON symbol_changes FROM tradeline_app;
