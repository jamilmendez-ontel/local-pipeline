/**
 * Timer Correction Apply Trigger — Google Apps Script
 *
 * Fires a repository_dispatch event to GitHub Actions when a tech submits
 * a duration correction via the Timer Correction Google Form. This triggers
 * the apply workflow which reads the correction, stores it, and rebuilds
 * stg_timer_activities_clean with the corrected duration.
 *
 * Setup:
 *   1. Open the Timer Correction response spreadsheet
 *   2. Extensions > Apps Script
 *   3. Paste this script
 *   4. Add Script Property: GITHUB_TOKEN = <same PAT as other pipeline triggers>
 *      (fine-grained PAT with contents:read+write on local-pipeline)
 *   5. Set up trigger: Run > Triggers > Add Trigger
 *      - Function: onFormSubmit
 *      - Event source: From spreadsheet
 *      - Event type: On form submit
 */

function onFormSubmit(e) {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  // Cooldown: skip if we fired within the last 5 seconds (Google Forms double-fire)
  var cache = CacheService.getScriptCache();
  var lastFire = cache.get('last_correction_dispatch');
  if (lastFire) {
    Logger.log('Skipping duplicate dispatch (cooldown active)');
    return;
  }
  cache.put('last_correction_dispatch', 'true', 5);  // 5-second cooldown

  // Log what was submitted
  if (e && e.namedValues) {
    Logger.log('Correction form response received: ' + JSON.stringify(e.namedValues));
  }

  var success = fireCorrectionDispatch(token);
  if (success) {
    Logger.log('Correction apply workflow triggered successfully');
  }
}

function fireCorrectionDispatch(token) {
  var url = 'https://api.github.com/repos/jamilmendez-ontel/local-pipeline/dispatches';

  var options = {
    method: 'post',
    contentType: 'application/json',
    headers: {
      'Authorization': 'Bearer ' + token,
      'Accept': 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28'
    },
    payload: JSON.stringify({
      event_type: 'timer-correction-apply'
    }),
    muteHttpExceptions: true
  };

  var response = UrlFetchApp.fetch(url, options);
  var code = response.getResponseCode();

  if (code === 204) {
    Logger.log('repository_dispatch fired successfully');
    return true;
  } else {
    Logger.log('repository_dispatch failed: HTTP ' + code + ' — ' + response.getContentText());
    return false;
  }
}

/**
 * Manual test function — run this to verify the dispatch works.
 */
function testDispatch() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set');
    return;
  }
  fireCorrectionDispatch(token);
}
