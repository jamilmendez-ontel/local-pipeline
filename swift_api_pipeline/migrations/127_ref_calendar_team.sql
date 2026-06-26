-- migrations/127_ref_calendar_team.sql
-- Fallback label->canonical mapping for team_normalized (primary source is the
-- matched employee's carrier_group; this is used for RD/unmatched rows).
-- Canonicals drawn from reference.ref_employees taxonomy. Lookup is by
-- lower(trim(team_raw)); store one row per lowercased key.
BEGIN;

CREATE TABLE IF NOT EXISTS reference.ref_calendar_team (
    team_raw       text PRIMARY KEY,
    team_canonical text,
    level          text,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);

INSERT INTO reference.ref_calendar_team (team_raw, team_canonical, level) VALUES
    ('CG1','CG1 - Verizon','carrier_group'),
    ('CG2','CG2 - AT&T/DISH','carrier_group'),
    ('CG3','CG3 - TMO/USCC','carrier_group'),
    ('Acctg','Accounting','carrier_group'),
    ('Accounting','Accounting','carrier_group'),
    ('Admin and Ops','Admin and Operations','carrier_group'),
    ('Admin & Ops','Admin and Operations','carrier_group'),
    ('T&A','Tools&Auto','carrier_group'),
    ('TNA','Tools&Auto','carrier_group'),
    ('CRTV','Creatives','carrier_group'),
    ('CRTVS','Creatives','carrier_group'),
    ('R&D','Research','carrier_group'),
    ('QPI','QPI','carrier_group'),
    ('DA','DA','carrier_group'),
    ('HR','HR','carrier_group'),
    ('TS Admin','TS-Admin','carrier_group'),
    ('DSM','PHDSM','carrier_group'),
    ('PHIDSM','PHDSM','carrier_group'),
    ('PHIDS','PHDSM','carrier_group'),
    ('PHI DS','PHDSM','carrier_group'),
    ('Swift','Swifttt','carrier_group'),
    ('Alpha','Alpha','cluster'),
    ('Beta','Beta','cluster'),
    ('Gamma','Gamma','cluster'),
    ('Delta','Delta','cluster'),
    ('Epsilon','Epsilon','cluster'),
    ('Zeta','Zeta','cluster'),
    ('MKTG','Marketing','department'),
    ('Marketing','Marketing','department'),
    ('PHI HR','HR','carrier_group'),
    ('T&D','Swifttt','carrier_group'),
    ('Trainee',NULL,'status'),
    ('SD',NULL,'unknown'),
    ('TS Ops',NULL,'unknown')
ON CONFLICT (team_raw) DO NOTHING;

COMMIT;
