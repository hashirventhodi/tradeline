-- NSE's corporate actions, one row per (symbol, series, ex-date, subject) as
-- NSE's list states them, with the kind and the price factor we derive from
-- the subject (see tradeline.adjust). The factor is an exact fraction (a 1:5
-- bonus is 5/6), stored as numerator and denominator; the generated ``factor``
-- is an approximation for reading by hand. Both are NULL for actions that are
-- not adjusted. Append-only, like the end-of-day rows: a row is inserted once
-- and the application role cannot update or delete it. A later run that derives
-- a different kind or factor for a stored row (a parser fix) records an anomaly
-- and keeps the row; applying the fix is a deliberate migration by the owner.
CREATE TABLE corporate_actions (
    symbol             text NOT NULL,
    series             text NOT NULL,
    ex_date            date NOT NULL,
    subject            text NOT NULL,
    isin               text,
    record_date        date,
    face_value         numeric,
    kind               text NOT NULL
                       CHECK (kind IN ('split', 'bonus', 'rights', 'dividend', 'other')),
    factor_numerator   bigint CHECK (factor_numerator > 0),
    factor_denominator bigint CHECK (factor_denominator > 0),
    factor             numeric GENERATED ALWAYS AS
                       (factor_numerator::numeric / factor_denominator) STORED,
    CHECK ((factor_numerator IS NULL) = (factor_denominator IS NULL)),
    CHECK ((kind IN ('split', 'bonus')) = (factor_numerator IS NOT NULL)),
    PRIMARY KEY (symbol, series, ex_date, subject)
);

GRANT SELECT, INSERT ON corporate_actions TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON corporate_actions FROM tradeline_app;
