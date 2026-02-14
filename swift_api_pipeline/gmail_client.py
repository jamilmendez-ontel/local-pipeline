#!/usr/bin/env python3
"""
Gmail API client for downloading email attachments.

Handles OAuth2 authentication, message search, and attachment download.
Credentials stored in swift_api_pipeline/gmail_credentials/.

Setup (one-time):
    1. Google Cloud Console → enable Gmail API
    2. Create OAuth 2.0 Client ID (Desktop app)
    3. Download credentials.json → gmail_credentials/credentials.json
    4. First run opens browser for Google login → saves token.pickle
"""

import os
import re
import pickle
import base64
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Gmail API scopes — read access + send + Drive file upload
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/drive.file",
]

# Paths relative to this file
_BASE_DIR = Path(__file__).parent
CREDENTIALS_DIR = _BASE_DIR / "gmail_credentials"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"
TOKEN_FILE = CREDENTIALS_DIR / "token.pickle"


def authenticate():
    """
    Authenticate with Gmail API using OAuth2.

    First run opens browser for consent. Subsequent runs use saved token.
    Returns an authorized Gmail API service object.
    """
    creds = None

    # Load existing token
    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

        # Validate scopes — force re-auth if stored token is missing required scopes
        if creds and hasattr(creds, "scopes") and creds.scopes:
            if not set(SCOPES).issubset(creds.scopes):
                creds = None  # Force re-auth with updated scopes

    # Refresh or get new credentials
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {CREDENTIALS_FILE}. "
                    "Download from Google Cloud Console → OAuth 2.0 Client IDs."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save token for next run
        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("gmail", "v1", credentials=creds)


def authenticate_drive():
    """
    Authenticate with Google Drive API using the same OAuth2 credentials.

    Returns an authorized Drive API service object.
    """
    creds = None

    if TOKEN_FILE.exists():
        with open(TOKEN_FILE, "rb") as f:
            creds = pickle.load(f)

        if creds and hasattr(creds, "scopes") and creds.scopes:
            if not set(SCOPES).issubset(creds.scopes):
                creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Gmail credentials not found at {CREDENTIALS_FILE}. "
                    "Download from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return build("drive", "v3", credentials=creds)


def search_messages(service, query: str, max_results: int = 100) -> List[Dict]:
    """
    Search Gmail messages matching a query string.

    Args:
        service: Authorized Gmail API service
        query: Gmail search query (e.g. 'subject:"Daily Revenue Report" has:attachment')
        max_results: Maximum messages to return

    Returns:
        List of message dicts with 'id' and 'threadId'
    """
    messages = []
    page_token = None

    while True:
        result = service.users().messages().list(
            userId="me",
            q=query,
            pageToken=page_token,
            maxResults=min(max_results - len(messages), 100)
        ).execute()

        batch = result.get("messages", [])
        messages.extend(batch)

        if len(messages) >= max_results:
            break

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def get_message_details(service, message_id: str) -> Dict:
    """
    Get message metadata including received date and subject.

    Args:
        service: Authorized Gmail API service
        message_id: Gmail message ID

    Returns:
        Dict with 'id', 'received_date' (datetime), 'subject', 'parts' (attachments)
    """
    msg = service.users().messages().get(
        userId="me", id=message_id, format="full"
    ).execute()

    # Parse internalDate (epoch ms) → datetime
    internal_date_ms = int(msg.get("internalDate", 0))
    received_date = datetime.fromtimestamp(
        internal_date_ms / 1000, tz=timezone.utc
    )

    # Extract subject from headers
    headers = msg.get("payload", {}).get("headers", [])
    subject = next(
        (h["value"] for h in headers if h["name"].lower() == "subject"),
        "(no subject)"
    )

    # Collect attachment info
    parts = []
    _collect_parts(msg.get("payload", {}), parts)

    return {
        "id": message_id,
        "received_date": received_date,
        "subject": subject,
        "parts": parts,
    }


def _collect_parts(payload: dict, parts: list):
    """Recursively collect attachment parts from a message payload."""
    filename = payload.get("filename", "")
    body = payload.get("body", {})
    attachment_id = body.get("attachmentId")

    if filename and attachment_id:
        parts.append({
            "filename": filename,
            "attachment_id": attachment_id,
            "mime_type": payload.get("mimeType", ""),
            "size": body.get("size", 0),
        })

    for sub_part in payload.get("parts", []):
        _collect_parts(sub_part, parts)


def download_attachment(
    service,
    message_id: str,
    filename_pattern: str,
    output_dir: str
) -> Optional[str]:
    """
    Download a specific attachment from a Gmail message.

    Args:
        service: Authorized Gmail API service
        message_id: Gmail message ID
        filename_pattern: Substring to match in attachment filename (e.g. "Aging+Detail")
        output_dir: Directory to save the downloaded file

    Returns:
        Full path to downloaded file, or None if no matching attachment found
    """
    details = get_message_details(service, message_id)

    # Find matching attachment
    matching_part = None
    for part in details["parts"]:
        if filename_pattern in part["filename"]:
            matching_part = part
            break

    if not matching_part:
        return None

    # Download attachment data
    att = service.users().messages().attachments().get(
        userId="me",
        messageId=message_id,
        id=matching_part["attachment_id"]
    ).execute()

    file_data = base64.urlsafe_b64decode(att["data"])

    # Save to output directory
    os.makedirs(output_dir, exist_ok=True)
    output_path = os.path.join(output_dir, matching_part["filename"])

    with open(output_path, "wb") as f:
        f.write(file_data)

    return output_path
