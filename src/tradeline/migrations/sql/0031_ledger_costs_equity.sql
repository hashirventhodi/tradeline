-- Costs, Equity and Corporate Actions on held Positions in the Ledger (#78).
--
-- A Fill carries its charges itemised (`tradeline.costs`): STT, stamp duty,
-- the exchange's transaction charge, SEBI's turnover fee, GST on those two,
-- the DP charge and GST on it, and Slippage, all in rupees. Fills written
-- before the cost model have none; a Fill has every item or none.
ALTER TABLE fills
    ADD COLUMN stt numeric CHECK (stt >= 0),
    ADD COLUMN stamp_duty numeric CHECK (stamp_duty >= 0),
    ADD COLUMN exchange_charge numeric CHECK (exchange_charge >= 0),
    ADD COLUMN sebi_fee numeric CHECK (sebi_fee >= 0),
    ADD COLUMN gst numeric CHECK (gst >= 0),
    ADD COLUMN dp_charge numeric CHECK (dp_charge >= 0),
    ADD COLUMN dp_gst numeric CHECK (dp_gst >= 0),
    ADD COLUMN slippage numeric CHECK (slippage >= 0),
    ADD CONSTRAINT fills_charges_together CHECK (
        num_nulls(stt, stamp_duty, exchange_charge, sebi_fee, gst, dp_charge, dp_gst, slippage)
        IN (0, 8)
    );

-- A Run's account: `cash` (the starting Equity less every buy and its charges,
-- plus every sell less its charges and any cash paid in lieu of a fractional
-- share) and `equity` (cash plus the open Positions at the day's adjusted
-- close), both after the day's session. Runs written before #78 have neither.
ALTER TABLE runs
    ADD COLUMN cash numeric,
    ADD COLUMN equity numeric,
    ADD CONSTRAINT runs_account_together CHECK ((cash IS NULL) = (equity IS NULL));

-- A held Position is `restated` on a split or bonus Ex-Date: shares divided by
-- the Adjustment Factor (`factor`, the product of the day's actions, as a
-- fraction such as 1/2), entry, stop and target multiplied by it, so Equity is
-- unchanged and the exits keep their meaning in the Adjusted Series. Shares
-- that come out fractional are rounded down and the fraction paid in cash at
-- the restated previous close (`cash_in_lieu`, rupees; 0 when none).
ALTER TABLE positions DROP CONSTRAINT positions_event_check;
ALTER TABLE positions
    ADD CONSTRAINT positions_event_check CHECK (event IN ('opened', 'restated', 'closed')),
    ADD COLUMN factor text CHECK (factor ~ '^[1-9][0-9]*/[1-9][0-9]*$'),
    ADD COLUMN cash_in_lieu numeric CHECK (cash_in_lieu >= 0),
    ADD CONSTRAINT positions_restated_with_factor CHECK (
        (event = 'restated') = (factor IS NOT NULL)
        AND (event = 'restated') = (cash_in_lieu IS NOT NULL)
    );

-- Each Position as it stands: its latest event's row (now with the new columns).
DROP VIEW current_positions;
CREATE VIEW current_positions AS
SELECT DISTINCT ON (mode, position_id) *
FROM positions
ORDER BY mode, position_id, day DESC, seq DESC;
GRANT SELECT ON current_positions TO tradeline_app;
