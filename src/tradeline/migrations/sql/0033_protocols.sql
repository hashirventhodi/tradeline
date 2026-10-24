-- Protocols (ADR 0007, 2026-10-24 amendment): how a Strategy version is
-- validated, frozen before any run. One row per Protocol, keyed by the SHA-256
-- of its canonical serialisation (tradeline.protocol: sorted keys, fixed
-- decimals), which is stored as it was hashed; the CHECK holds the key to the
-- content, so no row can claim a hash its content does not have. A Protocol
-- belongs to no Engine Mode: it is frozen before any backtest it governs.
-- Append-only.
CREATE TABLE protocols (
    hash      text PRIMARY KEY CHECK (hash ~ '^[0-9a-f]{64}$'),
    content   text NOT NULL,
    frozen_at timestamptz NOT NULL DEFAULT now(),
    CHECK (hash = encode(sha256(convert_to(content, 'UTF8')), 'hex'))
);

GRANT SELECT, INSERT ON protocols TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON protocols FROM tradeline_app;

-- A Strategy version names the Protocol it runs under, which must be frozen.
ALTER TABLE strategies ADD FOREIGN KEY (protocol) REFERENCES protocols (hash);
