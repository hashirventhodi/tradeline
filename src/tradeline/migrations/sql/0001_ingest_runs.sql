-- The application role. Roles are cluster-wide, so create it only if missing
-- and tolerate a concurrent creation. The connecting (owner) user is made a
-- member so that it can SET ROLE to it.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'tradeline_app') THEN
        BEGIN
            CREATE ROLE tradeline_app NOLOGIN;
        EXCEPTION WHEN duplicate_object THEN
            NULL;
        END;
    END IF;
END
$$;

GRANT tradeline_app TO CURRENT_USER;
GRANT USAGE ON SCHEMA public TO tradeline_app;

-- Every ingestion run, written once when the run finishes; the id is generated
-- by the application at start so log lines can carry it. Append-only.
CREATE TABLE ingest_runs (
    id            uuid PRIMARY KEY,
    command       text NOT NULL,
    source        text NOT NULL,
    range_start   date,
    range_end     date,
    started_at    timestamptz NOT NULL,
    finished_at   timestamptz NOT NULL,
    rows_written  integer NOT NULL DEFAULT 0,
    error         text,
    CHECK (finished_at >= started_at)
);

GRANT SELECT, INSERT ON ingest_runs TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON ingest_runs FROM tradeline_app;
