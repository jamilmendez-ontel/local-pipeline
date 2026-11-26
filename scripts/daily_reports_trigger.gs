/**
 * Apps Script — Daily Reports Pipeline Trigger
 * Fires repository_dispatch to run the daily reports pipeline on schedule.
 *
 * Setup:
 * 1. Create a standalone Apps Script at script.google.com (or bind to any sheet)
 * 2. Paste this code
 * 3. Set GITHUB_TOKEN in Script Properties (same PAT used for other triggers)
 * 4. Create the time-based triggers below. triggerDailyReportsRolling (every
 *    5 min) also starts the OnPoint loader about every 15 min (2026-11-26).
 *    Older notes:
 *    - triggerDaily: runs every day in the 3-4 AM EST window
 *    - triggerRequirements: runs every day in the 3-4 AM EST window
 *      (the function checks if today is 2nd-5th or 17th-20th before firing)
 *
 * Trigger setup:
 *   Edit → Triggers → Add Trigger
 *   - Function: triggerDaily → Time-driven → Day timer → 3am to 4am
 *   - Function: triggerRequirements → Time-driven → Day timer → 3am to 4am
 *
 * NOTE (2026-06-05): moved from the midnight-1am window to 3-4 AM. The old
 * window overlapped the nightly pipeline burst (asset-tasks/timer/forms),
 * which (a) exhausted Supavisor's 15-client session-mode connection cap and
 * crashed the requirements run with EMAXCONNSESSION, and (b) meant the report
 * could read stg data before the asset-tasks pipeline finished refreshing it.
 * 3 AM clears both. See memory "the june 5 incident".
 */

const GITHUB_OWNER = "jamilmendez-ontel";
const GITHUB_REPO = "local-pipeline";

function getToken() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) throw new Error("Missing GITHUB_TOKEN in Script Properties");
  return token;
}

/**
 * Daily trigger — timers only (last 3 days)
 * Runs every day.
 */
function triggerDaily() {
  fireDispatch("daily", 3);
}

/**
 * Bi-monthly trigger — requirements for the closing period
 * Only fires on 2nd-5th and 17th-20th of the month.
 */
function triggerRequirements() {
  var day = new Date().getDate();
  if ((day >= 2 && day <= 5) || (day >= 17 && day <= 20)) {
    fireDispatch("requirements");
  } else {
    console.log("Not in bi-monthly window (day " + day + "), skipping.");
  }
}

/**
 * Fire repository_dispatch to GitHub Actions
 */
function fireDispatch(mode, days) {
  var payload = { mode: mode };
  if (days) payload.days = String(days);

  var url = "https://api.github.com/repos/" + GITHUB_OWNER + "/" + GITHUB_REPO + "/dispatches";
  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "Authorization": "token " + getToken(),
      "Accept": "application/vnd.github.v3+json"
    },
    payload: JSON.stringify({
      event_type: "daily-reports",
      client_payload: payload
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  if (code === 204) {
    console.log("✅ Dispatched daily-reports (mode=" + mode + ")");
  } else {
    console.log("❌ Dispatch failed: " + code + " " + response.getContentText());
  }
}

/**
 * Rolling trigger — refreshes the last 30 days of ALL daily-reports data
 * (status/approval + hours + timers) in one job. Install as an every-5-min
 * time-based trigger (was every-10-min; tightened 2026-06-30 — run is ~2 min
 * so 5-min leaves a safe non-overlapping gap; source data has huge headroom). Replaces the daily + requirements split once that pair
 * is retired (retirement is a separate, manual step — leave triggerDaily /
 * triggerRequirements installed until then).
 */
function triggerDailyReportsRolling() {
  fireRollingDispatch(30);
  // Rides along on this 5-min trigger (2026-11-26): GitHub's own cron started
  // the OnPoint loader only ~3 times a day instead of every 15 min. Any error
  // here is caught so it can never stop the rolling dispatch above.
  try {
    maybeFireOnpointDispatch();
  } catch (e) {
    console.log("❌ OnPoint dispatch error (rolling unaffected): " + e);
  }
}

/**
 * OnPoint daily reports loader (event: onpoint-daily-reports) every ~15 min.
 * Called from triggerDailyReportsRolling(), so it needs no trigger of its own.
 * A Script Property remembers the last successful dispatch; the next one goes
 * out once 14 minutes have passed (a 5-min trigger lands about every 15 min).
 * A failed dispatch is not recorded, so the next 5-min tick retries it.
 * The workflow keeps its GitHub cron as a backstop; extra runs are harmless
 * (each run re-pulls the last 14 days and upserts).
 */
var ONPOINT_DISPATCH_EVERY_MIN = 14;
var ONPOINT_LAST_PROP = "ONPOINT_LAST_DISPATCH_MS";

function maybeFireOnpointDispatch() {
  var props = PropertiesService.getScriptProperties();
  var last = Number(props.getProperty(ONPOINT_LAST_PROP) || 0);
  var now = Date.now();
  if (now - last < ONPOINT_DISPATCH_EVERY_MIN * 60 * 1000) {
    return false;
  }
  if (fireOnpointDispatch()) {
    props.setProperty(ONPOINT_LAST_PROP, String(now));
    return true;
  }
  return false;
}

/**
 * Fire repository_dispatch for the OnPoint loader. Also safe to run by hand.
 */
function fireOnpointDispatch() {
  var url = "https://api.github.com/repos/" + GITHUB_OWNER + "/" + GITHUB_REPO + "/dispatches";
  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "Authorization": "token " + getToken(),
      "Accept": "application/vnd.github.v3+json"
    },
    payload: JSON.stringify({ event_type: "onpoint-daily-reports" }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  if (code === 204) {
    console.log("✅ Dispatched onpoint-daily-reports");
    return true;
  }
  console.log("❌ OnPoint dispatch failed: " + code + " " + response.getContentText());
  return false;
}

/**
 * Weekly DEEP rolling run — same workflow, 90-day window. Belt-and-suspenders
 * for late edits to requirements (hours) and timers on reports older than the
 * 30-day window: the stale-status sweep (2026-08-07) catches late APPROVALS at
 * any age for free, but req/timer child data is only re-fetched in-window.
 * ~20 min run (vs ~8 at 30 days) — fine weekly, far too heavy for the 5-min
 * cadence. Install as a weekly time-based trigger:
 *   Function: triggerDailyReportsDeep → Time-driven → Week timer →
 *   Every Sunday, 5am-6am (ET — after the nightly pipeline burst and the
 *   3-4 AM daily/requirements window; see 2026-06-05 note above).
 */
function triggerDailyReportsDeep() {
  fireRollingDispatch(90);
}

/**
 * Fire repository_dispatch for the rolling workflow (event: daily-reports-rolling).
 */
function fireRollingDispatch(days) {
  var url = "https://api.github.com/repos/" + GITHUB_OWNER + "/" + GITHUB_REPO + "/dispatches";
  var options = {
    method: "post",
    contentType: "application/json",
    headers: {
      "Authorization": "token " + getToken(),
      "Accept": "application/vnd.github.v3+json"
    },
    payload: JSON.stringify({
      event_type: "daily-reports-rolling",
      client_payload: { days: String(days) }
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();
  if (code === 204) {
    console.log("✅ Dispatched daily-reports-rolling (days=" + days + ")");
  } else {
    console.log("❌ Rolling dispatch failed: " + code + " " + response.getContentText());
  }
}
