-- Which Instrument a row stored under a Symbol belongs to, on any day: each
-- Symbol's days divided between the Instruments it named (ADR 0016). A
-- Symbol's run under one Instrument owns the days it named that Instrument and
-- every day before them back to the Symbol's previous run, and the Symbol's
-- last run owns every day after it too. So a day inside a run is that run's
-- Instrument's, and a day before any run is the next run's: Kite files an
-- Instrument's history from before a rename under its new name, which NSE never
-- used on those days. ``named_until`` is the run's last end-of-day row, so the
-- Symbol an Instrument was named last is the one with the latest.
--
-- ``after`` is exclusive and ``through`` inclusive; NULL is unbounded. A view
-- over the derived ``instrument_intervals``, so it follows every rebuild.
CREATE VIEW symbol_instruments AS
WITH runs AS (
    SELECT symbol, instrument_key, min(first_day) AS named_from, max(last_day) AS named_until
    FROM instrument_intervals
    GROUP BY symbol, instrument_key
)
SELECT
    symbol,
    instrument_key,
    named_from,
    named_until,
    lag(named_until) OVER w AS after,
    CASE WHEN lead(named_from) OVER w IS NULL THEN NULL ELSE named_until END AS through
FROM runs
WINDOW w AS (PARTITION BY symbol ORDER BY named_from);

GRANT SELECT ON symbol_instruments TO tradeline_app;

-- Kite's daily candles with the Instrument each belongs to: the Instrument its
-- Symbol names on its day (above), or, for a Symbol in no Instrument (none
-- rebuilt yet), the Symbol itself standing for one. Kite can hold one
-- Instrument-day under two names, fetched before and after a rename;
-- ``named_until`` says which name is Kite's more recent (NULL for a Symbol in
-- no Instrument).
CREATE VIEW candles_daily_instruments AS
SELECT
    coalesce(m.instrument_key, c.symbol) AS instrument,
    c.symbol,
    c.series,
    c.trade_date,
    c.open,
    c.high,
    c.low,
    c.close,
    c.volume,
    m.named_until
FROM candles_daily c
LEFT JOIN symbol_instruments m
    ON m.symbol = c.symbol
    AND (m.after IS NULL OR c.trade_date > m.after)
    AND (m.through IS NULL OR c.trade_date <= m.through);

GRANT SELECT ON candles_daily_instruments TO tradeline_app;
