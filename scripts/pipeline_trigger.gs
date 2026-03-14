/**
 * Pipeline Triggers for GitHub Actions — Swift API Pipelines
 *
 * Fires repository_dispatch events on a schedule to trigger individual
 * pipeline workflows with zero delay (unlike GHA cron which can lag 1-2 hours).
 *
 * Setup:
 *   1. Open the existing Apps Script project under jamil.mendez@nanoninth.com
 *      (same project as gmail_trigger.gs — reuses GITHUB_TOKEN)
 *   2. Paste these functions into a new file (e.g. pipeline_trigger.gs)
 *   3. Create time-driven triggers in Apps Script:
 *      - triggerOrgs()           → Daily, 10:13 PM EST
 *      - triggerLightPipelines() → Daily, 12:09 AM EST
 *   4. The GITHUB_TOKEN script property is already set from gmail_trigger.gs
 *
 * Schedules (EST):
 *   10:13 PM  — Orgs & Projects
 *   12:09 AM  — Timer
 *   12:13 AM  — User Priorities
 *   12:17 AM  — QA Forms
 *
 * Note: triggerLightPipelines() fires timer at :09, priorities at :13, forms at :17
 * by using Utilities.sleep() for staggering. Apps Script has a 6-min execution limit
 * so the 8-min total stagger fits within one invocation.
 */

var REPO = 'jamilmendez-ontel/local-pipeline';

/**
 * Trigger orgs pipeline — schedule this at 10:13 PM EST daily.
 */
function triggerOrgs() {
  fireDispatch_('pipeline-orgs');
}

/**
 * Trigger all three light pipelines with staggered timing.
 * Schedule this at 12:09 AM EST daily.
 *
 * 12:09 AM — Timer fires immediately
 * 12:13 AM — User Priorities (4 min delay)
 * 12:17 AM — QA Forms (8 min delay from start)
 *
 * Note: Apps Script time-driven triggers have ±1 min jitter, but the
 * relative spacing between dispatches is exact.
 */
function triggerLightPipelines() {
  // Timer — fires immediately
  fireDispatch_('pipeline-timer');
  Logger.log('Waiting 4 minutes before triggering priorities...');

  // User Priorities — 4 min after timer
  Utilities.sleep(4 * 60 * 1000);
  fireDispatch_('pipeline-priorities');
  Logger.log('Waiting 4 minutes before triggering forms...');

  // QA Forms — 4 min after priorities (8 min after timer)
  Utilities.sleep(4 * 60 * 1000);
  fireDispatch_('pipeline-forms');
}

/**
 * Fire a repository_dispatch event. Reuses the GITHUB_TOKEN from Script Properties.
 */
function fireDispatch_(eventType) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  var url = 'https://api.github.com/repos/' + REPO + '/dispatches';

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({
      event_type: eventType
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 204) {
    Logger.log('Dispatched ' + eventType + ' successfully');
  } else {
    Logger.log('ERROR dispatching ' + eventType + ': HTTP ' + code + ' — ' + response.getContentText());
  }
}

/**
 * Test function — manually trigger all pipelines to verify setup.
 * Run this once after setup to confirm everything works.
 */
function testAllDispatches() {
  fireDispatch_('pipeline-orgs');
  fireDispatch_('pipeline-timer');
  fireDispatch_('pipeline-priorities');
  fireDispatch_('pipeline-forms');
  Logger.log('All 4 dispatches fired — check GitHub Actions.');
}
