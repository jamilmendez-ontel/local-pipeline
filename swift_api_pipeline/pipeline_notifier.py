"""
Pipeline email notifications via Gmail API.

Captures pipeline logs and sends HTML summary emails with log attachments
after each pipeline run. Email failures never crash the pipeline.
"""

import logging
import threading
import traceback
import base64
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Sequence

from config import get_logger

logger = get_logger("notifier")

NOTIFICATION_RECIPIENTS = [
    "jamil.mendez@ontel.co",
    "hajie@ontel.co",
    "sheena@ontel.co",
]
TZ_EASTERN = ZoneInfo("America/New_York")

# Per-pipeline table lists for row count display in emails.
# Each pipeline's email only shows its own relevant tables.
PIPELINE_TABLES = {
    "Organizations & Projects": [
        ("data_raw", "raw_organizations"),
        ("data_raw", "raw_projects"),
        ("data_staging", "stg_organizations"),
        ("data_staging", "stg_projects"),
    ],
    "Asset Tasks": [
        ("data_raw", "raw_asset_tasks"),
        ("data_staging", "stg_assets"),
        ("data_staging", "stg_asset_tasks"),
    ],
    "Asset Tasks Extract": [
        ("data_raw", "raw_asset_tasks"),
    ],
    "Asset Tasks Transform": [
        ("data_staging", "stg_assets"),
        ("data_staging", "stg_asset_tasks"),
    ],
    "Asset Tasks GC": [
        ("data_raw", "raw_asset_tasks_gc"),
        ("data_staging", "stg_assets_gc"),
        ("data_staging", "stg_asset_tasks_gc"),
    ],
    "Asset Tasks GC Extract": [
        ("data_raw", "raw_asset_tasks_gc"),
    ],
    "Asset Tasks GC Transform": [
        ("data_staging", "stg_assets_gc"),
        ("data_staging", "stg_asset_tasks_gc"),
    ],
    "Analytics GC MV Refresh": [],
    "Assets Status": [
        ("data_raw", "raw_assets"),
        ("data_staging", "stg_assets"),
    ],
    "User Priorities": [
        ("data_raw", "raw_user_priorities"),
        ("data_staging", "stg_user_priorities"),
    ],
    "QA Forms": [
        ("data_staging", "stg_qa_form"),
    ],
    "Invoicing Form": [
        ("data_raw", "raw_invoicing_form"),
        ("data_staging", "stg_invoicing_form"),
    ],
    "Timer Activities": [
        ("data_raw", "raw_timer_activities"),
        ("data_staging", "stg_timer_activities"),
    ],
    "AR Aging": [
        ("data_raw", "raw_ar_aging"),
        ("data_staging", "stg_ar_aging"),
    ],
    "Sales Detail": [
        ("data_raw", "raw_sales_detail"),
        ("data_staging", "stg_sales_detail"),
    ],
    "Asset DID Backfill": [
        ("data_staging", "stg_timer_activities"),
        ("data_staging", "stg_qa_form"),
    ],
    "Analytics MV Refresh": [],
    "Calendar Leave": [
        ("data_raw", "raw_calendar_leave"),
        ("data_staging", "stg_calendar_leave"),
    ],
    "Daily Reports": [
        ("data_raw", "raw_daily_reports"),
        ("data_staging", "stg_daily_reports"),
        ("data_staging", "stg_daily_report_hours"),
        ("data_staging", "stg_daily_report_attendance"),
    ],
}

# All unique tables across all pipelines (for --extract / --transform modes)
ALL_TABLES = list(dict.fromkeys(
    t for tables in PIPELINE_TABLES.values() for t in tables
))


@dataclass
class PipelineResult:
    """Result of a single pipeline execution."""
    pipeline_name: str
    status: str  # "SUCCESS" or "FAILED"
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    error_message: Optional[str] = None
    details: dict = field(default_factory=dict)


