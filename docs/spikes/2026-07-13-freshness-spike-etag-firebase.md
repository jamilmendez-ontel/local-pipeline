# Spike: delta requests (ETag/updatedSince) + Firebase realtime path — 2026-07-13

**Type:** read-only spike. No writes to Swift, no DB writes. Timebox: one session.
**Script:** `swift_api_pipeline/spike_freshness.py` (committed; rerunnable).
**Feeds:** Phase 3 freshness roadmap in
`docs/superpowers/plans/2026-07-09-incremental-asset-tasks-shadow.md`.

## Why (recap)

Goal is event-driven sync: Swift change → only that entity fetched → upserted in
Supabase. Two candidate mechanisms, tested cheapest-first:

- **Spike A — conditional/delta requests.** If `/api/projects/{did}/assets` honors
  `If-None-Match` (HTTP 304) or an `updatedSince`/`sort=lastUpdated` param, change
  *discovery* drops from ~45 s per project to ~2 s, and minute-level polling becomes
  viable with a resident worker. No new infrastructure concepts needed.
- **Spike B — Firebase Realtime push.** Swift's auth endpoint hands out a
  `firebaseToken` (we already request it: `include: ["profile","firebaseToken"]` in
  `base_extractor.py:45`, we just discard it), and every DID is a Firebase push ID.
  If that token can subscribe to asset/task nodes over SSE, changes arrive as push
  events and polling demotes to a safety net.

## How to run it yourself

```bash
cd swift_api_pipeline
python spike_freshness.py            # needs SWIFT_EMAIL / SWIFT_PASSWORD in .env
```

The script never prints token values — only claim names, expiry deltas, status codes,
and header names. Safe to paste output into a doc.

## What to check, where, and what "verified" means

### Spike A (HTTP layer — all in response headers/body of the assets list)

| Check | Where to look | Verified when |
|---|---|---|
| Response-level ETag | `ETag` / `Last-Modified` headers on `GET /api/projects/{did}/assets` | Header present |
| Conditional GET | Re-request with `If-None-Match: <etag>` | **HTTP 304** returned (the win); 200 with full body = not honored |
| Row-level etag | JSON of each asset row (`etag`-like key) | Field exists per row (enables per-entity conditional fetch) |
| Delta params | Same endpoint + `updatedSince`/`modifiedSince`/`updatedAfter` (epoch ms) | Row count shrinks vs baseline AND rows all have `lastUpdated >= cutoff`. Unknown params are usually *silently ignored* — equal counts means ignored, not working |
| Server-side sort | `sort=lastUpdated` / `orderBy=lastUpdated` variants | First page comes back newest-first (then "walk until watermark" replaces full pagination) |

### Spike B (Firebase layer)

| Check | Where to look | Verified when |
|---|---|---|
| Token present | auth response JSON keys | `firebaseToken` key exists |
| Token type | JWT payload (base64-decode middle segment — script does it) | `aud` ends in `identitytoolkit` ⇒ **custom token** (must be exchanged before use); `iss` reveals the Firebase **project id** |
| Web API key | Swift web app JS bundle (script fetches `swiftprojects.io` and greps `apiKey`/`databaseURL`); manually: DevTools → Sources → search `firebaseConfig` | An `AIza…` key + a `firebaseio.com`/`firebasedatabase.app` URL found |
| Token exchange | `POST identitytoolkit …/accounts:signInWithCustomToken?key=<apiKey>` | Returns a Firebase `idToken` (1 h TTL) + `refreshToken` |
| RTDB reachable | `GET https://<db>/.json?shallow=true&auth=<firebase idToken>` | 200 = readable (scope!), 401 = exists but rules block us, 404 = wrong URL |
| SSE stream | Same URL with `Accept: text/event-stream`, keep open ~15 s | `event: put` frames arrive; touching an asset in the Swift UI mid-stream produces a `patch` event (the real proof — needs a human clicking, i.e., you) |

