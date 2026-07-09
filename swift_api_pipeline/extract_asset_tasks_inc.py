#!/usr/bin/env python3
"""Incremental asset-tasks pipeline (TS13+ shadow pilot).

Walk: project (content_watermarks skip) -> asset list (stg_assets_inc is
the watermark) -> task list for changed assets only -> guarded upserts of
changed tasks + keep-list reconcile inside every successfully-fetched
scope. See docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md
and the API findings doc for field semantics.
"""
from datetime import datetime, timezone

# FIELD_MAP: single place where Swift payload keys are named. MUST match
# docs/superpowers/specs/2026-07-09-inc-asset-tasks-api-findings.md; update
# here if the probe found different names.
#
# Note: there is no "assetId" key on asset-project rows. asset_identifier
# is sparse (5,022 of 5,025 rows in the probe); missing means None.
FIELD_MAP = {
    "id": "id",
    "last_updated": "lastUpdated",
    "collection": "collection",
    "asset_name": "name",
    "asset_identifier": "identifier",
    "req_count": ("metrics", "reqCount"),
    "task_count": ("metrics", "taskCount"),
}


def epoch_to_ts(val):
    """Swift epoch millis -> aware UTC datetime (None-safe)."""
    if not val:
        return None
    return datetime.fromtimestamp(int(val) / 1000, tz=timezone.utc)


def plan_asset_visits(fetched_assets, stored_assets):
    """Decide which assets to descend into.

    stored_assets: {asset_did: {"last_updated": datetime|None, ...}} from
    stg_assets_inc for this project. Returns (visits, missing_dids):
    visits = fetched asset rows that are new or moved past the stored
    last_updated; missing_dids = stored dids absent from the fetch
    (deletion candidates, reconciled by the caller).
    """
    visits = []
    fetched_ids = set()
    for a in fetched_assets:
        did = a.get(FIELD_MAP["id"])
        if not did:
            continue
        fetched_ids.add(did)
        stored = stored_assets.get(did)
        fetched_ts = epoch_to_ts(a.get(FIELD_MAP["last_updated"]))
        if stored is None or stored.get("last_updated") is None \
                or fetched_ts is None or fetched_ts > stored["last_updated"]:
            visits.append(a)
    missing = set(stored_assets) - fetched_ids
    return visits, missing


def plan_task_writes(fetched_tasks, stored_task_ts):
    """Same contract at task level. stored_task_ts: {task_did: last_updated}.

    The asset-tasks listing mixes collections: milestone rows repeat under
    every asset and are the only id duplicates. Only rows with
    collection == "asset-tasks" are considered here; every other
    collection (milestones, etc.) is ignored entirely, not written and
    not counted as fetched for deletion detection.
    """
    writes = []
    fetched_ids = set()
    for t in fetched_tasks:
        if t.get(FIELD_MAP["collection"]) != "asset-tasks":
            continue
        did = t.get(FIELD_MAP["id"])
        if not did:
            continue
        fetched_ids.add(did)
        stored_ts = stored_task_ts.get(did)
        fetched_ts = epoch_to_ts(t.get(FIELD_MAP["last_updated"]))
        if stored_ts is None or fetched_ts is None or fetched_ts > stored_ts:
            writes.append(t)
    missing = set(stored_task_ts) - fetched_ids
    return writes, missing
