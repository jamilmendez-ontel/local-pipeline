-- Migration 025: Carrier group lookup table
-- Maps search terms found in asset_id to carrier groups for COP reporting

CREATE TABLE IF NOT EXISTS data_staging.carrier_group_lookup (
    id SERIAL PRIMARY KEY,
    search_term TEXT NOT NULL UNIQUE,
    carrier_group TEXT NOT NULL,
    match_order INT NOT NULL
);

INSERT INTO data_staging.carrier_group_lookup (search_term, carrier_group, match_order) VALUES
    ('VZW',                'Verizon',    1),
    ('Verizon',            'Verizon',    2),
    ('Westell/CGC',        'Verizon',    3),
    ('DISH',               'AT&T/DISH',  4),
    ('AT&T',               'AT&T/DISH',  5),
    ('T-Mobile',           'TMO/USCC',   6),
    ('Viking Maintenance', 'TMO/USCC',   7),
    ('US Cellular',        'TMO/USCC',   8),
    ('Gulf Services',      'TMO/USCC',   9),
    ('FTTH',               'TMO/USCC',  10)
ON CONFLICT (search_term) DO UPDATE
    SET carrier_group = EXCLUDED.carrier_group,
        match_order   = EXCLUDED.match_order;