@dataclass
class PipelineOutcome:
    """Returned by a pipeline function to report a non-clean result WITHOUT raising.

    Lets run_pipeline_with_notification choose SUCCESS vs a red degraded email
    while still letting the successfully-processed work flow downstream.
    """
    run_id: Optional[str] = None
    failed_projects: list = field(default_factory=list)
    abnormal_projects: list = field(default_factory=list)
    detail: str = ""

    def is_clean(self) -> bool:
        return not self.failed_projects and not self.abnormal_projects

    def email_status(self) -> str:
        if self.failed_projects:
            return "PARTIAL FAILURE"
        if self.abnormal_projects:
            return "ABNORMAL ROW COUNT"
        return "SUCCESS"


class LogCaptureHandler(logging.Handler):
    """Logging handler that captures log lines in memory.

    When *logger_prefixes* is provided, only captures logs that either:
    - Come from the owner thread (covers shared loggers like base, retry, db)
    - Have a logger name starting with one of the prefixes (covers child threads,
      e.g. asset_tasks' 6 extraction workers logging to pipeline.asset_tasks)

    This prevents cross-contamination when multiple pipelines run in parallel.
    """

    def __init__(
        self,
        maxlen: int = 10000,
        owner_thread: Optional[int] = None,
        logger_prefixes: Optional[Sequence[str]] = None,
    ):
        super().__init__()
        self.records: deque = deque(maxlen=maxlen)
        self._owner_thread = owner_thread
        self._logger_prefixes = tuple(logger_prefixes) if logger_prefixes else None
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S"
        ))

    def emit(self, record):
        if self._logger_prefixes is not None:
            from_owner = record.thread == self._owner_thread
            name_match = record.name.startswith(self._logger_prefixes)
            if not from_owner and not name_match:
                return
        self.records.append(self.format(record))

    def get_log_output(self) -> str:
        return "\n".join(self.records)


@contextmanager
def capture_logs(logger_prefixes: Optional[Sequence[str]] = None):
    """Context manager that captures pipeline logs alongside normal output.

    Args:
        logger_prefixes: When provided, enables thread-aware filtering so only
            logs from the calling thread (shared loggers) or matching the given
            name prefixes (child worker threads) are captured.  Pass None to
            capture everything (for sequential/single-pipeline runs).
    """
    owner_thread = threading.get_ident() if logger_prefixes else None
    handler = LogCaptureHandler(
        owner_thread=owner_thread,
        logger_prefixes=logger_prefixes,
    )
    root_logger = logging.getLogger("pipeline")
    root_logger.addHandler(handler)
    try:
        yield handler
    finally:
        root_logger.removeHandler(handler)


def snapshot_row_counts(tables: Optional[List] = None) -> Dict[str, int]:
    """Take a snapshot of table row counts for email comparison.

    Retries once with a fresh DB pool if the connection is stale (common after
    long-running pipelines where the pool's connections get closed server-side).

    Args:
        tables: List of (schema, table) tuples to count. If None, returns empty.
    """
    if not tables:
        return {}
    from db import get_db, close_db
    for attempt in range(2):
        try:
            if attempt > 0:
                close_db()
            db = get_db()
            counts = {}
            for schema, table in tables:
                count = db.fetchval(f'SELECT COUNT(*) FROM {schema}.{table}')
                counts[f"{schema}.{table}"] = count if count is not None else 0
            return counts
        except Exception as e:
            if attempt == 0:
                logger.warning(f"Snapshot row counts failed, retrying with fresh connection: {e}")
            else:
                logger.warning(f"Failed to snapshot row counts after retry: {e}")
    return {}


