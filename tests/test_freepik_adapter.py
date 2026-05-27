"""Tests for the freepik.com cookiescanner adapter.

These exercise the pure-function logic (JWT decode, payload absorption,
plan normalisation, credits extraction) and the public ``scan()``
entrypoint with a stubbed HTTP client so no live freepik.com requests
fire during CI.
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass
from typing import Any

import pytest

from cookiescanner.cookies import Cookie, CookieJar
from cookiescanner.sites.freepik import (
    FreepikAdapter,
    _absorb_credits,
    _absorb_profile,
    _decode_jwt_payload,
    _finalise,
    _looks_like_cf_challenge,
    _looks_like_user,
)


# ── helpers ────────────────────────────────────────────────────────────


def _make_jwt(payload: dict[str, Any]) -> str:
    """Build a JWT with the given payload (signature isn't checked)."""
    header = base64.urlsafe_b64encode(b'{"alg":"HS256","typ":"JWT"}').rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    return f"{header}.{body}.signature"


def _jar_with(token: str) -> CookieJar:
    return CookieJar([
        Cookie(name="GR_TOKEN", value=token, domain=".freepik.com"),
        Cookie(name="GR_REFRESH_TOKEN", value="refresh", domain=".freepik.com"),
    ])


@dataclass
class _FakeResponse:
    status_code: int
    text: str = ""
    headers: dict[str, str] = None  # type: ignore[assignment]
    _json: Any = None

    def __post_init__(self) -> None:
        if self.headers is None:
            self.headers = {"content-type": "application/json"}

    def json(self) -> Any:
        return self._json


class _FakeClient:
    """Minimal stub for the ``HttpClient`` context manager."""

    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self.routes = routes
        self.calls: list[str] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def get(self, url: str, *, headers: dict[str, str] | None = None, **kwargs: Any) -> _FakeResponse:
        self.calls.append(url)
        if url in self.routes:
            return self.routes[url]
        return _FakeResponse(status_code=404, text="not found")


# ── unit tests: pure helpers ───────────────────────────────────────────


def test_decode_jwt_payload_extracts_user_email_exp() -> None:
    exp = int(time.time()) + 3600
    token = _make_jwt({"sub": "user-123", "email": "a@b.com", "exp": exp})
    info = _decode_jwt_payload(token)
    assert info is not None
    assert info["user_id"] == "user-123"
    assert info["email"] == "a@b.com"
    assert info["expired"] is False
    assert info["exp"] == exp
    assert info["exp_iso"].endswith("+00:00")


def test_decode_jwt_payload_flags_expired_tokens() -> None:
    exp = int(time.time()) - 60
    token = _make_jwt({"sub": "u", "exp": exp})
    info = _decode_jwt_payload(token)
    assert info and info["expired"] is True


def test_decode_jwt_payload_handles_garbage() -> None:
    assert _decode_jwt_payload("not-a-jwt") is None
    assert _decode_jwt_payload("not.a.jwt.payload") is None


def test_looks_like_user_accepts_top_level_and_nested() -> None:
    assert _looks_like_user({"email": "x@y.com"})
    assert _looks_like_user({"data": {"id": 42}})
    assert _looks_like_user({"profile": {"username": "akaza"}})
    assert not _looks_like_user({"unrelated": "garbage"})
    assert not _looks_like_user({})


def test_looks_like_cf_challenge() -> None:
    assert _looks_like_cf_challenge("Just a moment...")
    assert _looks_like_cf_challenge('<div class="challenge-platform"></div>')
    assert not _looks_like_cf_challenge('{"data": {"email": "a@b.com"}}')


def test_absorb_profile_maps_freepik_fields() -> None:
    info: dict[str, Any] = {}
    payload = {
        "data": {
            "id": 99,
            "email": "akaza@freepik.com",
            "firstName": "Akaza",
            "country": "JP",
            "subscription_type": "premium plus",
            "isPremiumPlus": True,
            "subscription_renewal": "2026-01-01T00:00:00Z",
            "credits_left": 142,
            "credits_total": 200,
        }
    }
    _absorb_profile(payload, info)
    assert info["email"] == "akaza@freepik.com"
    assert info["name"] == "Akaza"
    assert info["country"] == "JP"
    assert info["user_id"] == 99
    assert info["plan"] == "premium plus"  # not yet normalised — _finalise does that
    assert info["premium_plus"] is True
    assert info["credits"] == 142
    assert info["credits_total"] == 200


def test_absorb_credits_handles_nested_and_string_numbers() -> None:
    info: dict[str, Any] = {}
    _absorb_credits(
        {"credits": {"remaining_credits": "42", "totalCredits": 100}},
        info,
    )
    assert info["credits"] == 42
    assert info["credits_total"] == 100


def test_finalise_normalises_premium_plus_premium_free() -> None:
    info = {"plan": "premium plus", "premium_plus": True}
    _finalise(info)
    assert info["plan"] == "Premium+"
    assert info["is_pro"] is True

    info = {"plan": "Premium"}
    _finalise(info)
    assert info["plan"] == "Premium"
    assert info["is_pro"] is True

    info = {"plan": "regular"}
    _finalise(info)
    assert info["plan"] == "Free"
    assert info["is_pro"] is False

    info = {"premium": True}
    _finalise(info)
    assert info["plan"] == "Premium"

    info = {}
    _finalise(info)
    assert info["plan"] == "Free"
    assert info["is_pro"] is False


# ── scan() entrypoint ──────────────────────────────────────────────────


def test_scan_missing_gr_token_marks_dead() -> None:
    jar = CookieJar([Cookie(name="OptanonAlert", value="x", domain=".freepik.com")])
    adapter = FreepikAdapter(jar)
    result = adapter.scan()
    assert result.alive is False
    assert result.is_dead is True
    assert "GR_TOKEN" in (result.error or "")


def test_scan_expired_jwt_short_circuits_to_dead() -> None:
    token = _make_jwt({"sub": "u", "exp": int(time.time()) - 60})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)
    result = adapter.scan()
    # Even though we technically need HTTP, expired JWT is authoritative.
    assert result.alive is False
    assert result.is_dead is True
    assert "expired" in (result.error or "").lower()


