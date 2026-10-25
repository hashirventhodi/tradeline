-- AMFI's scheme master lines and NAV history for the Benchmark's schemes and the
-- factor fund (ADR 0015 amendment, #94; tradeline.ingest.benchmark).
--
-- `amfi_schemes`: a scheme code's line in AMFI's NAVAll.txt as first seen, a
-- new row only when its name, plan, option or ISINs differ from the code's
-- latest row, so the master is readable as of any time by `fetched_at`.
-- `nav_date` is the date of the NAV the line carried when first seen.
CREATE TABLE amfi_schemes (
    scheme_code       integer NOT NULL,
    scheme_name       text NOT NULL,
    plan              text NOT NULL,
    option            text NOT NULL,
    isin_growth       text,
    isin_reinvestment text,
    nav_date          date NOT NULL,
    fetched_at        timestamptz NOT NULL,
    PRIMARY KEY (scheme_code, fetched_at)
);

-- `amfi_navs`: one row per (scheme, NAV date, NAV) as AMFI's history API gives
-- it. A NAV AMFI later revises is a second row for the date, the later
-- `fetched_at` the one read. `uploaded_at` is AMFI's upload time as its clock
-- shows it (suffixed "Z" but displayed as the same Indian evening time), so it
-- carries no zone.
CREATE TABLE amfi_navs (
    scheme_code integer NOT NULL,
    nav_date    date NOT NULL,
    nav         numeric NOT NULL CHECK (nav > 0),
    uploaded_at timestamp,
    fetched_at  timestamptz NOT NULL,
    PRIMARY KEY (scheme_code, nav_date, nav)
);

GRANT SELECT, INSERT ON amfi_schemes, amfi_navs TO tradeline_app;
REVOKE UPDATE, DELETE, TRUNCATE ON amfi_schemes, amfi_navs FROM tradeline_app;
