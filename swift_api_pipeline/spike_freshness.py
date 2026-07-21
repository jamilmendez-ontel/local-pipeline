#!/usr/bin/env python3
"""Read-only freshness spike: (A) conditional/delta requests, (B) Firebase realtime path.

See docs/spikes/2026-07-13-freshness-spike-etag-firebase.md for the checklist this
implements. Prints NO token values — only claim names, expiry deltas, status codes.

Usage: python _spike_freshness.py   (SWIFT_EMAIL / SWIFT_PASSWORD from .env)
"""
import base64
import json
import re
import time

import requests

from config import SWIFT_BASE_URL, SWIFT_USERNAME, SWIFT_PASSWORD

PROJECT_DID = "-OmzvGwfYsSskngv6SEo"  # TS19 (smallest pilot project)
TIMEOUT = 30


def jwt_payload(token: str) -> dict:
    """Decode a JWT payload without verifying (inspection only)."""
    try:
        seg = token.split(".")[1]
        seg += "=" * (-len(seg) % 4)
        return json.loads(base64.urlsafe_b64decode(seg))
    except Exception as e:
        return {"_decode_error": str(e)}


def describe_token(name: str, token: str):
    p = jwt_payload(token)
    if "_decode_error" in p:
        print(f"  {name}: not a decodable JWT ({p['_decode_error']}); length={len(token)}")
        return p
    exp_min = (p.get("exp", 0) - time.time()) / 60 if p.get("exp") else None
    print(f"  {name}: claims={sorted(p.keys())}")
    print(f"    iss={p.get('iss')}  aud={p.get('aud')}  "
          f"exp_in_min={exp_min:.0f}" if exp_min else f"    iss={p.get('iss')}  aud={p.get('aud')}")
    return p


