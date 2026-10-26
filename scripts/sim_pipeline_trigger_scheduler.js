// Offline check of the SCHEDULE/runScheduler logic in pipeline_trigger.gs: stubs the Apps Script
// services, runs setupScheduler() against the 2026-10-26 list of 20 triggers, then ticks every 5 min
// (with jitter) for 8 days across the US DST end and prints each dispatch in ET and PHT.
// Usage: node scripts/sim_pipeline_trigger_scheduler.js
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(require('path').join(__dirname, 'pipeline_trigger.gs'), 'utf8');
let NOW = new Date('2026-10-28T14:00:00Z');           // Wed 10 AM ET
const props = {}, fired = [], logs = [];
let failNext = 0;
function fmt(d, tz, pat) {
  const parts = Object.fromEntries(new Intl.DateTimeFormat('en-US', {timeZone: tz, year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23', weekday:'short'}).formatToParts(d).map(p => [p.type, p.value]));
  const dow = {Mon:1,Tue:2,Wed:3,Thu:4,Fri:5,Sat:6,Sun:7}[parts.weekday];
  return {'yyyy-MM-dd': `${parts.year}-${parts.month}-${parts.day}`, 'HH:mm': `${parts.hour}:${parts.minute}`, 'u': String(dow), 'H': String(+parts.hour)}[pat];
}
let triggers = ['triggerTimerEmailsSecond','triggerAssetTasks','triggerOrgs','triggerHolidayFeedWatch','triggerOpenItemsData','triggerCalendarEvents','triggerScheduleChangesSync','triggerDuplicateAssetWatch','triggerAssetTasksGC','triggerPackageScrape','checkForRevenueReports','triggerAssetTasksIncFullWalk','triggerLightPipelines','triggerCalendarEvents','triggerPrioritiesDaily','triggerScheduleFeedAudit','triggerPriorities','triggerForms','triggerScheduleChangesSync','triggerTimerEmails'].map(h => ({getHandlerFunction: () => h}));
const builder = {timeBased(){return this}, everyMinutes(n){this.n=n; return this}, create(){ triggers.push({getHandlerFunction: () => 'runScheduler'}); }};
const ctx = {
  Utilities: {formatDate: (d, tz, p) => fmt(d, tz, p), sleep(){}},
  PropertiesService: {getScriptProperties: () => ({getProperty: k => k === 'GITHUB_TOKEN' ? 'x' : (props[k] ?? null), setProperty: (k, v) => { props[k] = v; }})},
  LockService: {getScriptLock: () => ({tryLock: () => true, releaseLock(){}})},
  Logger: {log: m => logs.push(m)},
  UrlFetchApp: {fetch: (url, o) => { const ev = JSON.parse(o.payload).event_type; const ok = failNext > 0 ? (failNext--, false) : true; if (ok) fired.push([NOW.toISOString(), ev, JSON.stringify(JSON.parse(o.payload).client_payload||{})]); return {getResponseCode: () => ok ? 204 : 401, getContentText: () => 'bad'}; }},
  ScriptApp: {getProjectTriggers: () => triggers, deleteTrigger: t => { triggers = triggers.filter(x => x !== t); }, newTrigger: () => Object.create(builder)},
  MailApp: {sendEmail(){}}, GmailApp: {}, CalendarApp: {},
  Date: class extends Date { constructor(...a){ a.length ? super(...a) : super(NOW.getTime()); } },
};
vm.createContext(ctx); vm.runInContext(src, ctx);
// 1. setupScheduler at a due time must refuse
NOW = new Date('2026-10-28T13:05:00Z'); // 09:05 ET -> duplicate watch due
try { ctx.setupScheduler(); console.log('FAIL: did not refuse'); } catch (e) { console.log('refuses when due:', e.message.slice(0, 90)); }
NOW = new Date('2026-10-28T15:00:00Z'); // 11 AM ET
ctx.setupScheduler();
console.log(logs.at(-1));
// 2. tick every 5 min (+ random 0-60 s jitter) for 8 days across DST end (Nov 1)
const end = new Date('2026-11-05T15:00:00Z');
failNext = 0; let failedOnce = false;
for (let t = NOW.getTime() + 37000; t < end.getTime(); t += 300000) {
  NOW = new Date(t + Math.floor(Math.random() * 60000));
  if (!failedOnce && fmt(NOW, 'America/New_York', 'HH:mm') >= '22:13' && fmt(NOW,'America/New_York','yyyy-MM-dd') === '2026-10-29') { failNext = 1; failedOnce = true; }
  ctx.runScheduler();
}
const byEv = {};
for (const [iso, ev, pl] of fired) {
  const d = new Date(iso);
  const k = ev + ' ' + pl;
  (byEv[k] = byEv[k] || []).push(fmt(d,'America/New_York','yyyy-MM-dd') + ' ' + fmt(d,'America/New_York','HH:mm') + ' ET / ' + fmt(d,'Asia/Manila','HH:mm') + ' PHT');
}
for (const [k, v] of Object.entries(byEv)) console.log(`\n${k}  (${v.length}x)\n   ` + v.join('\n   '));
console.log('\nretry log:', logs.filter(l => /failed; retrying/.test(l)));

// Spring-forward check: on 2027-03-14 ET jumps 01:59 -> 03:00; open_items_data (02:00) must still fire.
{
  const before = fired.length;
  for (let t = Date.parse('2027-03-14T05:00:00Z'); t < Date.parse('2027-03-14T09:00:00Z'); t += 300000) { NOW = new Date(t + 20000); ctx.runScheduler(); }
  const oi = fired.slice(before).filter(f => f[1] === 'pipeline-open-items-data').map(f => f[0]);
  console.log('\nspring-forward 2027-03-14 open-items fired at:', oi.length ? oi : 'NOT FIRED (FAIL)');
}
