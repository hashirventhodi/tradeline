-- A split or bonus none of NSE's lists has, cited from NSE's circulars by a
-- reviewed row (#37; ADR 0006, amended): stored beside the listed actions, with
-- no list and the row's citation. Every row has exactly one of the two; a cited
-- row is always a split or bonus, since the row exists to adjust. Append-only,
-- as 0004 made the table.
ALTER TABLE corporate_actions
    ALTER COLUMN nse_list DROP NOT NULL,
    ADD COLUMN citation text,
    ADD CHECK ((nse_list IS NULL) = (citation IS NOT NULL)),
    ADD CHECK (citation IS NULL OR kind IN ('split', 'bonus'));
