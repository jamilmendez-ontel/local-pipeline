#!/usr/bin/env python3
"""
Google Calendar API client for reading leave/PTO events.

Uses the same Google Cloud project credentials as gmail_client but with a
SEPARATE token (calendar_token.pickle) so it can authenticate as a different
Google account (e.g. jamil.mendez@ontel.co for calendar access).

Setup (one-time):
    1. Google Cloud Console -> enable Google Calendar API
    2. Same credentials.json as Gmail (already in gmail_credentials/)
    3. First run opens browser for Google login -> saves calendar_token.pickle
"""

import pickle
from pathlib import Path
from typing import List, Dict, Optional

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

# Calendar API scope — read-only
SCOPES = [
    "https://www.googleapis.com/auth/calendar.readonly",
]

# Paths — same credentials.json, separate token file
_BASE_DIR = Path(__file__).parent
CREDENTIALS_DIR = _BASE_DIR / "gmail_credentials"
CREDENTIALS_FILE = CREDENTIALS_DIR / "credentials.json"
TOKEN_FILE = CREDENTIALS_DIR / "calendar_token.pickle"


def authenticate_calendar():
    """
    Authenticate with Google Calendar API using OAuth2.

    First run opens browser for consent (sign in with the account that
    has access to the target calendar). Subsequent runs use saved token.

    Returns an authorized Calendar API service object.
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

    return build("calendar", "v3", credentials=creds)
