-- Every app connection first checks for a pending migration (db.py, #34's
-- retro). A login role that is only a member of tradeline_app must be able to
-- read which migrations are applied; only ``migrate`` records one.
GRANT SELECT ON schema_migrations TO tradeline_app;
