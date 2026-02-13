"""
Pipeline email notifications via Gmail API.

Captures pipeline logs and sends HTML summary emails with log attachments
after each pipeline run. Email failures never crash the pipeline.
"""

import logging
import traceback
import base64
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional

from config import get_logger

logger = get_logger("notifier")

NOTIFICATION_RECIPIENT = "jamil.mendez@ontel.co"
TZ_EASTERN = ZoneInfo("America/New_York")

# Staging tables to track in notification emails
ROW_COUNT_TABLES = [
    ("data_staging", "stg_organizations"),
    ("data_staging", "stg_projects"),
    ("data_staging", "stg_asset_tasks"),
    ("data_staging", "stg_assets"),
    ("data_staging", "stg_qa_form"),
    ("data_staging", "stg_timer_activities"),
    ("data_staging", "stg_user_priorities"),
    ("data_staging", "stg_ar_aging"),
    ("data_staging", "stg_sales_detail"),
]


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


class LogCaptureHandler(logging.Handler):
    """Logging handler that captures log lines in memory."""

    def __init__(self, maxlen: int = 10000):
        super().__init__()
        self.records: deque = deque(maxlen=maxlen)
        self.setFormatter(logging.Formatter(
            "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
            datefmt="%H:%M:%S"
        ))

    def emit(self, record):
        self.records.append(self.format(record))

    def get_log_output(self) -> str:
        return "\n".join(self.records)


@contextmanager
def capture_logs():
    """Context manager that captures pipeline logs alongside normal output."""
    handler = LogCaptureHandler()
    root_logger = logging.getLogger("pipeline")
    root_logger.addHandler(handler)
    try:
        yield handler
    finally:
        root_logger.removeHandler(handler)


def snapshot_row_counts() -> Dict[str, int]:
    """Take a snapshot of staging table row counts for email comparison."""
    try:
        from config import get_db
        db = get_db()
        counts = {}
        for schema, table in ROW_COUNT_TABLES:
            count = db.fetchval(f'SELECT COUNT(*) FROM {schema}.{table}')
            counts[f"{schema}.{table}"] = count if count is not None else 0
        return counts
    except Exception as e:
        logger.warning(f"Failed to snapshot row counts: {e}")
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
) -> str:
    """Build HTML table showing before/after row counts."""
    if not before and not after:
        return ""

    rows_html = ""
    for key in ROW_COUNT_TABLES:
        full_name = f"{key[0]}.{key[1]}"
        table_label = key[1]
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
            <td style="padding:6px 12px;border:1px solid #ddd;">{table_label}</td>
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
        row_counts_before or {}, row_counts_after or {}
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
    recipient: str = NOTIFICATION_RECIPIENT,
    row_counts_before: Optional[Dict[str, int]] = None,
    row_counts_after: Optional[Dict[str, int]] = None,
):
    """
    Send pipeline summary email with log attachment via Gmail API.

    Wrapped in try/except — email failures are logged but never crash the pipeline.
    """
    try:
        from gmail_client import authenticate

        service = authenticate()

        # Build the email
        duration_str = _format_duration(total_duration)
        subject = f"Pipeline {overall_status}: {run_label} ({duration_str})"

        msg = MIMEMultipart()
        msg["To"] = recipient
        msg["From"] = "me"
        msg["Subject"] = subject

        # HTML body
        html_body = _build_html_email(
            results, overall_status, run_label,
            started_at, ended_at, total_duration,
            row_counts_before, row_counts_after,
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

        logger.info(f"Notification email sent to {recipient}: {subject}")

    except Exception as e:
        logger.error(f"Failed to send notification email: {e}\n{traceback.format_exc()}")
