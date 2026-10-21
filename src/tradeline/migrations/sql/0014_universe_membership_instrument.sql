-- The Instrument a Universe member was ranked as, under a Universe Rule that
-- ranks Instruments (ADR 0016); the member's Symbol is the one in force on the
-- window's last day. NULL under a rule that ranks Symbols, so the rows stored
-- before this column existed are unchanged. The table stays append-only.
ALTER TABLE universe_membership ADD COLUMN instrument_key text;
