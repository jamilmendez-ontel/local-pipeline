"""Fire GitHub repository_dispatch events from the local pipeline.

Used to notify downstream projects (like cop-date-validator) that fresh
data is ready. Dispatch failures are logged but never fail the caller --
pipeline success should not depend on downstream notifications.
"""
from __future__ import annotations

import os
from typing import Any

import httpx

from config import get_logger

logger = get_logger("github_trigger")


def fire_dispatch(
    repo: str,
    event_type: str,
    client_payload: dict[str, Any] | None = None,
) -> bool:
    """Fire GitHub repository_dispatch. Returns True on 204 No Content.

    Never raises -- logs and returns False on any error.
    """
    token = os.getenv("GITHUB_PAT")
    if not token:
        logger.warning(f"GITHUB_PAT not set; skipping dispatch '{event_type}' -> {repo}")
        return False
    try:
        resp = httpx.post(
            f"https://api.github.com/repos/{repo}/dispatches",
            headers={
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github+json",
            },
            json={
                "event_type": event_type,
                "client_payload": client_payload or {},
            },
            timeout=10,
        )
        resp.raise_for_status()
        logger.info(f"fired '{event_type}' -> {repo} (status {resp.status_code})")
        return True
    except Exception as e:
        logger.error(f"dispatch to {repo} failed: {type(e).__name__}: {e}")
        return False
