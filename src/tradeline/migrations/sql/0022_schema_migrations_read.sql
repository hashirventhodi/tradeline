-- Which migrations are applied, readable by the application role for the
-- pending-migration check (#34); only ``migrate`` records one.
GRANT SELECT ON schema_migrations TO tradeline_app;
