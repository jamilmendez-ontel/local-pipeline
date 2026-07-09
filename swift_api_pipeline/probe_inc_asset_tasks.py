#!/usr/bin/env python3
"""One-off probe: dump hierarchy payload shapes for one TS project.

Writes key inventories (not full payloads, they may contain emails) to
stdout. Run locally with the pipeline .env present. Read-only against
Swift; touches no tables.

Usage:
    python probe_inc_asset_tasks.py --project-did <did> --project-name "TECH-OPS: TS17"
    python probe_inc_asset_tasks.py [--project-number 13]   # needs DB (WARP on)

With --project-did (plus optional --project-name for display), the
reference.ref_ontel_techops_projects lookup is bypassed and NO database
connection is made at all (the direct DB host is IPv6-only and unreachable
when Cloudflare WARP is off; pooler workaround exists but is not needed here).

Also inventories the `metrics` sub-object at project/asset-project/asset-task
level for child-count fields (e.g. taskCount, reqCount). If a parent's metrics
expose a reliable child count, deletions become detectable from a stored-vs-
fetched count mismatch alone -- relevant to the GC-scale deletion strategy
(see docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md).
"""
import argparse
import json
from collections import Counter

from extract import SwiftAPIExtractor
from extract_daily_reports import DailyReportsPipeline  # reuses _request/auth


class ProbeFetcher(DailyReportsPipeline):
    """DailyReportsPipeline's fetchers without its DB dependency.

    BaseExtractor.__init__ (used by DailyReportsPipeline.__init__) calls
    get_db(), which needs the direct DB host. This probe only needs the API
    side, so authenticate via SwiftAPIExtractor instead; _request()'s 401
    retry path (self.ext.authenticate() / self.ext.token) works unchanged.
    """

    def __init__(self):
        ex = SwiftAPIExtractor()
        ex.authenticate()
        self.ext = ex
        self.headers = {"Authorization": f"Bearer {ex.token}"}
        self.base = ex.base_url


def key_inventory(rows):
    keys = Counter()
    for r in rows:
        for k in r.keys():
            keys[k] += 1
    return dict(keys.most_common())


