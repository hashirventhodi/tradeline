-- The Paper Broker's Ledger tables (ADR 0003, 0019, #77): Orders, Fills and
-- Positions, each as event rows. Shaped as 0028's: every row carries its
-- Engine Mode and, in backtest mode alone, a backtest id; each table is
-- partitioned by mode. `day` and `seq` order every event of a mode's account
-- (a backtest, or the paper or live account): `day` is the trading day it
-- happened on, `seq` its place among that day's events across all three
-- tables.
--
-- Append-only: the application role may read and insert through the parent
-- tables and nothing else; the partitions grant it nothing.

-- An Order's events: `placed`, then `cancelled` unless a Fill ends it. Every
-- row repeats the Order's terms. `purpose` says why it exists: `entry` (a buy
-- from a Pass, with the Run and rank of that Pass), `target` (the Plan's
-- resting limit sell), or `stop` and `time` (an exit at the close less the
-- band). `session` is the trading day it can fill on; a target placed with its
-- entry's Fill rests through that day. `run_id` is the Run that placed it; the
-- target placed with a Fill belongs to the entry's Run. A cancellation's
-- `reason` is the fill model's (`open_above_limit`, `open_below_limit`,
-- `circuit_lock`, `no_bar`) or the Run's (`replaced`: re-placed for the next
-- session; `stop`, `time`: an exit fired and took the Position's one sell).
CREATE TABLE orders (
    mode        text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id uuid,
    id          uuid NOT NULL,
    event       text NOT NULL CHECK (event IN ('placed', 'cancelled')),
    day         date NOT NULL,
    seq         integer NOT NULL,
    reason      text CHECK ((event = 'cancelled') = (reason IS NOT NULL)),
    run_id      uuid NOT NULL,
    rank        integer,
    position_id uuid NOT NULL,
    instrument  text NOT NULL,
    symbol      text NOT NULL,
    side        text NOT NULL CHECK (side IN ('buy', 'sell')),
    purpose     text NOT NULL CHECK (purpose IN ('entry', 'target', 'stop', 'time')),
    limit_price numeric NOT NULL CHECK (limit_price > 0),
    shares      integer NOT NULL CHECK (shares > 0),
    session     date NOT NULL,
    CHECK ((side = 'buy') = (purpose = 'entry')),
    CHECK ((purpose = 'entry') = (rank IS NOT NULL)),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, id, event),
    FOREIGN KEY (mode, run_id) REFERENCES runs (mode, id),
    FOREIGN KEY (mode, run_id, rank) REFERENCES passes (mode, run_id, rank)
) PARTITION BY LIST (mode);

-- A Fill: the whole of one Order executed at one price. No Order fills in part.
CREATE TABLE fills (
    mode        text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id uuid,
    order_id    uuid NOT NULL,
    day         date NOT NULL,
    seq         integer NOT NULL,
    position_id uuid NOT NULL,
    instrument  text NOT NULL,
    symbol      text NOT NULL,
    side        text NOT NULL CHECK (side IN ('buy', 'sell')),
    price       numeric NOT NULL CHECK (price > 0),
    shares      integer NOT NULL CHECK (shares > 0),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, order_id)
) PARTITION BY LIST (mode);

-- A Position's events, each row its state after the event: `opened` by its
-- entry's Fill (entry is the fill price; stop, target and time stop are the
-- Pass's), `closed` by an exit's Fill, with the exit's price and reason
-- (`stop`, `target` or `time`). `run_id` and `rank` name the Pass, and so the
-- Plan, it came from.
CREATE TABLE positions (
    mode           text NOT NULL CHECK (mode IN ('backtest', 'paper', 'live')),
    backtest_id    uuid,
    position_id    uuid NOT NULL,
    event          text NOT NULL CHECK (event IN ('opened', 'closed')),
    day            date NOT NULL,
    seq            integer NOT NULL,
    run_id         uuid NOT NULL,
    rank           integer NOT NULL,
    instrument     text NOT NULL,
    symbol         text NOT NULL,
    opened_on      date NOT NULL,
    shares         integer NOT NULL CHECK (shares > 0),
    entry          numeric NOT NULL,
    stop           numeric NOT NULL,
    target         numeric NOT NULL,
    time_stop_days integer NOT NULL,
    exit_price     numeric CHECK ((event = 'closed') = (exit_price IS NOT NULL)),
    exit_reason    text CHECK (
        (event = 'closed') = (exit_reason IS NOT NULL)
        AND exit_reason IN ('stop', 'target', 'time')
    ),
    CHECK ((mode = 'backtest') = (backtest_id IS NOT NULL)),
    PRIMARY KEY (mode, position_id, day, seq),
    FOREIGN KEY (mode, run_id, rank) REFERENCES passes (mode, run_id, rank)
) PARTITION BY LIST (mode);

CREATE TABLE orders_backtest PARTITION OF orders FOR VALUES IN ('backtest');
CREATE TABLE orders_paper PARTITION OF orders FOR VALUES IN ('paper');
CREATE TABLE orders_live PARTITION OF orders FOR VALUES IN ('live');
CREATE TABLE fills_backtest PARTITION OF fills FOR VALUES IN ('backtest');
CREATE TABLE fills_paper PARTITION OF fills FOR VALUES IN ('paper');
CREATE TABLE fills_live PARTITION OF fills FOR VALUES IN ('live');
CREATE TABLE positions_backtest PARTITION OF positions FOR VALUES IN ('backtest');
CREATE TABLE positions_paper PARTITION OF positions FOR VALUES IN ('paper');
CREATE TABLE positions_live PARTITION OF positions FOR VALUES IN ('live');

-- Each Position as it stands: its latest event's row.
CREATE VIEW current_positions AS
SELECT DISTINCT ON (mode, position_id) *
FROM positions
ORDER BY mode, position_id, day DESC, seq DESC;

GRANT SELECT, INSERT ON orders, fills, positions TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON orders, fills, positions FROM tradeline_app;
GRANT SELECT ON current_positions TO tradeline_app;
