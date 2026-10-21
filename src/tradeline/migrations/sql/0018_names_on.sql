-- Whether a Symbol's run in ``symbol_instruments`` (migration 0015) owns the day
-- ``day``: after its ``after`` (exclusive) and through its ``through``
-- (inclusive), NULL being unbounded. The one statement of the rule every read
-- that places a Kite-dated row (a candle, a Universe month, a requested Symbol)
-- uses, so they cannot drift apart. A simple immutable SQL function, inlined by
-- the planner. ``queries._OWNED`` places NSE's rows differently, on purpose.
CREATE FUNCTION names_on(run_after date, run_through date, day date) RETURNS boolean
LANGUAGE sql IMMUTABLE PARALLEL SAFE
AS $$
    SELECT (run_after IS NULL OR day > run_after) AND (run_through IS NULL OR day <= run_through)
$$;

CREATE OR REPLACE VIEW candles_daily_instruments AS
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
    ON m.symbol = c.symbol AND names_on(m.after, m.through, c.trade_date);
