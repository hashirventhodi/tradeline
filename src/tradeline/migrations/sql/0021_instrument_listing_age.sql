-- The day each Instrument's Listing Age starts (ADR 0017): the earlier of its
-- listing date and its first end-of-day row in any series (its listing date
-- alone where it has no row). The one statement of
-- it that the Universe Rule (``listing_age_from = "earliest_evidence"``) and
-- reconcile's Pre-listing Candles (#34) both read, so they cannot drift apart. A
-- view over the derived Instrument tables, so it follows every rebuild.
CREATE VIEW instrument_listing_age AS
SELECT
    i.key AS instrument_key,
    LEAST(i.listed_on, min(v.first_day)) AS listing_age_from
FROM instruments i
LEFT JOIN instrument_intervals v ON v.instrument_key = i.key
GROUP BY i.key, i.listed_on;

GRANT SELECT ON instrument_listing_age TO tradeline_app;
