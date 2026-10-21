-- Link candidates, derived by the link builder with the other Instrument
-- tables and replaced with them on every rebuild: a new Symbol under an ISIN
-- an earlier Symbol had, with no record joining them. Never a link; the
-- Operator reviews each (`tradeline instruments`) and cites a manual link or
-- leaves the history split.
CREATE TABLE instrument_link_candidates (
    old_symbol text NOT NULL,
    new_symbol text NOT NULL,
    isin       text NOT NULL,
    old_last   date NOT NULL,
    new_first  date NOT NULL CHECK (new_first > old_last)
);

GRANT SELECT, INSERT, DELETE ON instrument_link_candidates TO tradeline_app;
