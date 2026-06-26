-- migrations/126_ref_leave_code.sql
-- Leave/work code reference for calendar normalization. Seeded from the
-- authoritative HR daily-report legend (2026-06-26). Unknown legacy codes
-- (LAC/STL/HD/LWOP) are intentionally omitted; they fall back to the raw code.
BEGIN;

CREATE TABLE IF NOT EXISTS reference.ref_leave_code (
    code              text PRIMARY KEY,
    code_num          text,
    label             text,
    category          text,
    scope_note        text,
    requires_rtw_form boolean NOT NULL DEFAULT false,
    is_active         boolean NOT NULL DEFAULT true,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now()
);

INSERT INTO reference.ref_leave_code (code, code_num, label, category, scope_note, requires_rtw_form) VALUES
    ('RDOT','001','Rest Day Overtime','overtime',NULL,false),
    ('RDO','002','Rest Day Offset','rest',NULL,false),
    ('VL','003','Vacation Leave','leave',NULL,false),
    ('SL','004','Sick Leave','leave',NULL,true),
    ('EL','005','Emergency Leave','leave',NULL,true),
    ('SDL','006','Sudden Leave','leave',NULL,false),
    ('UT','007','Undertime','leave',NULL,false),
    ('BL','008','Birthday Leave','leave',NULL,false),
    ('ML','009','Maternity Leave','leave','start date only',true),
    ('PL','010','Paternity Leave','leave',NULL,false),
    ('SPL','011','Solo Parent Leave','leave',NULL,false),
    ('BRL','013','Bereavement Leave','leave',NULL,false),
    ('LR','015','Weekend Live Review','work','TS Team only',false),
    ('WW','016','Weekend Work','work','TS Team only',false),
    ('LRWD','017','Weekday Live Review','work','TS Team only',false),
    ('LDL','018','Learning & Development Leave','leave',NULL,false),
    ('LDO','019','Learning & Development Overtime','overtime',NULL,false),
    ('RD',NULL,'Rest Day','rest','scheduled rest-day marker',false)
ON CONFLICT (code) DO NOTHING;

COMMIT;
