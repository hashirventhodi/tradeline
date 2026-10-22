-- The stored Cited Actions NSE's list supersedes, as the last Instruments
-- rebuild found them (#45; ADR 0006, amended by #37): one row per Symbol and
-- Ex-Date, with the listed actions and both factors, for the Operator to review
-- where they differ. Derived like instrument_anomalies, so replaced whole by
-- every rebuild.
CREATE TABLE cited_action_supersessions (
    symbol             text NOT NULL,
    ex_date            date NOT NULL,
    subjects           text[] NOT NULL,
    -- NSE's listed actions within seven days, the two arrays in step.
    listed_ex_dates    date[] NOT NULL,
    listed_subjects    text[] NOT NULL,
    cited_numerator    bigint NOT NULL,
    cited_denominator  bigint NOT NULL,
    listed_numerator   bigint NOT NULL,
    listed_denominator bigint NOT NULL,
    PRIMARY KEY (symbol, ex_date)
);

GRANT SELECT, INSERT, DELETE ON cited_action_supersessions TO tradeline_app;