def metrics_key_inventory(rows):
    """Inventory keys inside each row's `metrics` sub-object, plus a sample
    value for any key whose name suggests a child count (e.g. *Count). Counts
    are non-personal (small integers), so a sample value is fine to print.
    """
    keys = Counter()
    count_samples = {}
    rows_with_metrics = 0
    for r in rows:
        m = r.get("metrics")
        if isinstance(m, dict):
            rows_with_metrics += 1
            for k, v in m.items():
                keys[k] += 1
                if "count" in k.lower() and k not in count_samples:
                    count_samples[k] = v
    return rows_with_metrics, dict(keys.most_common()), count_samples


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-number", type=int, default=13)
    ap.add_argument("--project-did", help="Bypass the DB ref-table lookup entirely")
    ap.add_argument("--project-name", default="(name not looked up)",
                    help="Display name when using --project-did")
    args = ap.parse_args()

    if args.project_did:
        project_did = args.project_did
        project_name = args.project_name
    else:
        from config import SCHEMA_REFERENCE, get_db
        db = get_db()
        proj = db.fetchrow(
            f"SELECT project_did, project_name FROM {SCHEMA_REFERENCE}.ref_ontel_techops_projects "
            f"WHERE project_number = $1", args.project_number)
        if not proj:
            print(f"No row in {SCHEMA_REFERENCE}.ref_ontel_techops_projects for "
                  f"project_number = {args.project_number}. Inspect the table "
                  f"(SELECT * LIMIT 5) and adjust the WHERE clause.")
            return
        project_did = proj["project_did"]
        project_name = proj["project_name"]
    print(f"Project: {project_name} ({project_did})")

    pipe = ProbeFetcher()

    # ---- Asset-project level (GET /api/projects/{project_did}/assets) ----
    assets = pipe.fetch_assets(project_did)
    print(f"\nASSETS (asset-project level): {len(assets)} rows")
    print(json.dumps(key_inventory(assets), indent=2))
    ids = [a.get("id") for a in assets]
    print(f"asset id unique within project: {len(ids) == len(set(ids))} "
          f"({len(set(ids))} unique / {len(ids)} rows)")
    print(f"assets with lastUpdated: {sum(1 for a in assets if a.get('lastUpdated'))}")

    rows_with_m, m_keys, m_counts = metrics_key_inventory(assets)
    print(f"\nASSET metrics: present on {rows_with_m}/{len(assets)} rows")
    print(json.dumps(m_keys, indent=2))
    print(f"count-like fields (sample values, truncated): "
          f"{ {k: str(v)[:20] for k, v in m_counts.items()} }")

    # ---- Asset-task level, sampled from one asset ----
    sample = assets[0]
    tasks = pipe.fetch_tasks(sample["id"])
    print(f"\nTASKS for sample asset (name redacted): {len(tasks)} rows")
    print(json.dumps(key_inventory(tasks), indent=2))
    tids = [t.get("id") for t in tasks]
    print(f"task id unique within sample asset: {len(tids) == len(set(tids))} "
          f"({len(set(tids))} unique / {len(tids)} rows)")
    print(f"tasks with lastUpdated: {sum(1 for t in tasks if t.get('lastUpdated'))}")

    rows_with_m, m_keys, m_counts = metrics_key_inventory(tasks)
    print(f"\nTASK metrics: present on {rows_with_m}/{len(tasks)} rows")
    print(json.dumps(m_keys, indent=2))
    print(f"count-like fields (sample values, truncated): "
          f"{ {k: str(v)[:20] for k, v in m_counts.items()} }")

    # ---- Project-wide task id uniqueness across ALL assets ----
    # The brief only checks uniqueness within one sample asset; this extends
    # the check project-wide, since the findings doc's open item asks about
    # id uniqueness "within project" for tasks (natural-key requirement).
    # Capped at the first SWEEP_CAP assets to keep the one-off probe fast.
    SWEEP_CAP = 40
    sweep = assets[:SWEEP_CAP]
    print(f"\n--- Task id uniqueness across first {len(sweep)} of {len(assets)} assets ---")
    all_task_ids = []
    ids_by_collection = {}
    for a in sweep:
        try:
            a_tasks = pipe.fetch_tasks(a["id"])
        except Exception as e:
            print(f"  WARN: fetch_tasks failed for asset {a.get('id')}: {e}")
            continue
        for t in a_tasks:
            all_task_ids.append(t.get("id"))
            ids_by_collection.setdefault(t.get("collection"), []).append(t.get("id"))
    print(f"total rows fetched: {len(all_task_ids)}")
    print(f"row id unique across assets (all collections): "
          f"{len(all_task_ids) == len(set(all_task_ids))} "
          f"({len(set(all_task_ids))} unique / {len(all_task_ids)} rows)")
    # The listing mixes collections: project-level 'milestones' rows repeat
    # under every asset, so only per-collection uniqueness is meaningful.
    for coll, cids in sorted(ids_by_collection.items(), key=lambda kv: str(kv[0])):
        print(f"  collection={coll!r}: {len(cids)} rows, {len(set(cids))} unique -> "
              f"unique={len(cids) == len(set(cids))}")

    # ---- Project level lastUpdated + metrics (org projects listing) ----
    orgs = pipe.ext.extract_organizations()
    found = False
    for org in orgs:
        for p in pipe.ext.extract_projects(org["id"]):
            if p.get("id") == project_did or p.get("did") == project_did:
                print(f"\nPROJECT row keys: {sorted(p.keys())}")
                print(f"project lastUpdated present: {'lastUpdated' in p}")
                pm = p.get("metrics")
                if isinstance(pm, dict):
                    print(f"PROJECT metrics keys: {sorted(pm.keys())}")
                    # Counts live in NESTED dicts: metrics.asset aggregates
                    # across all asset-projects (incl. assetProjectCount);
                    # metrics.project covers project-level (non-asset) tasks.
                    for k, v in sorted(pm.items()):
                        if isinstance(v, dict):
                            counts = {ck: cv for ck, cv in v.items()
                                      if isinstance(cv, (int, float)) and "count" in ck.lower()}
                            print(f"PROJECT metrics.{k} keys: {sorted(v.keys())}")
                            print(f"PROJECT metrics.{k} count fields: {counts}")
                else:
                    print("PROJECT metrics: not present")
                found = True
                break
        if found:
            break
    if not found:
        print("\nPROJECT row not found in any org's project listing.")


if __name__ == "__main__":
    main()
