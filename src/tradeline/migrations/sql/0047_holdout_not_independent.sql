-- A first version can be Not Independent (ADR 0007, 2026-10-25 amendment): when its
-- family was chosen after reading published results covering the Holdout's years, its
-- Protocol states why (`not_independent`), and its Holdout run records that reason here.
-- The run still consumes the family's Holdout, once, and is Not Independent: it passes
-- the Backtest Gate only with a Fresh Period. `independent` is regenerated to read it.
ALTER TABLE holdout_attempts ADD COLUMN not_independent text;
ALTER TABLE holdout_attempts ADD CHECK (outcome = 'ran' OR not_independent IS NULL);
ALTER TABLE holdout_attempts DROP COLUMN independent;
ALTER TABLE holdout_attempts ADD COLUMN independent boolean GENERATED ALWAYS AS (
    consumes AND cardinality(corrected_since) = 0 AND not_independent IS NULL
) STORED;
