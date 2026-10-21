-- The Instruments, derived by the link builder (tradeline.instruments) from the
-- stored evidence. These tables hold no evidence of their own: every rebuild
-- replaces all of them in one transaction, so they are not append-only. The
-- evidence tables (end-of-day rows, symbol changes, corporate actions, the
-- symbol master) are never written by a rebuild.
--
-- An Instrument's key is its first Symbol and its listing date
-- (``ZOMATO@2021-07-23``); ``key_from`` says whether the date is NSE's listing
-- date or, where NSE gives none, the Instrument's first end-of-day row.
CREATE TABLE instruments (
    key          text PRIMARY KEY,
    first_symbol text NOT NULL,
    listed_on    date NOT NULL,
    key_from     text NOT NULL CHECK (key_from IN ('listing_date', 'first_row'))
);

-- An Instrument's history: each a run of consecutive end-of-day rows of one
-- Symbol in one series under one ISIN. A Symbol names at most one Instrument
-- on any day, so a Symbol's intervals never overlap across Instruments.
CREATE TABLE instrument_intervals (
    instrument_key text NOT NULL REFERENCES instruments (key) ON DELETE CASCADE,
    symbol         text NOT NULL,
    series         text NOT NULL,
    isin           text,
    first_day      date NOT NULL,
    last_day       date NOT NULL CHECK (last_day >= first_day),
    PRIMARY KEY (symbol, series, first_day)
);

CREATE INDEX instrument_intervals_key_idx ON instrument_intervals (instrument_key, first_day);
CREATE INDEX instrument_intervals_symbol_idx ON instrument_intervals (symbol, first_day);

-- The Instrument Links that joined an Instrument's stretches, each with the
-- evidence it came from.
CREATE TABLE instrument_links (
    instrument_key text NOT NULL REFERENCES instruments (key) ON DELETE CASCADE,
    kind           text NOT NULL,
    on_date        date NOT NULL,
    from_symbol    text NOT NULL,
    from_isin      text,
    to_symbol      text NOT NULL,
    to_isin        text,
    source         text NOT NULL
);

CREATE INDEX instrument_links_key_idx ON instrument_links (instrument_key, on_date);

-- What the builder would not join, for the Operator to review.
CREATE TABLE instrument_anomalies (
    kind    text NOT NULL,
    symbol  text NOT NULL,
    on_date date NOT NULL,
    detail  text NOT NULL
);

GRANT SELECT, INSERT, DELETE
    ON instruments, instrument_intervals, instrument_links, instrument_anomalies
    TO tradeline_app;
