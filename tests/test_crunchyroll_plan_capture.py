"""Regression tests for crunchyroll plan capture.

The legacy ``a or b`` expression treated the sentinel string ``"N/A"``
as a usable account id, which meant the subscription-products endpoint
was skipped whenever ``external_id`` came back as ``"N/A"`` — every
alive Crunchyroll cookie then ended up bucketed under "Unknown" plan
on the dashboard. This test pins down the fixed behaviour.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cookie_checker  # noqa: E402


def _cookies() -> list[dict]:
    return [
        {"domain": "crunchyroll.com", "name": "etp_rt", "value": "rt", "path": "/", "secure": True},
        {"domain": "crunchyroll.com", "name": "device_id", "value": "dev-uuid", "path": "/", "secure": True},
    ]


def _token_resp() -> dict:
    return {
        "status": 200,
        "json": {
            "access_token": "the-token",
            "country": "US",
            "token_type": "Bearer",
            "scope": "account",
            "expires_in": 300,
        },
        "text": "",
        "via": "cffi",
    }


def _make_response(*, status_code: int, json_payload):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_payload
    r.text = ""
    return r


def test_plan_capture_falls_back_to_account_id_when_external_id_is_na() -> None:
    """external_id="N/A" must NOT short-circuit the subscriptions call.

    Before the fix, ``a or b`` returned the truthy string ``"N/A"`` and
    the products endpoint was skipped (plan="Unknown"). After the fix,
    we walk past ``"N/A"`` and use the real ``account_id`` instead.
    """
    captured_urls: list[str] = []

    def fake_safe_get(_session, url, _headers, **_kwargs):
        captured_urls.append(url)
        if url == "https://beta-api.crunchyroll.com/accounts/v1/me":
            return _make_response(
                status_code=200,
                json_payload={
                    "account_id": "real-account-id",
                    # external_id missing on purpose — code stores "N/A"
                    "email_verified": True,
                    "created": "2020-01-01",
                },
            )
        if url == "https://beta-api.crunchyroll.com/accounts/v1/me/profile":
            return _make_response(
                status_code=200,
                json_payload={"username": "akaza", "email": "a@b.com"},
            )
        if "/subs/v3/subscriptions/" in url and url.endswith("/products"):
            # This is the call we must reach — assert via captured_urls below.
            return _make_response(
                status_code=200,
                json_payload={"items": [{"name": "Crunchyroll Mega Fan", "sku": "crunchyroll.megafanpack.monthly"}]},
            )
        return _make_response(status_code=404, json_payload={})

    with patch.object(cookie_checker, "_request_json", return_value=_token_resp()), \
         patch.object(cookie_checker, "_safe_get", side_effect=fake_safe_get):
        r = cookie_checker.check_crunchyroll(_cookies())

    assert r["alive"] is True
    # We must have reached the subscriptions endpoint using account_id,
    # NOT the "N/A" sentinel.
    sub_calls = [u for u in captured_urls if "/subs/v3/subscriptions/" in u]
    assert sub_calls, f"subscription endpoint never called; saw: {captured_urls}"
    assert all("real-account-id" in u for u in sub_calls)
    assert all("N/A" not in u for u in sub_calls)
    # And the plan must round-trip to "Mega Fan".
    assert r["info"]["plan"] == "Mega Fan"


def test_plan_capture_prefers_external_id_when_present() -> None:
    """When the API does return a real external_id, we still use it."""
    captured_urls: list[str] = []

    def fake_safe_get(_session, url, _headers, **_kwargs):
        captured_urls.append(url)
        if url == "https://beta-api.crunchyroll.com/accounts/v1/me":
            return _make_response(
                status_code=200,
                json_payload={
                    "account_id": "fallback-id",
                    "external_id": "real-external-uuid",
                },
            )
        if url == "https://beta-api.crunchyroll.com/accounts/v1/me/profile":
            return _make_response(status_code=200, json_payload={})
        if "/subs/v3/subscriptions/" in url and url.endswith("/products"):
            return _make_response(
                status_code=200,
                json_payload={"items": [{"name": "Ultimate Fan", "sku": "crunchyroll.ultimatefanpack.yearly"}]},
            )
        return _make_response(status_code=404, json_payload={})

    with patch.object(cookie_checker, "_request_json", return_value=_token_resp()), \
         patch.object(cookie_checker, "_safe_get", side_effect=fake_safe_get):
        r = cookie_checker.check_crunchyroll(_cookies())

    sub_calls = [u for u in captured_urls if "/subs/v3/subscriptions/" in u]
    assert any("real-external-uuid" in u for u in sub_calls)
    assert r["info"]["plan"] == "Ultimate Fan"


def test_plan_capture_skipped_when_both_ids_are_na() -> None:
    """If neither id is real, we don't blow up — we just leave plan blank."""

    def fake_safe_get(_session, url, _headers, **_kwargs):
        if url == "https://beta-api.crunchyroll.com/accounts/v1/me":
            # No account_id, no external_id — both stored as "N/A".
            return _make_response(status_code=200, json_payload={})
        if url == "https://beta-api.crunchyroll.com/accounts/v1/me/profile":
            return _make_response(status_code=200, json_payload={})
        # If we got here, the code tried to call the subscriptions
        # endpoint with "N/A" — that's a regression.
        if "/subs/v3/subscriptions/" in url:
            raise AssertionError(
                f"subscriptions endpoint should not be reached with N/A id; got {url}"
            )
        return _make_response(status_code=404, json_payload={})

    with patch.object(cookie_checker, "_request_json", return_value=_token_resp()), \
         patch.object(cookie_checker, "_safe_get", side_effect=fake_safe_get):
        r = cookie_checker.check_crunchyroll(_cookies())

    assert r["alive"] is True
    # Plan key absent (or anything except crashing) is acceptable.
    assert r["info"].get("plan") in (None, "Unknown", "Free")