def test_scan_happy_path_first_endpoint_returns_alive_with_credits(monkeypatch) -> None:
    token = _make_jwt({"sub": "user-x", "email": "akaza@freepik.com", "exp": int(time.time()) + 3600})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    profile_payload = {
        "data": {
            "id": 99,
            "email": "akaza@freepik.com",
            "firstName": "Akaza",
            "country": "JP",
            "subscription_type": "premium",
            "isPremium": True,
            "credits_left": 142,
            "credits_total": 200,
        }
    }
    fake_client = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=200,
            text=json.dumps(profile_payload),
            _json=profile_payload,
        ),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake_client)

    result = adapter.scan()
    assert result.alive is True
    assert result.is_dead is False
    assert result.error is None
    assert result.info["email"] == "akaza@freepik.com"
    assert result.info["plan"] == "Premium"
    assert result.info["is_pro"] is True
    assert result.info["credits"] == 142
    assert result.info["credits_total"] == 200
    # Should have stopped at the first endpoint.
    assert len(fake_client.calls) == 1


def test_scan_real_401_marks_dead(monkeypatch) -> None:
    token = _make_jwt({"sub": "u", "exp": int(time.time()) + 3600})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    fake_client = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=401, text='{"error":"unauthorized"}', _json={"error": "unauthorized"}
        ),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake_client)

    result = adapter.scan()
    assert result.alive is False
    assert result.is_dead is True
    assert "401" in (result.error or "")


def test_scan_cloudflare_challenge_falls_through_then_errors(monkeypatch) -> None:
    token = _make_jwt({"sub": "u", "exp": int(time.time()) + 3600})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    cf_body = "<html>Just a moment...<div class='challenge-platform'/></html>"
    fake_client = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=403, text=cf_body, _json=None,
            headers={"content-type": "text/html"},
        ),
        "https://www.freepik.com/api/profile/me": _FakeResponse(
            status_code=403, text=cf_body, _json=None,
            headers={"content-type": "text/html"},
        ),
        "https://www.freepik.com/api/regular/users/v1/me": _FakeResponse(
            status_code=403, text=cf_body, _json=None,
            headers={"content-type": "text/html"},
        ),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake_client)

    result = adapter.scan()
    assert result.alive is False
    # CF-only failures should NOT be classified as a dead cookie — they
    # signal an environmental block (IP not trusted) and the cookie may
    # still be valid behind a proper residential proxy.
    assert result.is_dead is False
    assert "cloudflare" in (result.error or "").lower()


def test_scan_falls_back_to_dedicated_credits_endpoint(monkeypatch) -> None:
    token = _make_jwt({"sub": "u", "exp": int(time.time()) + 3600})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    me_payload = {"data": {"email": "a@b.com", "subscription_type": "free"}}
    credits_payload = {"credits": 10, "credits_total": 10}
    fake_client = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=200,
            text=json.dumps(me_payload),
            _json=me_payload,
        ),
        "https://www.freepik.com/api/profile/v2/credits": _FakeResponse(
            status_code=200,
            text=json.dumps(credits_payload),
            _json=credits_payload,
        ),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake_client)

    result = adapter.scan()
    assert result.alive is True
    assert result.info["credits"] == 10
    assert result.info["credits_total"] == 10
    assert result.info["plan"] == "Free"


# ── registry wiring ────────────────────────────────────────────────────


def test_freepik_adapter_is_registered() -> None:
    from cookiescanner.sites import all_adapters

    sites = {a.SITE for a in all_adapters()}
    assert "freepik.com" in sites


def test_freepik_in_bot_supported_sites() -> None:
    from tgbot.config import SUPPORTED_SITE_IDS

    assert "freepik.com" in SUPPORTED_SITE_IDS


def test_freepik_in_scanner_cs_sites() -> None:
    from tgbot.scanner import CS_SITES

    assert "freepik.com" in CS_SITES


def test_freepik_in_plan_order() -> None:
    from tgbot.constants import PLAN_ORDER

    assert "freepik.com" in PLAN_ORDER
    assert "Premium+" in PLAN_ORDER["freepik.com"]


def test_freepik_detect_site_from_filename() -> None:
    import cookie_checker

    cookies = [{"domain": "", "name": "GR_TOKEN", "value": "x"}]
    assert cookie_checker.detect_site(cookies, "my_freepik_session.txt") == "freepik.com"


def test_freepik_detect_site_from_known_cookie_alone() -> None:
    import cookie_checker

    cookies = [{"domain": "", "name": "GR_TOKEN", "value": "x"}]
    assert cookie_checker.detect_site(cookies, "") == "freepik.com"


def test_freepik_detect_site_from_domain() -> None:
    import cookie_checker

    cookies = [{"domain": ".freepik.com", "name": "foo", "value": "x"}]
    assert cookie_checker.detect_site(cookies, "anon.txt") == "freepik.com"