def main():
    print("=== AUTH ===")
    r = requests.post(
        f"{SWIFT_BASE_URL}/api/auth/token",
        json={"grantType": "password", "include": ["profile", "firebaseToken"],
              "username": SWIFT_USERNAME, "password": SWIFT_PASSWORD, "scope": "openid"},
        headers={"Content-Type": "application/json"}, timeout=TIMEOUT)
    r.raise_for_status()
    auth = r.json()
    print(f"  auth response keys: {sorted(auth.keys())}")
    id_token = auth["idToken"]
    fb_token = auth.get("firebaseToken")
    describe_token("idToken", id_token)
    fb_claims = describe_token("firebaseToken", fb_token) if fb_token else {}
    headers = {"Authorization": f"Bearer {id_token}"}

    # ---------------- Spike A ----------------
    print("\n=== SPIKE A: conditional / delta requests on the assets list ===")
    url = f"{SWIFT_BASE_URL}/api/projects/{PROJECT_DID}/assets"
    base_params = {"page": 1, "pageSize": 100}
    r1 = requests.get(url, headers=headers, params=base_params, timeout=TIMEOUT)
    print(f"  baseline GET: {r1.status_code}; interesting headers: "
          f"{ {k: v for k, v in r1.headers.items() if k.lower() in ('etag', 'last-modified', 'cache-control', 'vary', 'x-total-count')} }")
    body = r1.json()
    rows = body.get("list", body if isinstance(body, list) else [])
    n_base = len(rows)
    print(f"  baseline rows: {n_base}; top-level keys: {sorted(body.keys()) if isinstance(body, dict) else 'list'}")
    if rows:
        row_keys = sorted(rows[0].keys())
        print(f"  row keys: {row_keys}")
        etag_like = [k for k in row_keys if "tag" in k.lower() or k.lower() in ("_etag", "version", "rev")]
        upd_like = [k for k in row_keys if "updat" in k.lower() or "modif" in k.lower()]
        print(f"  row-level etag-like: {etag_like}; updated-like: {upd_like}")

    etag = r1.headers.get("ETag")
    if etag:
        r2 = requests.get(url, headers={**headers, "If-None-Match": etag},
                          params=base_params, timeout=TIMEOUT)
        print(f"  If-None-Match: {r2.status_code}  "
              f"({'304 WIN — conditional GET honored' if r2.status_code == 304 else 'not honored (full body)'})")
    else:
        print("  no response-level ETag header — conditional GET not available")

    cutoff_ms = int((time.time() - 3600 * 24 * 7) * 1000)  # 7 days ago
    for param in ("updatedSince", "modifiedSince", "updatedAfter", "since"):
        rp = requests.get(url, headers=headers,
                          params={**base_params, param: cutoff_ms}, timeout=TIMEOUT)
        n = len(rp.json().get("list", [])) if rp.status_code == 200 else None
        verdict = "IGNORED (same count)" if n == n_base else f"count={n} <-- CHECK"
        print(f"  ?{param}={cutoff_ms}: {rp.status_code} {verdict}")

    for sortp in ({"sort": "lastUpdated"}, {"sort": "-lastUpdated"},
                  {"orderBy": "lastUpdated", "order": "desc"}, {"sort": "lastUpdated,desc"}):
        rs = requests.get(url, headers=headers, params={**base_params, **sortp}, timeout=TIMEOUT)
        rows_s = rs.json().get("list", []) if rs.status_code == 200 else []
        upd_field = next((k for k in (rows_s[0].keys() if rows_s else []) if "updat" in k.lower()), None)
        firsts = [row.get(upd_field) for row in rows_s[:3]] if upd_field else "n/a"
        print(f"  ?{sortp}: {rs.status_code} first3 {upd_field}={firsts}")

    # ---------------- Spike B ----------------
    print("\n=== SPIKE B: Firebase realtime path ===")
    if not fb_token:
        print("  no firebaseToken in auth response — spike B dead at step 1")
        return

    # 1) find web config (apiKey + databaseURL) in the public web app bundle
    api_key = None
    db_urls = set()
    try:
        app_html = requests.get("https://swiftprojects.io", timeout=TIMEOUT).text
        js_paths = re.findall(r'src="([^"]+\.js)"', app_html)
        blobs = [app_html]
        for p in js_paths[:6]:
            full = p if p.startswith("http") else f"https://swiftprojects.io/{p.lstrip('/')}"
            try:
                blobs.append(requests.get(full, timeout=TIMEOUT).text)
            except requests.RequestException:
                pass
        for blob in blobs:
            api_key = api_key or next(iter(re.findall(r'apiKey["\']?\s*[:=]\s*["\'](AIza[0-9A-Za-z_-]{20,})', blob)), None)
            db_urls.update(re.findall(r'https://[a-z0-9-]+\.(?:firebaseio\.com|[a-z0-9-]+\.firebasedatabase\.app)', blob))
        print(f"  web bundle: apiKey={'FOUND' if api_key else 'not found'}; databaseURLs={sorted(db_urls) or 'none'}")
    except requests.RequestException as e:
        print(f"  web bundle fetch failed: {e}")

    # candidate DB URLs from token project id if bundle gave nothing
    proj = None
    iss = str(fb_claims.get("iss", ""))
    m = re.search(r"@([a-z0-9-]+)\.iam\.gserviceaccount\.com", iss)
    if m:
        proj = m.group(1)
    elif isinstance(fb_claims.get("aud"), str) and "securetoken" in fb_claims["aud"]:
        proj = fb_claims["aud"].rsplit("/", 1)[-1]
    print(f"  firebase project id (from token): {proj}")
    if proj and not db_urls:
        db_urls = {f"https://{proj}.firebaseio.com",
                   f"https://{proj}-default-rtdb.firebaseio.com",
                   f"https://{proj}-default-rtdb.asia-southeast1.firebasedatabase.app"}

    # 2) exchange custom token if needed
    fb_id_token = None
    aud = str(fb_claims.get("aud", ""))
    if "identitytoolkit" in aud:
        print("  token type: CUSTOM token (needs signInWithCustomToken exchange)")
        if api_key:
            rx = requests.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken?key={api_key}",
                json={"token": fb_token, "returnSecureToken": True}, timeout=TIMEOUT)
            print(f"  exchange: {rx.status_code}"
                  + ("" if rx.status_code == 200 else f" body={rx.text[:200]}"))
            if rx.status_code == 200:
                fb_id_token = rx.json()["idToken"]
                print(f"  got firebase idToken (expiresIn={rx.json().get('expiresIn')}s) + refreshToken={'refreshToken' in rx.json()}")
        else:
            print("  no apiKey found headlessly -> exchange not attempted "
                  "(manual: DevTools > search 'firebaseConfig' in app bundle)")
    else:
        print("  token type: looks like a ready-to-use ID token")
        fb_id_token = fb_token

    # 3) probe RTDB URLs
    if fb_id_token:
        for db in sorted(db_urls):
            for path in (f"/projects/{PROJECT_DID}", f"/assets", "/"):
                try:
                    pr = requests.get(f"{db}{path}.json",
                                      params={"shallow": "true", "auth": fb_id_token}, timeout=15)
                    print(f"  {db}{path}.json -> {pr.status_code} {pr.text[:120]!r}")
                    if pr.status_code == 200:
                        break
                except requests.RequestException as e:
                    print(f"  {db}{path}.json -> EXC {e}")
                    break  # host-level failure, skip other paths
    else:
        print("  no usable firebase idToken -> RTDB probe skipped")

    print("\nDone. Fill results into docs/spikes/2026-07-13-freshness-spike-etag-firebase.md")


if __name__ == "__main__":
    main()
