-- Which of NSE's corporate-action lists a row came from (#33): the main
-- board's (``index=equities``), the SME platform's (``sme``), or fund units'
-- (``mf``). Every row stored before this came from the main board's, hence the
-- default; the ingest always states the list. The list is not part of the key:
-- a record two lists serve is stored once, as the first list to serve it gave it.
ALTER TABLE corporate_actions
    ADD COLUMN nse_list text NOT NULL DEFAULT 'equities'
        CHECK (nse_list IN ('equities', 'sme', 'mf'));
