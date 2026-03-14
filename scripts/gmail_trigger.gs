/**
 * Gmail Trigger for GitHub Actions — Gmail Revenue Pipelines
 *
 * Monitors Gmail for unread "Daily Revenue Report" emails and fires a
 * repository_dispatch event to kick off the aging + sales pipelines on GHA.
 *
 * Setup:
 *   1. Create Apps Script project under jamil.mendez@nanoninth.com
 *   2. Paste this script
 *   3. Add Script Property: GITHUB_TOKEN = <fine-grained PAT with contents:read+write on local-pipeline>
 *   4. Create time-driven trigger: checkForRevenueReports(), every 5 minutes
 *   5. Run scheduleTokenRotationReminder() once to create a calendar reminder 5 days before PAT expiry
 */

function checkForRevenueReports() {
  var threads = GmailApp.search('subject:"Daily Revenue Report" has:attachment is:unread', 0, 10);

  if (threads.length === 0) {
    return;
  }

  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    Logger.log('ERROR: GITHUB_TOKEN not set in Script Properties');
    return;
  }

  var dispatched = false;

  for (var i = 0; i < threads.length; i++) {
    var thread = threads[i];
    var subject = thread.getFirstMessageSubject();
    Logger.log('Found unread revenue report: ' + subject);

    // Fire dispatch only once per invocation (all unread emails trigger the same pipeline)
    if (!dispatched) {
      var success = fireRepositoryDispatch(token);
      if (!success) {
        Logger.log('ERROR: repository_dispatch failed — skipping mark-as-read');
        return;
      }
      dispatched = true;
    }

    // Mark thread as read so it doesn't re-trigger
    thread.markRead();
  }

  Logger.log('Dispatched gmail-revenue-report event, marked ' + threads.length + ' thread(s) as read');
}

function fireRepositoryDispatch(token) {
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
      event_type: 'gmail-revenue-report'
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
 * Run this function ONCE after creating the PAT to schedule a calendar
 * reminder 5 days before the 90-day expiry. Creates an all-day event
 * with an email reminder on the nanoninth.com account's default calendar.
 */
function scheduleTokenRotationReminder() {
  var EXPIRY_DAYS = 90;
  var REMINDER_DAYS_BEFORE = 5;

  var reminderDate = new Date();
  reminderDate.setDate(reminderDate.getDate() + EXPIRY_DAYS - REMINDER_DAYS_BEFORE);

  var event = CalendarApp.getDefaultCalendar().createAllDayEvent(
    'Rotate GitHub PAT for Pipeline Triggers',
    reminderDate,
    {
      description:
        'The fine-grained GitHub PAT (local-pipeline repo, contents:read+write) expires in 5 days.\n\n' +
        'This PAT is used by ALL pipeline triggers in Apps Script:\n' +
        '- Gmail Revenue Pipelines (gmail_trigger.gs)\n' +
        '- Orgs, Timer, Priorities, Forms (pipeline_trigger.gs)\n\n' +
        'Steps:\n' +
        '1. Go to https://github.com/settings/tokens and generate a new 90-day PAT\n' +
        '   - Repository: local-pipeline only\n' +
        '   - Permissions: Contents → Read and Write\n' +
        '   - Expiration: 90 days\n' +
        '2. Update GITHUB_TOKEN in Apps Script project settings (Script Properties)\n' +
        '3. Run scheduleTokenRotationReminder() again to set the next reminder'
    }
  );

  event.addEmailReminder(0);       // At start of day
  event.addEmailReminder(24 * 60); // 1 day before

  Logger.log('Rotation reminder created for ' + reminderDate.toDateString());
}