def _format_duration(seconds: float) -> str:
    """Format seconds into a human-readable duration string."""
    total = int(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h {minutes}m {secs}s"
    elif minutes > 0:
        return f"{minutes}m {secs}s"
    else:
        return f"{secs}s"


def _build_row_counts_html(
    before: Dict[str, int],
    after: Dict[str, int],
    tables: Optional[List] = None,
) -> str:
    """Build HTML table showing before/after row counts.

    Args:
        tables: List of (schema, table) tuples to display. If None, derives
                from the before/after keys.
    """
    if not before and not after:
        return ""

    # Determine which tables to show
    if tables:
        display_tables = tables
    else:
        # Fall back to keys present in before/after
        all_keys = set(before.keys()) | set(after.keys())
        display_tables = []
        for key in sorted(all_keys):
            parts = key.split(".", 1)
            if len(parts) == 2:
                display_tables.append((parts[0], parts[1]))

    if not display_tables:
        return ""

    rows_html = ""
    prev_schema = None
    for schema, table in display_tables:
        # Add section header when schema changes
        if schema != prev_schema:
            label = "Raw" if schema == "data_raw" else "Staging"
            rows_html += f"""
        <tr>
            <td colspan="4" style="padding:8px 12px;border:1px solid #ddd;background-color:#e8eaf6;font-weight:bold;">{label}</td>
        </tr>"""
            prev_schema = schema

        full_name = f"{schema}.{table}"
        prev = before.get(full_name, 0)
        curr = after.get(full_name, 0)
        diff = curr - prev

        if diff > 0:
            diff_str = f'<span style="color:#2e7d32;">+{diff:,}</span>'
        elif diff < 0:
            diff_str = f'<span style="color:#c62828;">{diff:,}</span>'
        else:
            diff_str = '<span style="color:#888;">0</span>'

        rows_html += f"""
        <tr>
            <td style="padding:6px 12px;border:1px solid #ddd;">{table}</td>
            <td style="padding:6px 12px;border:1px solid #ddd;text-align:right;">{prev:,}</td>
            <td style="padding:6px 12px;border:1px solid #ddd;text-align:right;">{curr:,}</td>
            <td style="padding:6px 12px;border:1px solid #ddd;text-align:right;">{diff_str}</td>
        </tr>"""

    return f"""
        <h3 style="margin-top:24px;margin-bottom:8px;">Row Counts</h3>
        <table style="border-collapse:collapse;">
            <thead>
                <tr style="background-color:#f5f5f5;">
                    <th style="padding:6px 12px;border:1px solid #ddd;text-align:left;">Table</th>
                    <th style="padding:6px 12px;border:1px solid #ddd;text-align:right;">Before</th>
                    <th style="padding:6px 12px;border:1px solid #ddd;text-align:right;">After</th>
                    <th style="padding:6px 12px;border:1px solid #ddd;text-align:right;">Change</th>
                </tr>
            </thead>
            <tbody>
                {rows_html}
            </tbody>
        </table>"""


def _build_html_email(
    results: List[PipelineResult],
    overall_status: str,
    run_label: str,
    started_at: datetime,
    ended_at: datetime,
    total_duration: float,
    row_counts_before: Optional[Dict[str, int]] = None,
    row_counts_after: Optional[Dict[str, int]] = None,
    row_count_tables: Optional[List] = None,
) -> str:
    """Build HTML email body with inline CSS."""
    color = "#2e7d32" if overall_status == "SUCCESS" else "#c62828"

    # Convert timestamps to Eastern time for display
    started_et = started_at.astimezone(TZ_EASTERN)
    ended_et = ended_at.astimezone(TZ_EASTERN)

    # Build per-pipeline rows
    rows_html = ""
    for r in results:
        detail_parts = []
        for k, v in r.details.items():
            val = f"{v:,}" if isinstance(v, int) else str(v)
            detail_parts.append(f"{k}: {val}")
        details_str = ", ".join(detail_parts) if detail_parts else "-"
        error_str = r.error_message or "-"
        status_color = "#2e7d32" if r.status == "SUCCESS" else "#c62828"
        rows_html += f"""
        <tr>
            <td style="padding:8px;border:1px solid #ddd;">{r.pipeline_name}</td>
            <td style="padding:8px;border:1px solid #ddd;color:{status_color};font-weight:bold;">{r.status}</td>
            <td style="padding:8px;border:1px solid #ddd;">{_format_duration(r.duration_seconds)}</td>
            <td style="padding:8px;border:1px solid #ddd;">{details_str}</td>
            <td style="padding:8px;border:1px solid #ddd;color:#c62828;">{error_str}</td>
        </tr>"""

    # Build row counts section
    row_counts_html = _build_row_counts_html(
        row_counts_before or {}, row_counts_after or {},
        tables=row_count_tables,
    )

    html = f"""
    <html>
    <body style="font-family:Arial,sans-serif;margin:0;padding:0;">
        <div style="background-color:{color};color:white;padding:16px 24px;">
            <h2 style="margin:0;">{run_label}: {overall_status}</h2>
        </div>
        <div style="padding:24px;">
            <table style="margin-bottom:24px;">
                <tr><td style="padding:4px 16px 4px 0;font-weight:bold;">Started:</td><td>{started_et:%Y-%m-%d %H:%M:%S %Z}</td></tr>
                <tr><td style="padding:4px 16px 4px 0;font-weight:bold;">Ended:</td><td>{ended_et:%Y-%m-%d %H:%M:%S %Z}</td></tr>
                <tr><td style="padding:4px 16px 4px 0;font-weight:bold;">Duration:</td><td>{_format_duration(total_duration)}</td></tr>
            </table>

            <h3 style="margin-bottom:8px;">Pipeline Details</h3>
            <table style="border-collapse:collapse;width:100%;">
                <thead>
                    <tr style="background-color:#f5f5f5;">
                        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Pipeline</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Status</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Duration</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Details</th>
                        <th style="padding:8px;border:1px solid #ddd;text-align:left;">Error</th>
                    </tr>
                </thead>
                <tbody>
                    {rows_html}
                </tbody>
            </table>

            {row_counts_html}
        </div>
    </body>
    </html>
    """
    return html


def send_pipeline_email(
    results: List[PipelineResult],
    log_output: str,
    overall_status: str,
    run_label: str,
    started_at: datetime,
    ended_at: datetime,
    total_duration: float,
    recipients: List[str] = None,
    row_counts_before: Optional[Dict[str, int]] = None,
    row_counts_after: Optional[Dict[str, int]] = None,
    row_count_tables: Optional[List] = None,
):
    """
    Send pipeline summary email with log attachment via Gmail API.

    Wrapped in try/except — email failures are logged but never crash the pipeline.
    """
    try:
        from gmail_client import authenticate

        if recipients is None:
            recipients = NOTIFICATION_RECIPIENTS

        service = authenticate()

        # Build the email
        duration_str = _format_duration(total_duration)
        subject = f"Pipeline {overall_status}: {run_label} ({duration_str})"

        msg = MIMEMultipart()
        msg["To"] = ", ".join(recipients)
        msg["From"] = "me"
        msg["Subject"] = subject

        # HTML body
        html_body = _build_html_email(
            results, overall_status, run_label,
            started_at, ended_at, total_duration,
            row_counts_before, row_counts_after,
            row_count_tables=row_count_tables,
        )
        msg.attach(MIMEText(html_body, "html"))

        # Log attachment — filename in Eastern Time
        if log_output:
            started_et = started_at.astimezone(TZ_EASTERN)
            log_filename = f"pipeline_log_{started_et:%Y%m%d_%H%M%S}.txt"
            log_attachment = MIMEText(log_output, "plain")
            log_attachment.add_header(
                "Content-Disposition", "attachment", filename=log_filename
            )
            msg.attach(log_attachment)

        # Send via Gmail API
        raw = base64.urlsafe_b64encode(msg.as_string().encode()).decode()
        service.users().messages().send(
            userId="me",
            body={"raw": raw},
        ).execute()

        logger.info(f"Notification email sent to {', '.join(recipients)}: {subject}")

    except Exception as e:
        logger.error(f"Failed to send notification email: {e}\n{traceback.format_exc()}")
