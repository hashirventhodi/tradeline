-- Raw daily candles exactly as Kite's historical API returned them, one per
-- (symbol, series, day), keyed like the instrument master. Nothing is derived
-- and nothing is adjusted: the adjusted series is computed on read (ADR 0006).
-- Kite can resend a bar; a resend is inserted only if the bar is new, and a
-- resend whose values differ is recorded as an anomaly in the ingest run and
-- the stored bar kept. The application role cannot update or delete rows.
CREATE TABLE candles_daily (
    symbol     text NOT NULL,
    series     text NOT NULL,
    trade_date date NOT NULL,
    open       numeric NOT NULL,
    high       numeric NOT NULL,
    low        numeric NOT NULL,
    close      numeric NOT NULL,
    volume     bigint NOT NULL,
    PRIMARY KEY (symbol, series, trade_date)
);

GRANT SELECT, INSERT ON candles_daily TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON candles_daily FROM tradeline_app;
