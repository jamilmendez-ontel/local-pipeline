#!/usr/bin/env python3
"""Duplicate asset watcher: find Swift assets created twice (same project, same
path, same name, different asset ids), show how many timers each one hides,
and email which copy to keep.

Why: v1 has no asset id on a timer or a QA form. data_staging.backfill_asset_did()
matches on site name + path and leaves asset_did NULL when two assets share
both, so every timer on such a site loses its asset. Measured 2026-10-19: 58
duplicate groups (126 assets) in TS13-TS20; 87 v1 timers (45.0 h) NULL on 11
of them; TS20's WINCHESTER pair failed the timer v2 parity audit two nights
running. Deleting the empty copy in Swift fixes v1 on its next load.

Per group, an asset is ACTIVE when any task left 'pending' (or its task counts
are unknown) or a timer (v2, data_staging.stg_timer_activities_inc, which
carries Swift's own asset id) points at it. Verdicts:
  delete_empty  one active copy: KEEP it, DELETE the empty ones.
  all_empty     no active copy: KEEP the oldest (by the Swift id's creation
                time), DELETE the rest.
  merge_needed  two or more active copies: nothing is safe to delete; someone
                moves the work onto one copy first.
Impact per group = v1 timers (data_staging.stg_timer_activities) on the same
project + path + name whose asset_did is NULL: count, hours, last start.

Cadence (stateless, no run table): every run emails the groups whose newest
copy was created in the last NEW_HOURS, so the duplicate can be deleted before
anyone starts a timer on it; on Mondays (ET) it emails the full list. Nothing
to report = one log line, no email, exit 0. Crash = exit 1 (GitHub's workflow
failure notification covers watcher self-death).

The email is HTML in the Ontel report style (masthead, KPI cards, sections in
priority order: hides timer hours, needs a merge, clutter only) with a plain
text fallback and the full workbook attached (Summary, Groups, Copies, Blank
timers). The watcher never writes anything. Recipient: Jamil only (his call,
2026-10-19).

Usage:
    python duplicate_asset_watcher.py                       # check + email
    python duplicate_asset_watcher.py --dry-run             # check + print; no email
    python duplicate_asset_watcher.py --full                # full list regardless of the day
    python duplicate_asset_watcher.py --dry-run --full --xlsx-out ../out/x.xlsx --html-out ../out/x.html
"""

import argparse
import base64
import html
import io
import sys
from datetime import datetime, timedelta, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

from config import get_logger, get_db, close_db, retry_db, setup_logging

setup_logging()
logger = get_logger("duplicate_asset_watcher")

RECIPIENTS = ["jamil.mendez@ontel.co"]
NEW_HOURS = 26          # daily trigger + up to 2 h lag, with no gap between runs
ET = ZoneInfo("America/New_York")
PUSH_CHARS = "-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz"

_GROUPS_CTE = """
WITH g AS (
    SELECT project_did, asset_id, trim(asset_name) AS aname
    FROM data_staging.stg_assets
    WHERE asset_name IS NOT NULL AND asset_did IS NOT NULL
    GROUP BY 1, 2, 3
    HAVING count(DISTINCT asset_did) > 1
)"""

DUPLICATES_SQL = _GROUPS_CTE + """,
tc AS (   -- one pass over the timers: stg_timer_activities_inc has no asset_did index
    SELECT asset_did, count(*) AS timers
    FROM data_staging.stg_timer_activities_inc
    WHERE vanished_at IS NULL AND asset_did IS NOT NULL
    GROUP BY 1
)
SELECT p.project_name, s.project_did, s.asset_id, g.aname AS asset_name, s.asset_did,
       s.task_count, s.task_count - s.tasks_pending AS touched,
       COALESCE(tc.timers, 0) AS timers
FROM data_staging.stg_assets s
JOIN g ON g.project_did = s.project_did
      AND g.asset_id IS NOT DISTINCT FROM s.asset_id
      AND g.aname = trim(s.asset_name)
LEFT JOIN tc ON tc.asset_did = s.asset_did
LEFT JOIN data_staging.stg_projects p ON p.project_did = s.project_did
ORDER BY p.project_name, g.aname, s.asset_did
"""

