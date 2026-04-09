import json
import requests
from credentials import get_user_id

base_url = "https://prod.api.swiftprojects.io"

# ----------------------------
# Tiny helper: safe GET -> JSON
# (very simple: tries up to 3 times, prints why it failed, returns {} if not JSON)
# ----------------------------
def get_json(url, headers=None, params=None, note="request"):
    for attempt in range(1, 4):  # 3 tries
        try:
            r = requests.get(url, headers=headers or {}, params=params or {}, timeout=60)
            # print(f"→ {note}: {r.request.method} {r.url}")  # uncomment if you want to see full URL
            r.raise_for_status()

            # must be JSON
            ctype = (r.headers.get("Content-Type") or "").lower()
            if "application/json" not in ctype:
                print(f"⚠️ {note}: non-JSON response | Content-Type={ctype}")
                print(f"   Body (first 200): {r.text[:200]!r}")
                return {}

            return r.json()

        except requests.HTTPError:
            print(f"❌ {note}: HTTP {r.status_code} {r.reason} | attempt {attempt}/3")
            print(f"   Body (first 200): {r.text[:200]!r}")
        except Exception as e:
            print(f"❌ {note}: {e} | attempt {attempt}/3")

    # after 3 tries, give up
    return {}


# ----------------------------
# 3) Assets by Project
# ----------------------------
def get_assets(project_DID, tok):
    headers = {"Authorization": f"Bearer {tok}"}
    url = f"{base_url}/api/projects/{project_DID}/assets"

    page = 0
    page_size = 1000
    results = []

    while True:
        params = {"page": page, "pageSize": page_size}
        data = get_json(url, headers=headers, params=params, note=f"assets page {page}")
        rows = data.get("list", [])

        if not rows:
            break

        for item in rows:
            asset = item.get("asset") or {}
            results.append({
            })

        if len(rows) < page_size:
            break
        page += 1

    return results


# ----------------------------
# 4) Tasks by Asset-Project
# ----------------------------
def get_tasks(asset_project_DID, tok):
    headers = {"Authorization": f"Bearer {tok}"}
    url = f"{base_url}/api/asset-projects/{asset_project_DID}/asset-tasks"

    page = 0
    page_size = 1000
    results = []

    while True:
        params = {
            "page": page,
            "pageSize": page_size,
            "filter": "punch item",
            "timezone": "America/New_York",
            "dateFormat": "yyyy-MM-dd'T'HH:mm:ssZ"
        }

        data = get_json(url, headers=headers, params=params, note=f"tasks page {page}")
        rows = data.get("list", [])

        if not rows:
            break

        for item in rows:
            if item.get("collection") == "asset-tasks":
                assigned = item.get("assignedTo") or {}
                results.append({
                })

        if len(rows) < page_size:
            break
        page += 1

    return results


# ----------------------------
# 5) Requirements by Task
# ----------------------------
def get_requirements(task_DID, tok):
    headers = {"Authorization": f"Bearer {tok}"}
    url = f"{base_url}/api/asset-tasks/{task_DID}/requirements"

    page = 0
    page_size = 1000
    results = []

    while True:
        params = {"page": page, "pageSize": page_size}
        data = get_json(url, headers=headers, params=params, note=f"requirements page {page}")
        rows = data.get("list", [])

        if not rows:
            break

        for item in rows:
            results.append({
            })

        if len(rows) < page_size:
            break
        page += 1

    return results