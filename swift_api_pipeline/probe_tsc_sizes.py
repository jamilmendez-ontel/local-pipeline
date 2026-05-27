"""Parallel size probe for TSC Construction's 53 active projects.

For each project, paginates Swift /assets/_export counting rows only (no DB writes).
Caps at 200 pages (~200K rows) or 4 min per project — if hit, the project is
classified as "large, capped". This is fine because we only need to bucket
projects into size categories, not get exact counts on the monsters.

Output: probe_tsc_sizes_results.json + console summary.
"""
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from dotenv import load_dotenv
import requests

load_dotenv()

EMAIL = os.environ["SWIFT_EMAIL"]
PASSWORD = os.environ["SWIFT_PASSWORD"]

AUTH_URL = "https://prod.api.swiftprojects.io/api/auth/token"
EXPORT = "https://prod.api.swiftprojects.io/api/next/projects/{pid}/assets/_export"

PAGE_SIZE = 1000
CAP_PAGES = 200       # ~200K rows max counted per project
CAP_SECONDS = 240     # 4 min absolute max per project
MAX_WORKERS = 12

# The 53 TSC Construction active projects (org_did = -LSLnbjcxlr7_LrWCzeH).
PROJECTS = [
    ("-NvX_bKSnNxCMXAL24rQ", "Ericsson/AT&T/AR-OK"),
    ("-NhlLzF3y6wAwpoCv8jc", "Ericsson/AT&T/FL"),
    ("-NijEbcNBUbVfTyyN69P", "Ericsson/AT&T/GA"),
    ("-NvX_km-_fOUQJygtfef", "Ericsson/AT&T/KS"),
    ("-OHxB6eI1C9slp-byUX5", "Ericsson/AT&T/KS (Turf 6)"),
    ("-N1ncOq0TpVZQhRWnKGU", "Ericsson/AT&T/MI-IN"),
    ("-NhlLk4gK9p6Uz7OkJvW", "Ericsson/AT&T/New England"),
    ("-OD7LFyfnc_Nj8RlsmCb", "Ericsson/AT&T/New England (Turf 6)"),
    ("-OjRF2YrjFEwN4DL0i03", "Ericsson/AT&T/New England (Turf 6) (2026)"),
    ("-NhlLptJVkQkfb0ExCYN", "Ericsson/AT&T/NTX"),
    ("-OIv9eGun8fo1OtF_m_g", "Ericsson/AT&T/NTX (Turf 6)"),
    ("-NvX_SINvBufTHiJPWQV", "Ericsson/AT&T/OH"),
    ("-O7HW82CKkb5pU6SUm8G", "Ericsson/AT&T/OH (Turf 6)"),
    ("-NvX_hS1c923_isjFfQR", "Ericsson/AT&T/PA"),
    ("-OArSzsR0VBTAjibJbE2", "Ericsson/AT&T/PA (Turf 6)"),
    ("-NhlLrqUtMaR1rVNPRXR", "Ericsson/AT&T/STX"),
    ("-ODHVSi544dz1LOIujMy", "Ericsson/AT&T/STX (Turf 6)"),
    ("-NW7wk8vB2V_o_vJOyMc", "Ericsson/Nemont Communications/MT - Overlay"),
    ("-NOpZnTfRaDb50Lq3dAH", "Ericsson/T-Mobile/BAWA - Overlay"),
    ("-NNsN__UW2sWtE8G8ORQ", "Ericsson/T-Mobile/CT - Overlay"),
    ("-MbrNUJJ1G_BzHQns1QX", "Ericsson/T-Mobile/FL - Excalibur"),
    ("-MHH8q2VXbgLbQDsaSNM", "Ericsson/T-Mobile/FL - Overlay"),
    ("-Ma-9UGPb5w1kHjS91hb", "Ericsson/T-Mobile/GA - Overlay"),
    ("-Mojvihqoxj-MUemd1Kb", "Ericsson/T-Mobile/NC - Overlay"),
    ("-NwucbnHksxIm-JBW9-Z", "Ericsson/T-Mobile/NC-SC/TV - Overlay"),
    ("-NhlLnyPnREy3GDhwzFL", "Ericsson/T-Mobile/New England - Overlay"),
    ("-OknI6RnIcmIUl2KykuP", "Ericsson/T-Mobile/NTX- NSB Macro/Civil"),
    ("-M7IqzDnXvYtQJ9KRxFI", "Ericsson/T-Mobile/PA - Overlay"),
    ("-MojvcP025Ot19fEoiuc", "Ericsson/T-Mobile/SC - Overlay"),
    ("-NFxsiuiWiNkrq8_HH7O", "Ericsson/T-Mobile/SFL - Excalibur"),
    ("-NhlLuPPMSZbRDi4mxwt", "Ericsson/T-Mobile/STX"),
    ("-Olw36jWi0918LlriI3Z", "Ericsson/T-Mobile/STX- NSB Macro/Civil"),
    ("-Okn2Q3cYS5r98tIdoQk", "Ericsson/T-Mobile/TX - Generator Install"),
    ("-MRQQb266CoA7lBL5efy", "Ericsson/T-Mobile/UPNY - Overlay"),
    ("-NIE8INGjClhN9_G3y7F", "Ericsson/T-Mobile/VA - Decom"),
    ("-N6nWJz-oSY-JAUkUjlp", "Ericsson/T-Mobile/VA - Overlay"),
    ("-NXtqiF07uYitz-z8Rlt", "Ericsson/T-Mobile/WV - Microwave"),
    ("-Mymx2EVRdRyeP4eHhoG", "Ericsson/T-Mobile/WV - Overlay"),
    ("-NEg4px03wOVYGVFQG3y", "Ericsson/Viaero/CO-NE"),
    ("-NhmLZOwhwj0y-n5Ibvi", "VZW/CAR-TN - Embedded"),
    ("-Mb7iVV00Z5ZTy-W27jH", "VZW/CGC - Embedded"),
    ("-NSM2Xk7eRj-Ifjzit7U", "VZW/CGC - NSB Macro"),
    ("-MSdEk1C6R0fqWVWIkZY", "VZW/CGC - Small Cell"),
    ("-ONv85b-0NaRQVYVQhQm", "VZW/CTX - Embedded"),
    ("-NxnvFPrYuhqXdJFJx4B", "VZW/FL - Embedded"),
    ("-O7aA5VQzI0yI6QEJ349", "VZW/FL - Ground Scope"),
    ("-NhmLbH13lz1R6R4dsdp", "VZW/GA-AL - Embedded"),
    ("-OHETpPon_dQXuQ5fptO", "VZW/Mountain Plains - Embedded"),
    ("-NCvoSShuHnMwymAawxK", "VZW/OPW - Embedded"),
    ("-OinpJcOAUjl1R2IOTib", "VZW/OPW - NSB Macro"),
    ("-MjffgYBPT-ZhUSr3slc", "VZW/SOVA - Embedded"),
    ("-Oj6OhDRwnwQptPvfxDy", "VZW/SOVA - NSB Macro"),
    ("-MXJTwDGZOn-uJYv0EL-", "VZW/SOVA - Small Cell"),
]


