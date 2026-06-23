/**
 * Apps Script — Daily Reports Pipeline Trigger
 * Fires repository_dispatch to run the daily reports pipeline on schedule.
 *
 * Setup:
 * 1. Create a standalone Apps Script at script.google.com (or bind to any sheet)
 * 2. Paste this code
 * 3. Set GITHUB_TOKEN in Script Properties (same PAT used for other triggers)
 * 4. Create two time-based triggers:
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
 * (status/approval + hours + timers) in one job. Install as an every-10-min
 * time-based trigger. Replaces the daily + requirements split once that pair
 * is retired (retirement is a separate, manual step — leave triggerDaily /
 * triggerRequirements installed until then).
 */
function triggerDailyReportsRolling() {
  fireRollingDispatch(30);
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
