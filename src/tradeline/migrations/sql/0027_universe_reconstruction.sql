-- Rule Validity (ADR 0005, 2026-10-24 amendment): the Operator's declaration that
-- a Universe Rule version is the rule in force from a date, and when it was
-- declared. Distinct from universe_rule_versions.recorded_at, when the version
-- was first computed. A later declaration with a later in_force_from supersedes
-- an earlier one from that date; nothing is ever withdrawn. Append-only.
CREATE TABLE universe_rule_validity (
    rule_version  text NOT NULL REFERENCES universe_rule_versions (version),
    in_force_from date NOT NULL,
    declared_at   timestamptz NOT NULL,
    PRIMARY KEY (rule_version, in_force_from, declared_at)
);

GRANT SELECT, INSERT ON universe_rule_validity TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON universe_rule_validity FROM tradeline_app;

-- One month's Universe build per (month, rule version): the window of end-of-day
-- rows it was ranked from (window_start to data_cutoff, the Data Cutoff being the
-- last trading day before the month), the nse_eod ingest runs that wrote rows in
-- that window, the last Instruments rebuild before it, and a SHA-256 of the
-- window's end-of-day rows and of the Instrument intervals they belong to. A
-- month stored before this table existed is backfilled; cutoff_inferred marks
-- one whose window no recorded ingest run covers, so its cutoff is read from the
-- rows there now. Written once, when the membership is first stored or
-- backfilled; a later run that finds different inputs reports them and keeps
-- this row. Append-only.
CREATE TABLE universe_month_builds (
    month              date NOT NULL CHECK (extract(day FROM month) = 1),
    rule_version       text NOT NULL REFERENCES universe_rule_versions (version),
    window_start       date NOT NULL,
    data_cutoff        date NOT NULL CHECK (data_cutoff >= window_start AND data_cutoff < month),
    eod_run_ids        uuid[] NOT NULL,
    instruments_run_id uuid,
    eod_hash           text NOT NULL CHECK (eod_hash ~ '^[0-9a-f]{64}$'),
    links_hash         text NOT NULL CHECK (links_hash ~ '^[0-9a-f]{64}$'),
    cutoff_inferred    boolean NOT NULL,
    built_at           timestamptz NOT NULL,
    PRIMARY KEY (month, rule_version)
);

GRANT SELECT, INSERT ON universe_month_builds TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON universe_month_builds FROM tradeline_app;
