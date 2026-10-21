-- Raw minute candles exactly as Kite's historical API returned them, one per
-- (symbol, series, bar start), keyed like the instrument master and handled
-- like candles_daily: a bar is inserted only if new, a resend whose values
-- differ is an anomaly recorded in the ingest run, and the application role
-- cannot update or delete rows.
--
-- Partitioned by calendar month in Indian time, the trading calendar's
-- timezone. No partition exists up front: the ingestion asks
-- candles_minute_partition for each month in a batch before inserting it, and
-- the function creates the month's partition on first use. It runs as its
-- owner (SECURITY DEFINER) so the application role needs no CREATE privilege;
-- it can create only partitions of this table, named and bounded by the month.
CREATE TABLE candles_minute (
    symbol text NOT NULL,
    series text NOT NULL,
    ts     timestamptz NOT NULL,
    open   numeric NOT NULL,
    high   numeric NOT NULL,
    low    numeric NOT NULL,
    close  numeric NOT NULL,
    volume bigint NOT NULL,
    PRIMARY KEY (symbol, series, ts)
) PARTITION BY RANGE (ts);

GRANT SELECT, INSERT ON candles_minute TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON candles_minute FROM tradeline_app;

CREATE FUNCTION candles_minute_partition(month date) RETURNS text
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
DECLARE
    first_day date := date_trunc('month', month)::date;
    name text := format('candles_minute_%s', to_char(first_day, 'YYYY_MM'));
BEGIN
    IF to_regclass(format('public.%I', name)) IS NULL THEN
        BEGIN
            EXECUTE format(
                'CREATE TABLE public.%I PARTITION OF public.candles_minute'
                ' FOR VALUES FROM (%L) TO (%L)',
                name,
                first_day::timestamp AT TIME ZONE 'Asia/Kolkata',
                (first_day + interval '1 month')::timestamp AT TIME ZONE 'Asia/Kolkata'
            );
        EXCEPTION WHEN duplicate_table THEN
            NULL;  -- a concurrent run created it first
        END;
    END IF;
    RETURN name;
END
$$;

REVOKE ALL ON FUNCTION candles_minute_partition(date) FROM PUBLIC;
GRANT EXECUTE ON FUNCTION candles_minute_partition(date) TO tradeline_app;
