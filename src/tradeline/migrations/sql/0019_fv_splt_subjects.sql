-- Applies a parser fix to stored corporate actions (#29), as 0004 says a fix
-- is applied: a deliberate migration by the owner. From 2016 to 2018 NSE wrote
-- some face value splits as "Fv Splt Frm Rs 10 To Re 1", which
-- tradeline.adjust.interpret did not read, so they were stored as "other"
-- without a factor. The #13 database holds 14 (JSWSTEEL, KARURVYSYA,
-- SOLARINDS, ...). Each is re-derived here as interpret now reads it: a split
-- whose factor is the new face value over the old, in lowest terms. Every
-- other column, and every other row, is unchanged.
WITH short_form AS (
    SELECT symbol, series, ex_date, subject,
           (regexp_match(subject, '^Fv Splt Frm R[se] (\d+) To R[se] (\d+)$', 'i'))::bigint[] AS fv
    FROM corporate_actions
    WHERE kind = 'other'
)
UPDATE corporate_actions c
SET kind = 'split',
    factor_numerator = s.fv[2] / gcd(s.fv[1], s.fv[2]),
    factor_denominator = s.fv[1] / gcd(s.fv[1], s.fv[2])
FROM short_form s
WHERE s.fv IS NOT NULL
  AND (c.symbol, c.series, c.ex_date, c.subject) = (s.symbol, s.series, s.ex_date, s.subject);