def auth() -> str:
    payload = {
        "grantType": "password",
        "include": ["profile", "firebaseToken"],
        "username": EMAIL,
        "password": PASSWORD,
        "scope": "openid",
    }
    r = requests.post(AUTH_URL, headers={"Content-Type": "application/json"},
                      json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["idToken"]


def probe_project(token: str, pid: str, name: str) -> dict:
    """Count total rows for one project, with caps."""
    headers = {"Authorization": f"Bearer {token}"}
    params = {"pageSize": PAGE_SIZE, "dateFormat": "yyyy-MM-dd", "timezone": "America/New_York"}
    rows = 0
    pages = 0
    after_ap = None
    after_id = None
    start = time.monotonic()
    first_ap = None
    last_ap = None

    while True:
        elapsed = time.monotonic() - start
        if pages >= CAP_PAGES or elapsed >= CAP_SECONDS:
            return {
                "project_did": pid, "project_name": name,
                "rows": rows, "pages": pages, "capped": True,
                "elapsed_s": round(elapsed, 1),
                "first_approved": first_ap, "last_approved": last_ap,
            }

        if after_ap and after_id:
            params["afterAp"] = after_ap
            params["afterId"] = after_id

        try:
            r = requests.get(EXPORT.format(pid=pid), headers=headers, params=params, timeout=60)
        except Exception as e:
            return {
                "project_did": pid, "project_name": name,
                "rows": rows, "pages": pages, "capped": False,
                "error": f"{type(e).__name__}: {e}",
                "elapsed_s": round(time.monotonic() - start, 1),
            }

        if r.status_code == 204:
            return {"project_did": pid, "project_name": name, "rows": rows, "pages": pages,
                    "capped": False, "elapsed_s": round(time.monotonic() - start, 1),
                    "first_approved": first_ap, "last_approved": last_ap}
        if r.status_code != 200:
            return {"project_did": pid, "project_name": name, "rows": rows, "pages": pages,
                    "capped": False, "error": f"HTTP {r.status_code}",
                    "elapsed_s": round(time.monotonic() - start, 1)}

        body = r.json()
        page_rows = body.get("list", [])
        if not page_rows:
            return {"project_did": pid, "project_name": name, "rows": rows, "pages": pages,
                    "capped": False, "elapsed_s": round(time.monotonic() - start, 1),
                    "first_approved": first_ap, "last_approved": last_ap}

        if pages == 0:
            first_ap = page_rows[0].get("Task_Approved_On")
        last_ap = page_rows[-1].get("Task_Approved_On")
        rows += len(page_rows)
        pages += 1

        nxt = body.get("next")
        if not nxt:
            return {"project_did": pid, "project_name": name, "rows": rows, "pages": pages,
                    "capped": False, "elapsed_s": round(time.monotonic() - start, 1),
                    "first_approved": first_ap, "last_approved": last_ap}
        after_ap = nxt.get("ap")
        after_id = nxt.get("id")


def main():
    print(f"[{datetime.now():%H:%M:%S}] Authenticating...")
    token = auth()
    print(f"[{datetime.now():%H:%M:%S}] Probing {len(PROJECTS)} TSC projects, "
          f"max {MAX_WORKERS} parallel, cap {CAP_PAGES} pages / {CAP_SECONDS}s each\n")

    results = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(probe_project, token, pid, name): (pid, name)
                   for pid, name in PROJECTS}
        for i, fut in enumerate(as_completed(futures), 1):
            pid, name = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                res = {"project_did": pid, "project_name": name, "error": str(e)}
            results.append(res)
            cap = "(CAPPED)" if res.get("capped") else ""
            err = f"ERR={res.get('error')}" if res.get("error") else ""
            print(f"[{datetime.now():%H:%M:%S}] {i:>2}/{len(PROJECTS)} "
                  f"{res.get('rows', 0):>9,} rows  {cap:8}  {name}  {err}")

    # Sort by rows desc
    results.sort(key=lambda r: r.get("rows", 0), reverse=True)

    out_path = "probe_tsc_sizes_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\n[{datetime.now():%H:%M:%S}] Done. Results -> {out_path}\n")

    # Bucket summary
    buckets = {"tiny (<1K)": [], "small (1K-10K)": [], "medium (10K-100K)": [],
               "large (100K-200K)": [], "huge (>=200K, capped)": [], "error": []}
    total_known = 0
    for r in results:
        if r.get("error"):
            buckets["error"].append(r)
        elif r.get("capped"):
            buckets["huge (>=200K, capped)"].append(r); total_known += r["rows"]
        elif r["rows"] < 1000:
            buckets["tiny (<1K)"].append(r); total_known += r["rows"]
        elif r["rows"] < 10000:
            buckets["small (1K-10K)"].append(r); total_known += r["rows"]
        elif r["rows"] < 100000:
            buckets["medium (10K-100K)"].append(r); total_known += r["rows"]
        else:
            buckets["large (100K-200K)"].append(r); total_known += r["rows"]

    print("=" * 70)
    print("SIZE DISTRIBUTION")
    print("=" * 70)
    for label, items in buckets.items():
        if not items:
            continue
        print(f"\n{label}: {len(items)} project(s)")
        for r in items:
            print(f"  {r.get('rows', 0):>9,}  {r['project_name']}")

    print(f"\nTOTAL rows observed (capped projects counted at cap): {total_known:,}")
    huge_count = len(buckets["huge (>=200K, capped)"])
    if huge_count:
        print(f"Real total includes {huge_count} 'huge' project(s) capped at 200K — "
              f"actual sum could be 2-10x higher than observed.")


if __name__ == "__main__":
    main()
