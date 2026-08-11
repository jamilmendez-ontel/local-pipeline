"""Dynamic TS project lists — single source of truth for TS-scoped consumers.

reference.ref_ontel_techops_projects is a view over stg_projects that derives
project_number from the name, so new TS projects appear automatically after
each projects-pipeline run. Never hardcode a TS list; query it here.
"""

MIN_TS_NUMBER = 13

_TS_PROJECTS_QUERY = """
    SELECT project_name, project_did, project_number
    FROM reference.ref_ontel_techops_projects
    WHERE project_number >= $1
    ORDER BY project_number
"""

_QA_EXPORT_QUERY = """
    SELECT p.project_name, p.project_did
    FROM reference.ref_qa_forms f
    JOIN reference.ref_ontel_techops_projects p ON p.project_number = f.ts_number
    WHERE f.active
    ORDER BY f.ts_number
"""


async def fetch_ts_projects(conn, min_number=MIN_TS_NUMBER):
    rows = await conn.fetch(_TS_PROJECTS_QUERY, min_number)
    return [dict(r) for r in rows]


async def fetch_qa_export_projects(conn):
    rows = await conn.fetch(_QA_EXPORT_QUERY)
    return [(r["project_name"], r["project_did"]) for r in rows]


def partition_by_rows(projects, counts):
    with_rows = [p for p in projects if counts.get(p, 0) > 0]
    empty = [p for p in projects if counts.get(p, 0) <= 0]
    return with_rows, empty
