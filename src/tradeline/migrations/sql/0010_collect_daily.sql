-- ``collect-daily`` runs several sources in one go. Every ingest run it makes
-- carries the collection's id, so the history can show what one day's
-- collection did, source by source. NULL for a run made on its own.
ALTER TABLE ingest_runs ADD COLUMN collection_id uuid;

CREATE INDEX ingest_runs_collection_idx ON ingest_runs (collection_id)
    WHERE collection_id IS NOT NULL;
CREATE INDEX ingest_runs_started_at_idx ON ingest_runs (started_at);