# v1 timers the duplicate left without an asset (same project + path + name)
BLANK_TIMERS_SQL = _GROUPS_CTE + """
SELECT g.project_did, g.asset_id, g.aname AS asset_name, t.user_email,
       t.start_time, t.end_time, t.duration_min, t.task
FROM data_staging.stg_timer_activities t
JOIN g ON g.project_did = t.project_did
      AND g.asset_id IS NOT DISTINCT FROM t.site_id
      AND g.aname = trim(t.site_name)
WHERE t.asset_did IS NULL
ORDER BY g.project_did, g.aname, t.start_time
"""


def push_created_at(did):
    """Creation time encoded in the first 8 chars of a Firebase push id."""
    if not did or len(did) < 8 or any(c not in PUSH_CHARS for c in did[:8]):
        return None
    ms = 0
    for c in did[:8]:
        ms = ms * 64 + PUSH_CHARS.index(c)
    return datetime.fromtimestamp(ms / 1000, timezone.utc)


def _created_key(a):
    return push_created_at(a["asset_did"]) or datetime.max.replace(tzinfo=timezone.utc)


def _key(r):
    return (r["project_did"], r["asset_id"], r["asset_name"])


def group_rows(rows):
    groups = {}
    for r in rows:
        groups.setdefault(_key(r), {"project_name": r["project_name"], "asset_id": r["asset_id"],
                                    "asset_name": r["asset_name"], "assets": []})["assets"].append(dict(r))
    return list(groups.values())


def classify_group(assets):
    """assets: one group's rows. Returns the group dict with verdict/keep/remove."""
    assets = sorted(assets, key=_created_key)
    for a in assets:
        # unknown task state (NULL counts) counts as active: never flag it for deletion
        a["active"] = a["touched"] is None or a["touched"] > 0 or (a["timers"] or 0) > 0
        a["created_at"] = push_created_at(a["asset_did"])
    active = [a for a in assets if a["active"]]
    if len(active) > 1:
        verdict, keep = "merge_needed", None
    elif len(active) == 1:
        verdict, keep = "delete_empty", active[0]["asset_did"]
    else:
        verdict, keep = "all_empty", assets[0]["asset_did"]
    remove = [] if keep is None else [a["asset_did"] for a in assets if a["asset_did"] != keep]
    first = assets[0]
    return {"project_name": first["project_name"], "project_did": first["project_did"],
            "asset_id": first["asset_id"], "asset_name": first["asset_name"], "assets": assets,
            "verdict": verdict, "keep": keep, "remove": remove,
            "blank_timers": [], "blank_count": 0, "blank_minutes": 0.0, "blank_hours": 0.0, "last_blank": None}


def attach_blank_timers(groups, blank_rows):
    """Attach the v1 timers each group left without an asset; returns the groups."""
    by_key = {(g["project_did"], g["asset_id"], g["asset_name"]): g for g in groups}
    for r in blank_rows:
        g = by_key.get(_key(r))
        if g is not None:
            g["blank_timers"].append(dict(r))
    for g in groups:
        g["blank_count"] = len(g["blank_timers"])
        g["blank_minutes"] = sum(float(t["duration_min"] or 0) for t in g["blank_timers"])
        g["blank_hours"] = round(g["blank_minutes"] / 60.0, 1)
        g["last_blank"] = max((t["start_time"] for t in g["blank_timers"] if t["start_time"]), default=None)
    return groups


def section_of(g):
    """1 = hides timer hours (fix first), 2 = needs a merge, 3 = clutter only."""
    if g["blank_count"] > 0:
        return 1
    return 2 if g["verdict"] == "merge_needed" else 3