**Manual verification you can do in the background (browser, no code):** open the Swift
web app → DevTools → Network tab → filter `firebaseio` or `firebasedatabase` or WS →
watch which URLs/nodes the app itself subscribes to. Whatever path the app listens on
is the path our listener should use. Screenshot/copy the URL patterns (strip tokens).

## Decision rule

- A wins (304s or working delta param) → resident worker = 1-min poller. Simple, documented surface.
- B wins (SSE stream delivers events) → resident worker = Firebase listener + hourly reconciliation walk. True push.
- Both fail → resident worker = current walk at 5-min cadence (already measured viable); revisit after vendor contact.

## Results (run 2026-07-13, TS19 `-OmzvGwfYsSskngv6SEo`)

### Spike A — server-side delta filtering: NOT AVAILABLE
- No response-level `ETag`/`Last-Modified` header → **no conditional GET (304)**.
- `updatedSince` / `modifiedSince` / `updatedAfter` / `since` params → **silently ignored**
  (identical row count to baseline; classic "unknown param dropped" behavior).
- `sort` / `orderBy=lastUpdated` (all 4 variants) → **ignored** (first 3 rows identical
  across every variant). No server-side sort → can't "walk newest until watermark".
- BUT every asset row carries **`lastUpdated`** (epoch ms) and a per-row **`ETag`**.
  So client-side delta is possible, but discovery still requires pulling the full list —
  this is exactly today's ~45 s bottleneck and the REST API can't shrink it. **A loses.**

### Spike B — Firebase realtime path: REACHABLE (the win)
- `firebaseToken` is present in every auth response (we currently discard it). It's an
  **Auth0-minted JWT** (`iss=uplink.auth0.com`), not a Google custom token — so **no
  `signInWithCustomToken` exchange is needed**; Swift has configured Firebase to trust
  Auth0 as a JWT auth provider. TTL = 4320 min (**72 h**); refresh = just re-auth.
- RTDB URL discovered from the public web bundle: **`https://swift-projects.firebaseio.com`**.
- `GET /projects/{did}.json?auth=<firebaseToken>` → **200 with live data**. The project
  node exposes `organization, name, locationOrientation, lastUpdated, status, createdBy,
  description, dateCreated, isPrivate`. **`lastUpdated` on this node is a free per-project
  change signal.**
- `GET /projects/{did}.json` with `Accept: text/event-stream` → **stream opens (HTTP 200)**
  and holds the connection (SSE working; our 8 s probe window closed it before a keep-alive
  frame — expected for a quiet node).
- Root `/assets` and `/tasks` → **401 Permission denied** (rules block root listing);
  `/assets/{did}` and `/tasks/{did}` → 200 `null` (not the real path). **The asset/task
  node scheme is not yet known** — see manual step below.

### Decision
**Pursue Spike B.** Intermediate architecture available *today* with what's proven:
stream `/projects/{did}` `lastUpdated` over SSE = the EVENT → on change, run the existing
targeted hierarchy walk for just that project → guarded upsert. This already replaces
"poll all projects hourly" with "instant per-project trigger, zero cost while quiet" —
proper and event-driven, no workaround. Tightening to per-*asset* events needs the node
scheme below.

### Open — needs one manual step (Jamil, background, browser only)
Find the RTDB path the Swift app itself subscribes to for asset/task changes:
1. Log into the Swift web app, open **DevTools → Network**, filter **`firebaseio`** (and
   check the **WS** tab).
2. Open a project, click into an asset. Watch the streaming/long-poll requests to
   `swift-projects.firebaseio.com` — the **path** in those URLs (e.g. `/orgs/…/assets/…`)
   is where change events live. Copy the path patterns (tokens can stay redacted).
3. Paste them here; the listener subscribes to exactly those nodes.

Re-run anytime: `python swift_api_pipeline/spike_freshness.py`.
