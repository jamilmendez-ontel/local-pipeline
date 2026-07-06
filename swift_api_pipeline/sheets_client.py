#!/usr/bin/env python3
"""
Google Sheets client -- reads spreadsheet data via Google Drive API CSV export.

Uses the Google Drive API (drive.readonly scope) instead of the Sheets API
because the Sheets API is not enabled in the GCP project. The Drive export
endpoint can export Google Sheets as CSV without needing the Sheets API.

Uses the same Google Cloud project credentials as gmail_client but with a
SEPARATE token (sheets_token.pickle) authenticated as jamil.mendez@ontel.co.

Setup (one-time):
    1. Same credentials.json as Gmail (already in gmail_credentials/)
    2. First run opens browser for Google login -> saves sheets_token.pickle
"""

import csv
import io
import pickle
from pathlib import Path
from typing import List

from google.auth.transport.requests import Request, AuthorizedSession
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

# Drive API scope -- read-only (used for spreadsheet CSV export)
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

# Paths -- same credentials.json, separate token file
_BASE_DIR = Path(__file__).parent
CREDENTIALS_DIR = _BASE_DIR / "gmail_credentials"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"
TOKEN_FILE = CREDENTIALS_DIR / "sheets_token.pickle"


def authenticate_sheets() -> Credentials:
    """
    Authenticate with Google Drive API using OAuth2.
    First run opens browser for consent. Subsequent runs use saved token.
    Returns OAuth2 credentials (used with AuthorizedSession for CSV export).
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
                    f"Credentials not found at {CREDENTIALS_FILE}. "
                    "Download from Google Cloud Console."
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)

        CREDENTIALS_DIR.mkdir(parents=True, exist_ok=True)
        with open(TOKEN_FILE, "wb") as f:
            pickle.dump(creds, f)

    return creds


def read_spreadsheet(creds: Credentials, spreadsheet_id: str) -> List[List[str]]:
    """
    Read all rows from a Google Spreadsheet via Drive API CSV export.

    Args:
        creds: Authenticated OAuth2 credentials.
        spreadsheet_id: The spreadsheet ID from the URL.

    Returns:
        List of rows, where each row is a list of cell values (strings).
        First row is the header row.
    """
    session = AuthorizedSession(creds)
    url = (
        f"https://www.googleapis.com/drive/v3/files/{spreadsheet_id}"
        f"/export?mimeType=text/csv"
    )
    resp = session.get(url)
    resp.raise_for_status()

    # Drive's CSV export omits the charset, so requests falls back to ISO-8859-1
    # and mangles UTF-8 (e.g. "Mañalac" -> "MaÃ±alac"). The export is UTF-8.
    resp.encoding = "utf-8"
    reader = csv.reader(io.StringIO(resp.text))
    return list(reader)


def get_modified_time(creds: Credentials, file_id: str):
    """Return a file's Drive modifiedTime as an RFC3339 string (e.g.
    '2026-07-06T05:12:34.123Z'), or None if unavailable."""
    session = AuthorizedSession(creds)
    url = f"https://www.googleapis.com/drive/v3/files/{file_id}?fields=modifiedTime"
    resp = session.get(url)
    resp.raise_for_status()
    return resp.json().get("modifiedTime")


if __name__ == "__main__":
    print("Authenticating with Google Drive API (for Sheets export)...")
    c = authenticate_sheets()
    print("Authentication successful! sheets_token.pickle saved.")
    print(f"Token file: {TOKEN_FILE}")