def sort_groups(groups):
    return sorted(groups, key=lambda g: (section_of(g), -g["blank_count"], -g["blank_hours"],
                                         g["project_name"] or "", g["asset_name"] or ""))


def select_groups(groups, now, full):
    if full:
        return list(groups)
    cutoff = now - timedelta(hours=NEW_HOURS)
    return [g for g in groups
            if any(a["created_at"] and a["created_at"] >= cutoff for a in g["assets"])]


def is_full_list_day(now):
    return now.astimezone(ET).weekday() == 0


def summarize(groups):
    return {"groups": len(groups),
            "affected": sum(1 for g in groups if g["blank_count"]),
            "timers": sum(g["blank_count"] for g in groups),
            "hours": round(sum(g["blank_minutes"] for g in groups) / 60.0, 1),   # sum raw minutes, round once
            "merge": sum(1 for g in groups if g["verdict"] == "merge_needed"),
            "copies_to_delete": sum(len(g["remove"]) for g in groups)}


def by_project(groups):
    out = {}
    for g in groups:
        p = out.setdefault(g["project_name"] or "(unknown project)",
                           {"groups": 0, "affected": 0, "timers": 0, "minutes": 0.0, "merge": 0, "delete": 0})
        p["groups"] += 1
        p["affected"] += 1 if g["blank_count"] else 0
        p["timers"] += g["blank_count"]
        p["minutes"] += g["blank_minutes"]
        p["merge"] += 1 if g["verdict"] == "merge_needed" else 0
        p["delete"] += len(g["remove"])
    for p in out.values():
        p["hours"] = round(p.pop("minutes") / 60.0, 1)
    return dict(sorted(out.items()))


VERDICT_TEXT = {
    "delete_empty": "Delete the empty copy",
    "all_empty": "Keep the oldest, delete the rest",
    "merge_needed": "MERGE first: more than one copy has work",
}

SECTION_TITLES = {
    1: ("Fix first: duplicates hiding timer hours",
        "These sites already have timers with no asset. Deleting the extra copy lets the next timer load fill the asset back in."),
    2: ("Needs a merge",
        "More than one copy has work on it. Move the work onto one copy in Swift, then delete the other."),
    3: ("Clutter only",
        "No timer is affected yet. Delete the extra copy before anyone starts a timer on it."),
}


def _fmt_et(dt, fmt="%b %d, %Y"):
    return dt.astimezone(ET).strftime(fmt) if dt else ""


def _tag(g, a):
    if g["verdict"] == "merge_needed":
        return "HAS WORK"
    return "KEEP" if a["asset_did"] == g["keep"] else "DELETE"


def _tasks_text(a):
    return (f"{a['touched']} of {a['task_count']} tasks touched" if a["touched"] is not None
            else "task state unknown")


# ---------------------------------------------------------------------------
# Plain text (fallback part of the email)
# ---------------------------------------------------------------------------

