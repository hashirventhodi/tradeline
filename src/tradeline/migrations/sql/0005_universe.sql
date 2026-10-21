-- Universe Rule versions: one row per distinct parameter set, keyed by the
-- SHA-256 of the parameters as canonical JSON (tradeline.config.UniverseRule).
-- A run under parameters already recorded writes nothing here. Append-only.
CREATE TABLE universe_rule_versions (
    version     text PRIMARY KEY CHECK (version ~ '^[0-9a-f]{64}$'),
    parameters  jsonb NOT NULL,
    recorded_at timestamptz NOT NULL
);

GRANT SELECT, INSERT ON universe_rule_versions TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON universe_rule_versions FROM tradeline_app;

-- Universe membership: one row per member per (month, rule version), with its
-- rank and its traded value over the rule's trailing window. ``month`` is the
-- first calendar day of the month. A month is written once per rule version;
-- a later run that computes something different keeps the stored rows and
-- reports an anomaly. Append-only.
CREATE TABLE universe_membership (
    month        date NOT NULL CHECK (extract(day FROM month) = 1),
    rule_version text NOT NULL REFERENCES universe_rule_versions (version),
    rank         integer NOT NULL CHECK (rank >= 1),
    symbol       text NOT NULL,
    series       text NOT NULL,
    traded_value numeric NOT NULL,
    PRIMARY KEY (month, rule_version, symbol),
    UNIQUE (month, rule_version, rank)
);

GRANT SELECT, INSERT ON universe_membership TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON universe_membership FROM tradeline_app;
