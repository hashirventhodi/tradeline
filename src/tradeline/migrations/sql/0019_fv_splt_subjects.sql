-- Applies a parser fix to stored corporate actions (#29), as 0004 says a fix
-- is applied: a deliberate migration by the owner. From 2016 to 2018 NSE wrote
-- some face value splits as "Fv Splt Frm Rs 10 To Re 1", which
-- tradeline.adjust.interpret did not read, so they were stored as "other"
-- without a factor. The #13 database holds 14 (JSWSTEEL, KARURVYSYA,
-- SOLARINDS, ...).
--
-- A subject that is wholly that short form is re-derived as interpret now
-- reads it: "Rs" or "Re" with an optional dot, any spacing, decimal face
-- values, an optional "/-"; a split whose factor is the new face value over the
-- old, in lowest terms. A short form inside a longer subject is left as stored,
-- since interpret may compound it with another action; were one stored, the
-- next ingest of its range would report it as derived differently. Every other
-- column, and every other row, is unchanged.
WITH short_form AS (
    SELECT symbol, series, ex_date, subject,
           (regexp_match(
               subject,
               '^\s*fv\s*splt\s*frm\s*r[se]\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?'
               '\s*to\s*r[se]\.?\s*(\d+(?:\.\d+)?)\s*(?:/-)?\s*$',
               'i'))::numeric[] AS fv
    FROM corporate_actions
    WHERE kind = 'other'
),
-- Both face values as whole numbers over one power of ten, so the factor is exact.
scaled AS (
    SELECT symbol, series, ex_date, subject,
           (fv[1] * 10 ^ greatest(scale(fv[1]), scale(fv[2])))::bigint AS old_fv,
           (fv[2] * 10 ^ greatest(scale(fv[1]), scale(fv[2])))::bigint AS new_fv
    FROM short_form
    WHERE fv IS NOT NULL
)
UPDATE corporate_actions c
SET kind = 'split',
    factor_numerator = s.new_fv / gcd(s.old_fv, s.new_fv),
    factor_denominator = s.old_fv / gcd(s.old_fv, s.new_fv)
FROM scaled s
WHERE (c.symbol, c.series, c.ex_date, c.subject) = (s.symbol, s.series, s.ex_date, s.subject);