def build_email_body(groups, now, full, total):
    et = now.astimezone(ET)
    s = summarize(groups)
    head = "Full weekly list" if full else f"New in the last {NEW_HOURS} h"
    lines = [
        f"Duplicate Swift assets: {head}, {len(groups)} group(s) ({total} in Swift today). "
        f"Checked {et:%a %b %d, %Y %I:%M %p} ET.",
        f"{s['affected']} group(s) hide {s['timers']} timer(s), {s['hours']} h; {s['merge']} need a merge.",
        "Full detail in the attached workbook.",
        "",
    ]
    for sec in (1, 2, 3):
        rows = [g for g in groups if section_of(g) == sec]
        if not rows:
            continue
        lines += [f"== {SECTION_TITLES[sec][0]} ({len(rows)}) ==", ""]
        for g in rows:
            impact = (f", {g['blank_count']} blank timers, {g['blank_hours']} h" if g["blank_count"] else "")
            lines += [f"{g['project_name']} | {g['asset_name']}{impact}",
                      f"  Path: {g['asset_id']}",
                      f"  {VERDICT_TEXT[g['verdict']]}"]
            for a in g["assets"]:
                created = _fmt_et(a["created_at"], "%b %d %Y %I:%M:%S %p ET") or "unknown"
                lines.append(f"    {_tag(g, a):<8} {a['asset_did']}  created {created}, "
                             f"{_tasks_text(a)}, {a['timers']} timers")
            lines.append("")
    lines.append("Sent by duplicate_asset_watcher.py (local-pipeline). It never changes Swift or the database.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML email (Ontel report style, inline styles for Gmail)
# ---------------------------------------------------------------------------

INK, MUTED, LINE, PAPER = "#0f172a", "#64748b", "#e2e8f0", "#f8fafc"
CYAN, CYAN_DK, NAVY = "#06b6d4", "#0891b2", "#1e2740"
OK, WARN, BAD = "#0a7a52", "#8a5a00", "#b23b30"
SANS = "'Plus Jakarta Sans',Arial,Helvetica,sans-serif"
MONO = "'Spline Sans Mono',Consolas,'Courier New',monospace"
TAG_COLOR = {"KEEP": OK, "DELETE": BAD, "HAS WORK": WARN}


def _e(v):
    return html.escape("" if v is None else str(v))


def _kpi(label, value, color=INK):
    return (f'<td style="padding:4px;width:20%;vertical-align:top">'
            f'<div style="background:#fff;border:1px solid {LINE};border-radius:10px;padding:10px 10px">'
            f'<div style="font:600 10px {SANS};letter-spacing:.08em;text-transform:uppercase;color:{MUTED}">{_e(label)}</div>'
            f'<div style="font:700 24px {MONO};color:{color};margin-top:4px">{_e(value)}</div></div></td>')


def _th(label, align="left"):
    return (f'<th style="text-align:{align};padding:8px 10px;font:600 10px {MONO};letter-spacing:.06em;'
            f'text-transform:uppercase;color:{MUTED};border-bottom:2px solid {LINE};background:{PAPER}">{_e(label)}</th>')


def _td(v, align="left", mono=False, color=INK, bold=False, bg="#fff"):
    font = MONO if mono else SANS
    weight = 700 if bold else 400
    return (f'<td style="text-align:{align};padding:8px 10px;font:{weight} 12px {font};color:{color};'
            f'border-bottom:1px solid {LINE};vertical-align:top;background:{bg}">{v}</td>')


def _short_project(name):
    return (name or "").replace("TECH-OPS: ", "")


def _copies_html(g):
    parts = []
    for a in g["assets"]:
        tag = _tag(g, a)
        parts.append(
            f'<div style="margin:3px 0 6px"><span style="display:inline-block;width:58px;'
            f'font:700 9px {MONO};color:#fff;background:{TAG_COLOR[tag]};border-radius:4px;padding:2px 0;'
            f'text-align:center">{tag}</span> <span style="font:400 11px {MONO};color:{INK}">{_e(a["asset_did"])}</span>'
            f'<div style="font:400 11px {SANS};color:{MUTED};margin:2px 0 0 62px">created {_e(_fmt_et(a["created_at"]))} &middot; '
            f'{_e(_tasks_text(a))} &middot; {a["timers"]} timers</div></div>')
    return "".join(parts)


def build_email_html(groups, now, full, total):
    et = now.astimezone(ET)
    s = summarize(groups)
    mode = "Weekly full list" if full else f"New in the last {NEW_HOURS} hours"
    out = [f'<div style="background:{PAPER};padding:20px 0;font-family:{SANS};color:{INK}">'
           f'<table role="presentation" cellpadding="0" cellspacing="0" align="center" '
           f'style="width:100%;max-width:880px;border-collapse:collapse">']
    # masthead
    out.append(
        f'<tr><td style="background:{NAVY};background-image:linear-gradient(135deg,#1e2740,#182032);'
        f'border-radius:14px 14px 0 0;padding:22px 26px">'
        f'<div style="font:700 11px {MONO};letter-spacing:.14em;color:{CYAN}">ONTEL &middot; PIPELINE ALERTS</div>'
        f'<div style="font:700 24px {SANS};color:#fff;margin-top:6px">Duplicate Swift assets</div>'
        f'<div style="font:400 13px {SANS};color:#cbd5e1;margin-top:4px">{_e(mode)} &middot; checked '
        f'{_e(et.strftime("%a %b %d, %Y %I:%M %p"))} ET &middot; {total} group(s) in Swift today</div>'
        f'</td></tr>')
    # KPIs
    out.append(f'<tr><td style="background:#fff;padding:14px 20px 4px">'
               f'<table role="presentation" style="width:100%;border-collapse:collapse"><tr>'
               + _kpi("Duplicate groups", s["groups"])
               + _kpi("Hiding timers", s["affected"], BAD if s["affected"] else OK)
               + _kpi("Blank timers", s["timers"], BAD if s["timers"] else OK)
               + _kpi("Blank hours", f'{s["hours"]:.1f}', BAD if s["timers"] else OK)
               + _kpi("Need a merge", s["merge"], WARN if s["merge"] else OK)
               + '</tr></table></td></tr>')
    # bottom line
    out.append(
        f'<tr><td style="background:#fff;padding:6px 26px 16px"><div style="border-left:4px solid {CYAN};'
        f'background:#ecfeff;padding:12px 14px;border-radius:6px;font:400 13px {SANS};color:{INK}">'
        f'<b>What to do:</b> in Swift, delete each copy marked <b style="color:{BAD}">DELETE</b>. '
        f'The next timer load then fills the asset back in. Start with section 01. '
        f'Sites marked <b style="color:{WARN}">HAS WORK</b> need their work moved onto one copy first. '
        f'Every timer, copy and id is in the attached workbook.</div></td></tr>')
    # sections
    for n, sec in enumerate((1, 2, 3), start=1):
        rows = [g for g in groups if section_of(g) == sec]
        if not rows:
            continue
        title, note = SECTION_TITLES[sec]
        out.append(
            f'<tr><td style="background:#fff;padding:10px 26px 4px">'
            f'<div style="font:700 11px {MONO};letter-spacing:.12em;color:{CYAN_DK}">0{n} &middot; {len(rows)} SITE(S)</div>'
            f'<div style="font:700 17px {SANS};color:{INK};margin-top:2px">{_e(title)}</div>'
            f'<div style="font:400 12px {SANS};color:{MUTED};margin-top:2px">{_e(note)}</div></td></tr>')
        out.append(f'<tr><td style="background:#fff;padding:6px 20px 14px">'
                   f'<table role="presentation" style="width:100%;border-collapse:collapse"><tr>'
                   + _th("Proj") + _th("Site") + _th("Timers", "right") + _th("Hours", "right")
                   + _th("Last timer") + _th("Copies") + '</tr>')
        for i, g in enumerate(rows):
            bg = "#fff" if i % 2 == 0 else PAPER
            site = (f'<div style="font-weight:700">{_e(g["asset_name"])}</div>'
                    f'<div style="font:400 10px {MONO};color:{MUTED};margin-top:2px;word-break:break-all">{_e(g["asset_id"])}</div>'
                    f'<div style="font:600 11px {SANS};color:{CYAN_DK};margin-top:3px">{_e(VERDICT_TEXT[g["verdict"]])}</div>')
            out.append('<tr>'
                       + _td(f'<span style="white-space:nowrap">{_e(_short_project(g["project_name"]))}</span>', mono=True, bold=True, bg=bg)
                       + _td(site, bg=bg)
                       + _td(g["blank_count"] or "-", "right", True, BAD if g["blank_count"] else MUTED, bool(g["blank_count"]), bg)
                       + _td(f'{g["blank_hours"]:.1f}' if g["blank_count"] else "-", "right", True,
                             BAD if g["blank_count"] else MUTED, False, bg)
                       + _td(f'<span style="white-space:nowrap">{_e(_fmt_et(g["last_blank"], "%Y-%m-%d")) or "-"}</span>', mono=True, color=MUTED, bg=bg)
                       + _td(_copies_html(g), bg=bg)
                       + '</tr>')
        out.append('</table></td></tr>')
    # footer
    out.append(
        f'<tr><td style="background:#fff;border-top:1px solid {LINE};border-radius:0 0 14px 14px;padding:14px 26px;'
        f'font:400 11px {SANS};color:{MUTED}">A duplicate is one site created more than once in the same project with the '
        f'same path and name. The old timer and QA form loads match assets by name and path, so they cannot tell the '
        f'copies apart and leave the asset empty. Blank timers = old-load timers on that site with no asset. '
        f'Sent by duplicate_asset_watcher.py (local-pipeline); it never changes Swift or the database.</td></tr>')
    out.append('</table></div>')
    return "".join(out)


# ---------------------------------------------------------------------------
# Workbook
# ---------------------------------------------------------------------------

def build_workbook(groups, now, full, total):
    """Snapshot workbook: Summary, Groups, Copies, Blank timers. Values, not
    formulas: it is generated in a headless job with no recalculation step,
    and Gmail's preview shows formula cells empty."""
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    F = "Arial"
    head_font = Font(name=F, bold=True, color="FFFFFF", size=10)
    head_fill = PatternFill("solid", fgColor="1E2740")
    body = Font(name=F, size=10)
    bold = Font(name=F, size=10, bold=True)
    red = Font(name=F, size=10, bold=True, color="B23B30")
    zebra = PatternFill("solid", fgColor="F8FAFC")
    tag_font = {"KEEP": Font(name=F, size=10, bold=True, color="0A7A52"),
                "DELETE": Font(name=F, size=10, bold=True, color="B23B30"),
                "HAS WORK": Font(name=F, size=10, bold=True, color="8A5A00")}
    et = now.astimezone(ET)
    s = summarize(groups)

    def table(ws, start_row, headers, rows, widths, num_formats=None):
        for c, h in enumerate(headers, 1):
            cell = ws.cell(start_row, c, h)
            cell.font, cell.fill = head_font, head_fill
            cell.alignment = Alignment(vertical="center", wrap_text=True)
        for r, row in enumerate(rows, start_row + 1):
            for c, v in enumerate(row, 1):
                cell = ws.cell(r, c, v)
                cell.font = body
                if (r - start_row) % 2 == 0:
                    cell.fill = zebra
                if num_formats and num_formats.get(c):
                    cell.number_format = num_formats[c]
        for c, w in enumerate(widths, 1):
            ws.column_dimensions[get_column_letter(c)].width = w
        if rows:
            ws.auto_filter.ref = f"A{start_row}:{get_column_letter(len(headers))}{start_row + len(rows)}"
        ws.freeze_panes = ws.cell(start_row + 1, 1)

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Duplicate Swift assets"
    ws["A1"].font = Font(name=F, size=16, bold=True, color="0F172A")
    ws["A2"] = (f"{'Weekly full list' if full else f'New in the last {NEW_HOURS} hours'} · checked "
                f"{et:%a %b %d, %Y %I:%M %p} ET · {total} group(s) in Swift today")
    ws["A2"].font = Font(name=F, size=10, color="64748B")
    kpis = [("Duplicate groups", s["groups"]), ("Groups hiding timers", s["affected"]),
            ("Blank timers", s["timers"]), ("Blank hours", s["hours"]),
            ("Need a merge", s["merge"]), ("Copies to delete", s["copies_to_delete"])]
    for i, (k, v) in enumerate(kpis, 4):
        ws.cell(i, 1, k).font = bold
        ws.cell(i, 2, v).font = red if k.startswith("Blank") and v else body
        if k == "Blank hours":
            ws.cell(i, 2).number_format = "0.0"
    proj = by_project(groups)
    start = 4 + len(kpis) + 2
    ws.cell(start - 1, 1, "By project").font = Font(name=F, size=12, bold=True)
    table(ws, start, ["Project", "Duplicate groups", "Groups hiding timers", "Blank timers", "Blank hours",
                      "Need a merge", "Copies to delete"],
          [[p, v["groups"], v["affected"], v["timers"], v["hours"], v["merge"], v["delete"]]
           for p, v in proj.items()],
          [26, 16, 20, 14, 13, 14, 16], {5: "0.0"})
    ws.freeze_panes = None
    note = start + len(proj) + 2
    ws.cell(note, 1, "How to read this").font = bold
    for i, line in enumerate([
        "A duplicate = one site created more than once in the same Swift project with the same path and name.",
        "Blank timers = old-load timers (stg_timer_activities) on that site whose asset is empty because of the duplicate.",
        "Action: delete each copy marked DELETE in Swift; the next timer load fills the asset back in.",
        "HAS WORK = more than one copy has tasks or timers; move the work onto one copy before deleting.",
        "Snapshot values written by duplicate_asset_watcher.py (local-pipeline); it never changes Swift or the database.",
    ], note + 1):
        ws.cell(i, 1, line).font = Font(name=F, size=10, color="64748B")

    sec_name = {1: "1 Fix first (hides timers)", 2: "2 Needs a merge", 3: "3 Clutter only"}
    ws = wb.create_sheet("Groups")
    table(ws, 1, ["Priority", "Project", "Site name", "Path", "Action", "Blank timers", "Blank hours",
                  "Last blank timer (ET)", "Keep asset id", "Delete asset id(s)", "Copies"],
          [[sec_name[section_of(g)], g["project_name"], g["asset_name"], g["asset_id"],
            VERDICT_TEXT[g["verdict"]], g["blank_count"], g["blank_hours"],
            g["last_blank"].astimezone(ET).replace(tzinfo=None) if g["last_blank"] else None,
            g["keep"] or "", ", ".join(g["remove"]), len(g["assets"])] for g in groups],
          [24, 16, 40, 56, 34, 13, 12, 20, 24, 48, 8], {7: "0.0", 8: "yyyy-mm-dd hh:mm"})
    for r in range(2, len(groups) + 2):
        if ws.cell(r, 6).value:
            ws.cell(r, 6).font = red
            ws.cell(r, 7).font = red

    ws = wb.create_sheet("Copies")
    rows = []
    for g in groups:
        for a in g["assets"]:
            rows.append([g["project_name"], g["asset_name"], _tag(g, a), a["asset_did"],
                         a["created_at"].astimezone(ET).replace(tzinfo=None) if a["created_at"] else None,
                         a["touched"], a["task_count"], a["timers"], g["asset_id"]])
    table(ws, 1, ["Project", "Site name", "Tag", "Asset id", "Created (ET)", "Tasks touched", "Task count",
                  "Timers (new pipeline)", "Path"],
          rows, [16, 40, 11, 24, 20, 13, 11, 20, 56], {5: "yyyy-mm-dd hh:mm:ss"})
    for r in range(2, len(rows) + 2):
        ws.cell(r, 3).font = tag_font[ws.cell(r, 3).value]

    ws = wb.create_sheet("Blank timers")
    rows = []
    for g in groups:
        for t in g["blank_timers"]:
            rows.append([g["project_name"], g["asset_name"], t["user_email"],
                         t["start_time"].astimezone(ET).replace(tzinfo=None) if t["start_time"] else None,
                         t["end_time"].astimezone(ET).replace(tzinfo=None) if t["end_time"] else None,
                         round(float(t["duration_min"] or 0) / 60.0, 2), t["task"], g["asset_id"]])
    table(ws, 1, ["Project", "Site name", "Member", "Start (ET)", "End (ET)", "Hours", "Task", "Path"],
          rows, [16, 40, 30, 18, 18, 8, 40, 56],
          {4: "yyyy-mm-dd hh:mm", 5: "yyyy-mm-dd hh:mm", 6: "0.00"})

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Send
# ---------------------------------------------------------------------------

def build_message(subject, text, html_body, xlsx_bytes, xlsx_name):
    msg = MIMEMultipart("mixed")
    msg["To"] = ", ".join(RECIPIENTS)
    msg["Subject"] = subject
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(text, "plain", "utf-8"))
    alt.attach(MIMEText(html_body, "html", "utf-8"))
    msg.attach(alt)
    part = MIMEApplication(xlsx_bytes, _subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    part.add_header("Content-Disposition", "attachment", filename=xlsx_name)
    msg.attach(part)
    return msg


def send_email(msg):
    import gmail_client
    from gmail_client import authenticate, masked_sender
    if not gmail_client.TOKEN_FILE.exists():
        raise RuntimeError(f"gmail token missing ({gmail_client.TOKEN_FILE}); "
                           f"refusing interactive OAuth in a headless job")
    service = authenticate()
    msg["From"] = masked_sender(service, "Pipeline Alerts")
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute(num_retries=3)
    logger.info(f"Email sent to {', '.join(RECIPIENTS)}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="print; no email")
    parser.add_argument("--full", action="store_true", help="full list regardless of the day")
    parser.add_argument("--xlsx-out", help="also write the workbook to this path")
    parser.add_argument("--html-out", help="also write the HTML email body to this path")
    args = parser.parse_args()

    now = datetime.now(timezone.utc)
    full = args.full or is_full_list_day(now)
    db = None
    try:
        db = get_db()
        rows = retry_db(lambda: db.fetch(DUPLICATES_SQL), description="read duplicate assets")
        blanks = retry_db(lambda: db.fetch(BLANK_TIMERS_SQL), description="read blank v1 timers")
        groups = attach_blank_timers([classify_group(g["assets"]) for g in group_rows(rows)], blanks)
        picked = sort_groups(select_groups(groups, now, full))
        s_all = summarize(groups)
        logger.info(f"{len(groups)} duplicate group(s) in Swift; {s_all['affected']} hide "
                    f"{s_all['timers']} timer(s) / {s_all['hours']} h; {s_all['merge']} need a merge; "
                    f"{len(picked)} to report ({'full list' if full else f'new in {NEW_HOURS} h'})")
        if not picked:
            logger.info("Nothing to report; no email.")
            return 0
        s = summarize(picked)
        text = build_email_body(picked, now, full, len(groups))
        html_body = build_email_html(picked, now, full, len(groups))
        xlsx = build_workbook(picked, now, full, len(groups))
        xlsx_name = f"duplicate-swift-assets-{now.astimezone(ET):%Y-%m-%d}.xlsx"
        if args.xlsx_out:
            with open(args.xlsx_out, "wb") as f:
                f.write(xlsx)
            logger.info(f"Workbook written to {args.xlsx_out}")
        if args.html_out:
            with open(args.html_out, "w", encoding="utf-8") as f:
                f.write("<!doctype html><meta charset='utf-8'>" + html_body)
            logger.info(f"HTML written to {args.html_out}")
        subject = (f"Duplicate Swift assets: {len(picked)} "
                   f"{'group(s), weekly list' if full else 'new group(s)'}"
                   + (f", {s['timers']} blank timers ({s['hours']} h)" if s["timers"] else "")
                   + (f", {s['merge']} need a merge" if s["merge"] else ""))
        if args.dry_run:
            logger.info(f"Dry run: email suppressed.\nSubject: {subject}\n{text}")
        else:
            send_email(build_message(subject, text, html_body, xlsx, xlsx_name))
        return 0
    except Exception:
        logger.exception("duplicate asset watcher crashed")
        return 1
    finally:
        if db is not None:
            close_db()


if __name__ == "__main__":
    sys.exit(main())
