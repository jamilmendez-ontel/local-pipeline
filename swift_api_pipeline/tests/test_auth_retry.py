# swift_api_pipeline/tests/test_auth_retry.py
"""BaseExtractor.authenticate retries Swift's transient auth failures (5xx,
429, network) and fails fast on credential errors (2026-10-14 incident)."""
import os
import sys
from threading import Lock

import pytest
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import base_extractor
from base_extractor import (BaseExtractor, AUTH_MAX_ATTEMPTS, AUTH_BACKOFF_SECONDS,
                            REAUTH_DEDUPE_SECONDS)


class FakeResponse:
    def __init__(self, status_code, body=None):
        self.status_code = status_code
        self._body = body or {}

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} Error", response=self)


def make_extractor():
    # Skip __init__: it opens a DB pool.
    ext = BaseExtractor.__new__(BaseExtractor)
    ext.base_url = "https://swift.test"
    ext.token = None
    ext.token_lock = Lock()
    ext._token_obtained_at = 0.0
    ext._auth_failure = None
    return ext


@pytest.fixture
def sleeps(monkeypatch):
    calls = []
    monkeypatch.setattr(base_extractor.time, "sleep", calls.append)
    return calls


def script_posts(monkeypatch, outcomes):
    """Each outcome is a FakeResponse to return or an exception to raise."""
    calls = []

    def fake_post(*args, **kwargs):
        calls.append(kwargs)
        outcome = outcomes[len(calls) - 1]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(base_extractor.requests, "post", fake_post)
    return calls


def test_succeeds_first_try_without_sleeping(monkeypatch, sleeps):
    calls = script_posts(monkeypatch, [FakeResponse(200, {"idToken": "tok"})])
    assert make_extractor().authenticate() == "tok"
    assert len(calls) == 1
    assert calls[0]["timeout"] > 0
    assert sleeps == []


def test_retries_500_then_succeeds(monkeypatch, sleeps):
    calls = script_posts(monkeypatch, [
        FakeResponse(500), FakeResponse(503), FakeResponse(200, {"idToken": "tok"}),
    ])
    assert make_extractor().authenticate() == "tok"
    assert len(calls) == 3
    assert len(sleeps) == 2


def test_retries_429_and_network_errors(monkeypatch, sleeps):
    script_posts(monkeypatch, [
        FakeResponse(429),
        requests.ConnectionError("reset"),
        requests.Timeout("slow"),
        FakeResponse(200, {"idToken": "tok"}),
    ])
    assert make_extractor().authenticate() == "tok"
    assert len(sleeps) == 3


@pytest.mark.parametrize("status", [400, 401, 403])
def test_credential_error_fails_fast(monkeypatch, sleeps, status):
    calls = script_posts(monkeypatch, [FakeResponse(status)])
    with pytest.raises(requests.HTTPError):
        make_extractor().authenticate()
    assert len(calls) == 1
    assert sleeps == []


def test_gives_up_after_max_attempts(monkeypatch, sleeps):
    calls = script_posts(monkeypatch, [FakeResponse(500)] * AUTH_MAX_ATTEMPTS)
    ext = make_extractor()
    with pytest.raises(requests.HTTPError):
        ext.authenticate()
    assert len(calls) == AUTH_MAX_ATTEMPTS
    assert sleeps == list(AUTH_BACKOFF_SECONDS)
    assert ext.token is None


def test_network_error_on_last_attempt_propagates(monkeypatch, sleeps):
    script_posts(monkeypatch, [FakeResponse(502)] * (AUTH_MAX_ATTEMPTS - 1)
                 + [requests.ConnectionError("down")])
    with pytest.raises(requests.ConnectionError):
        make_extractor().authenticate()


def test_exhausted_login_trips_breaker_for_later_callers(monkeypatch, sleeps):
    # Review finding: N workers re-logging after a 401 during an outage each
    # waited the full backoff in turn behind token_lock. After one exhausted
    # login, every later call must fail at once without touching Swift.
    calls = script_posts(monkeypatch, [FakeResponse(500)] * AUTH_MAX_ATTEMPTS)
    ext = make_extractor()
    with pytest.raises(requests.HTTPError):
        ext.authenticate()
    sleeps.clear()
    for call in (ext.authenticate, ext.reauthenticate, ext.get_auth_headers):
        with pytest.raises(RuntimeError, match="already failed"):
            call()
    assert len(calls) == AUTH_MAX_ATTEMPTS
    assert sleeps == []


def test_credential_error_also_trips_breaker(monkeypatch, sleeps):
    calls = script_posts(monkeypatch, [FakeResponse(401)])
    ext = make_extractor()
    with pytest.raises(requests.HTTPError):
        ext.authenticate()
    with pytest.raises(RuntimeError):
        ext.reauthenticate()
    assert len(calls) == 1


def test_reauth_herd_logs_in_once(monkeypatch, sleeps):
    # Several workers hit a 401 on the same expired token: the first re-login
    # mints a fresh token, the rest get it back without logging in again.
    clock = [1000.0]
    monkeypatch.setattr(base_extractor.time, "monotonic", lambda: clock[0])
    calls = script_posts(monkeypatch, [
        FakeResponse(200, {"idToken": "old"}),
        FakeResponse(200, {"idToken": "new"}),
    ])
    ext = make_extractor()
    assert ext.authenticate() == "old"
    clock[0] += 3600  # token expired an hour later
    assert [ext.reauthenticate() for _ in range(5)] == ["new"] * 5
    assert len(calls) == 2


def test_reauth_after_dedupe_window_logs_in_again(monkeypatch, sleeps):
    clock = [1000.0]
    monkeypatch.setattr(base_extractor.time, "monotonic", lambda: clock[0])
    calls = script_posts(monkeypatch, [
        FakeResponse(200, {"idToken": "a"}),
        FakeResponse(200, {"idToken": "b"}),
    ])
    ext = make_extractor()
    ext.authenticate()
    clock[0] += REAUTH_DEDUPE_SECONDS + 1
    assert ext.reauthenticate() == "b"
    assert len(calls) == 2
