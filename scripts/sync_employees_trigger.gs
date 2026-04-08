/**
 * Apps Script for Employee Reference Google Sheet
 * Triggers GHA workflow to sync sheet data to Supabase
 *
 * Setup:
 * 1. Open the Google Sheet
 * 2. Extensions → Apps Script
 * 3. Paste this code
 * 4. Set GITHUB_PAT (same PAT used for other triggers)
 * 5. Run onOpen() once to create the menu
 */

const GITHUB_OWNER = "jamilmendez-ontel";
const GITHUB_REPO = "local-pipeline";
// PAT stored in Script Properties → Project Settings → Script Properties → GITHUB_TOKEN
function getToken() {
  var token = PropertiesService.getScriptProperties().getProperty('GITHUB_TOKEN');
  if (!token) {
    SpreadsheetApp.getUi().alert("❌ GITHUB_TOKEN not set. Go to Project Settings → Script Properties.");
    throw new Error("Missing GITHUB_TOKEN");
  }
  return token;
}

/**
 * Add custom menu to the sheet
 */
function onOpen() {
  SpreadsheetApp.getUi()
    .createMenu("⚡ Sync")
    .addItem("Sync to Supabase", "triggerSync")
    .addItem("Sync with Date", "triggerSyncWithDate")
    .addToUi();
}

/**
 * Trigger GHA workflow to sync employees
 */
function triggerSync() {
  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/dispatches`;

  const options = {
    method: "post",
    headers: {
      "Authorization": `Bearer ${getToken()}`,
      "Accept": "application/vnd.github.v3+json",
    },
    contentType: "application/json",
    payload: JSON.stringify({
      event_type: "sync-employees",
    }),
    muteHttpExceptions: true,
  };

  const response = UrlFetchApp.fetch(url, options);
  const code = response.getResponseCode();

  if (code === 204) {
    SpreadsheetApp.getUi().alert("✅ Sync triggered! Check GitHub Actions for progress.");
  } else {
    SpreadsheetApp.getUi().alert(`❌ Failed (${code}): ${response.getContentText()}`);
  }
}

/**
 * Trigger sync with a specific effective date
 */
function triggerSyncWithDate() {
  const ui = SpreadsheetApp.getUi();
  const result = ui.prompt(
    "Effective Date",
    "Enter the effective date for changes (YYYY-MM-DD):",
    ui.ButtonSet.OK_CANCEL
  );

  if (result.getSelectedButton() !== ui.Button.OK) return;

  const dateStr = result.getResponseText().trim();
  if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
    ui.alert("❌ Invalid date format. Use YYYY-MM-DD.");
    return;
  }

  const url = `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/dispatches`;

  const options = {
    method: "post",
    headers: {
      "Authorization": `Bearer ${getToken()}`,
      "Accept": "application/vnd.github.v3+json",
    },
    contentType: "application/json",
    payload: JSON.stringify({
      event_type: "sync-employees",
      client_payload: {
        effective_date: dateStr,
      },
    }),
    muteHttpExceptions: true,
  };

  const response = UrlFetchApp.fetch(url, options);
  const code = response.getResponseCode();

  if (code === 204) {
    ui.alert(`✅ Sync triggered with effective date ${dateStr}!`);
  } else {
    ui.alert(`❌ Failed (${code}): ${response.getContentText()}`);
  }
}
