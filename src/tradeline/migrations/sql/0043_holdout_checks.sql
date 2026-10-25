-- Pre-Holdout checks (ADR 0007, 2026-10-25 amendment; #102): one row each time
-- one of the machinery checks runs for a Protocol, from records, not results,
-- at the git commit the process ran (`git_commit`). The Holdout of `protocol`
-- runs only when the latest row of each `item` at the commit it runs at passed.
--
-- `item`: 'contract_notes' (every real contract note matches the rates in
-- force on its trade date; with none supplied it is unmet unless `accepted`
-- states why it is accepted as unmet), 'dividends' (every dividend subject of
-- every eligible member parsed or is in the reviewed list), 'demergers' (every
-- demerger in NSE's list for an eligible member has a demerger table row) and
-- 'tax_tests' (the tax ledger's synthetic tests passed at the commit).
-- `summary` is the one line naming why it failed or what it counted; `detail`
-- holds what it read. Like `holdout_attempts`, no Engine Mode. Append-only.
CREATE TABLE holdout_checks (
    id          uuid PRIMARY KEY,
    protocol    text NOT NULL REFERENCES protocols (hash),
    git_commit  text NOT NULL,
    item        text NOT NULL
        CHECK (item IN ('contract_notes', 'dividends', 'demergers', 'tax_tests')),
    checked_at  timestamptz NOT NULL DEFAULT clock_timestamp(),
    passed      boolean NOT NULL,
    accepted    text,
    summary     text NOT NULL,
    detail      jsonb NOT NULL,
    CHECK (accepted IS NULL OR item = 'contract_notes')
);

CREATE INDEX holdout_checks_by_protocol ON holdout_checks (protocol, git_commit, item, checked_at);

GRANT SELECT, INSERT ON holdout_checks TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON holdout_checks FROM tradeline_app;
