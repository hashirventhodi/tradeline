-- Gives stored corporate actions the kinds #15 adds (ADR 0006, amended by #15),
-- as 0004 says a parser fix is applied: a deliberate migration by the owner.
-- Demergers, schemes of arrangement or amalgamation, and capital reductions are
-- Flagged Actions; buybacks are recorded under their own kind. All were stored
-- as "other". The same reading fixes dividends whose subjects NSE misspelled
-- ("Divdend", "Divided", "Meetingdividend" run together), also stored as
-- "other".
--
-- Every action that is neither a split nor a bonus is read again from its
-- subject as tradeline.adjust.interpret now reads it, in its order of
-- precedence: demerger, scheme, capital reduction, rights, dividend, buyback.
-- Splits and bonuses, the factors, and every other column are unchanged.
ALTER TABLE corporate_actions DROP CONSTRAINT IF EXISTS corporate_actions_kind_check;
ALTER TABLE corporate_actions ADD CONSTRAINT corporate_actions_kind_check
    CHECK (kind IN ('split', 'bonus', 'demerger', 'scheme', 'capital_reduction',
                    'rights', 'dividend', 'buyback', 'other'));

UPDATE corporate_actions
SET kind = CASE
    WHEN subject ~* '\mde-?\s*merger' THEN 'demerger'
    -- "Scheme Of", NSE's "Sheme Of"; not a fund's "Conversion Of Etf Into Index Scheme".
    WHEN subject ~* '\msc?heme\s+of\M|\mamalgamation\M|\mmerger\M|\mreconstruction\M'
        THEN 'scheme'
    WHEN subject ~* '\mcapital\s*reduction\M' THEN 'capital_reduction'
    WHEN subject ~* '\mrights?\M' THEN 'rights'
    WHEN subject ~* '\mdiv\M|div(idend|dend|ided|idned|ivend)|\mdistribution\M' THEN 'dividend'
    WHEN subject ~* '\mbuy\s*back\M' THEN 'buyback'
    ELSE 'other'
END
WHERE kind IN ('rights', 'dividend', 'other');
