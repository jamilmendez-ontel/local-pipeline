"""
Export a 1000-row sample of stg_timer_activities_clean joined to the asset it is
linked to (via asset_did), so we can eyeball how the timer<->asset connection
behaves -- especially site_id (folder path) vs asset_id (folder path), and
site_name vs asset_name.

Output: out/timer_asset_link_inspect.xlsx  (two sheets: Sample, Legend)
Run:    venv\\Scripts\\python.exe out\\timer_asset_link_inspect.py
"""
import asyncio
import os
from pathlib import Path

import asyncpg
import pandas as pd
from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
load_dotenv(ROOT / ".env")

# Direct host (db.<ref>.supabase.co) is IPv6-only and does not resolve from here,
# so connect through the Supavisor pooler (IPv4), same workaround the GHA pipelines use.
PROJECT_REF = "voqfjfngdpcvevbkikud"
DSN = dict(
    host="aws-0-ap-southeast-1.pooler.supabase.com",
    port=6543,  # transaction pooler
    database=os.environ.get("SUPABASE_DB", "postgres"),
    user=f"postgres.{PROJECT_REF}",
    password=os.environ["SUPABASE_PASSWORD"],
    statement_cache_size=0,  # required for Supavisor transaction mode
    ssl="require",           # encrypted connection (Supabase pooler)
)

# Three strata so the file shows the full range of connection behaviour:
#   A) linked AND site_name == asset_name      (the clean, trustworthy case)
#   B) linked BUT site_name != asset_name      (matched by path/FA, name differs -> the risky case)
#   C) NOT linked (asset_did IS NULL)           (what timer has that found no asset)
QUERY = """
WITH base AS (
    SELECT
        c.project,
        c.project_did,
        c.site_name,
        c.site_id,
        c.task,
        c.task_clean,
        c.user_email,
        (c.start_time AT TIME ZONE 'America/New_York') AS start_et,
        ROUND(c.duration_min, 2) AS duration_min,
        c.asset_did,
        a.asset_name,
        a.asset_id   AS asset_path,
        a.asset_status,
        a.carrier_group
    FROM data_staging.stg_timer_activities_clean c
    LEFT JOIN LATERAL (
        SELECT asset_name, asset_id, asset_status, carrier_group
        FROM data_staging.stg_assets a
        WHERE a.asset_did = c.asset_did
        ORDER BY (TRIM(a.asset_name) = TRIM(c.site_name)) DESC,
                 (a.project_did = c.project_did) DESC
        LIMIT 1
    ) a ON c.asset_did IS NOT NULL
),
tagged AS (
    SELECT *,
        CASE
            WHEN asset_did IS NULL THEN 'C: UNLINKED (no asset matched)'
            WHEN TRIM(site_name) = TRIM(asset_name) AND site_id = asset_path THEN 'A: name + path match'
            WHEN TRIM(site_name) = TRIM(asset_name) THEN 'A: name match (path differs)'
            WHEN site_id = asset_path THEN 'B: path match (name differs)'
            ELSE 'B: linked by FA/other (name & path differ)'
        END AS link_basis
    FROM base
),
stratA AS (SELECT * FROM tagged WHERE link_basis LIKE 'A:%' ORDER BY random() LIMIT 500),
stratB AS (SELECT * FROM tagged WHERE link_basis LIKE 'B:%' ORDER BY random() LIMIT 300),
stratC AS (SELECT * FROM tagged WHERE link_basis LIKE 'C:%' ORDER BY random() LIMIT 200)
SELECT * FROM stratA
UNION ALL SELECT * FROM stratB
UNION ALL SELECT * FROM stratC;
"""

COLUMNS = [
    "link_basis",
    "project", "project_did",
    "site_name", "asset_name",
    "site_id", "asset_path",
    "task", "task_clean",
    "asset_status", "carrier_group",
    "user_email", "start_et", "duration_min", "asset_did",
]

LEGEND = pd.DataFrame(
    [
        ["A: name + path match", "Timer site_name == asset_name AND site_id == asset_path. Strongest link."],
        ["A: name match (path differs)", "Names equal; folder paths differ (usually a shared batch path). Link trusted on the name."],
        ["B: path match (name differs)", "Linked because site_id folder path == asset_id, but the names differ. Risky."],
        ["B: linked by FA/other (name & path differ)", "Linked via FA number or fallthrough; neither name nor path equal. Riskiest."],
        ["C: UNLINKED (no asset matched)", "Timer row has no asset_did. Shows what timer carries when no asset is found."],
        ["", ""],
        ["site_id", "TIMER side: the folder/batch path the technician's site sits under."],
        ["asset_path", "ASSET side: stg_assets.asset_id, the folder/batch path of the linked asset."],
        ["site_name", "TIMER side: the site label the technician typed (may carry qualifiers like '(Civil)')."],
        ["asset_name", "ASSET side: stg_assets.asset_name, the canonical asset label."],
    ],
    columns=["value", "meaning"],
)


async def main():
    conn = await asyncpg.connect(**DSN)
    try:
        rows = await conn.fetch(QUERY)
    finally:
        await conn.close()

    df = pd.DataFrame([dict(r) for r in rows])
    df = df[COLUMNS]
    df = df.sort_values(["link_basis", "project", "site_name"]).reset_index(drop=True)

    out = HERE / "timer_asset_link_inspect.xlsx"
    with pd.ExcelWriter(out, engine="openpyxl") as xl:
        df.to_excel(xl, sheet_name="Sample", index=False)
        LEGEND.to_excel(xl, sheet_name="Legend", index=False)
        # widen columns a bit for readability
        ws = xl.sheets["Sample"]
        widths = {
            "A": 34, "B": 22, "C": 24, "D": 36, "E": 36,
            "F": 52, "G": 52, "H": 26, "I": 26, "J": 16,
            "K": 14, "L": 26, "M": 20, "N": 12, "O": 24,
        }
        for col, w in widths.items():
            ws.column_dimensions[col].width = w
        ws.freeze_panes = "A2"

    counts = df["link_basis"].value_counts().to_dict()
    print(f"Wrote {len(df)} rows -> {out}")
    for k, v in counts.items():
        print(f"  {v:>4}  {k}")


if __name__ == "__main__":
    asyncio.run(main())
