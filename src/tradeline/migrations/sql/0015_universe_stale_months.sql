-- Universe months a rebuild of the Instruments made stale (ADR 0016): a stored
-- month under a rule that ranks Instruments, whose window held a (Symbol, day)
-- the rebuild moved to another Instrument. The stored membership is kept as it
-- was; recomputing it is the Operator's deliberate act. A month is recorded
-- once per rule version, by the rebuild that first found it (its run id in
-- ingest_runs), and stays stale. Append-only, unlike the Instrument tables a
-- rebuild replaces.
CREATE TABLE universe_stale_months (
    month        date NOT NULL CHECK (extract(day FROM month) = 1),
    rule_version text NOT NULL REFERENCES universe_rule_versions (version),
    found_by     uuid NOT NULL,
    PRIMARY KEY (month, rule_version)
);

GRANT SELECT, INSERT ON universe_stale_months TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON universe_stale_months FROM tradeline_app;
