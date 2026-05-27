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
    _absorb_jwt_claims,
    _absorb_profile,
    _decode_jwt_payload,
    _finalise,
    _looks_like_akamai_challenge,
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


def test_decode_jwt_payload_tolerates_out_of_range_exp() -> None:
    """An absurd `exp` (e.g. year 9999+) must not abort scan() — just
    skip the exp keys so the HTTP probe still runs."""
    huge = 10**14  # well past datetime's range on most libcs
    token = _make_jwt({"sub": "u", "exp": huge})
    info = _decode_jwt_payload(token)
    assert info is not None
    # exp keys are absent because the value couldn't be parsed.
    assert "exp" not in info
    assert "exp_iso" not in info
    assert "expired" not in info
    # But the rest of the payload still came through.
    assert info["user_id"] == "u"


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


def test_looks_like_akamai_challenge_detects_bm_verify() -> None:
    """Freepik fronts /api/* with Akamai BotManager; the challenge is a
    200 + HTML with a ``bm-verify`` meta-refresh redirect. The adapter
    must distinguish this from a real auth verdict."""
    body = (
        '<!DOCTYPE html><html><head><meta http-equiv="refresh" '
        "content=\"5; URL='/api/profile/v2/me?bm-verify=AAQAAAAN_xxx'\" />"
        "</head><body></body></html>"
    )
    assert _looks_like_akamai_challenge(body) is True
    assert _looks_like_akamai_challenge("") is False
    assert _looks_like_akamai_challenge("normal json response") is False


def test_scan_akamai_challenge_falls_through_without_marking_dead(monkeypatch) -> None:
    """Akamai-only failures must NOT be classified as a dead cookie;
    they signal an environmental block (untrusted IP)."""
    token = _make_jwt({"sub": "u", "exp": int(time.time()) + 3600})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    bm_body = (
        '<!DOCTYPE html><html><head><meta http-equiv="refresh" '
        "content=\"5; URL='/api/profile/v2/me?bm-verify=AAQ_xxx'\"/>"
        "</head></html>"
    )
    fake = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=200, text=bm_body,
            headers={"content-type": "text/html"},
        ),
        "https://www.freepik.com/api/profile/me": _FakeResponse(
            status_code=200, text=bm_body,
            headers={"content-type": "text/html"},
        ),
        "https://www.freepik.com/api/regular/users/v1/me": _FakeResponse(
            status_code=200, text=bm_body,
            headers={"content-type": "text/html"},
        ),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake)

    result = adapter.scan()
    assert result.alive is False
    # Critical: bot-challenge != dead cookie; the user may retry behind
    # a residential proxy and have the same cookie come back ALIVE.
    assert result.is_dead is False
    assert "akamai" in (result.error or "").lower()


def test_scan_populates_jwt_claims_on_environmental_block(monkeypatch) -> None:
    """Even when every endpoint is bot-challenged, the JWT-derived info
    should still land in ``result.info`` so the HIT card (or a retry
    behind a proxy) has the email/name/plan-hint already extracted."""
    token = _make_jwt({
        "sub": "40cee198d7bc4c8ab3cb388e2edcaee0",
        "email": "fotosartdesign@gmail.com",
        "name": "user5967936",
        "picture": "https://lh3.googleusercontent.com/pic.jpg",
        "accounts_user_id": 5967936,
        "scopes": "freepik/images freepik/videos flaticon/png flaticon/svg",
        "team_id": None,
        "firebase": {"sign_in_provider": "custom",
                     "identities": {"google.com": ["1146"]}},
        "email_verified": True,
        "exp": int(time.time()) + 3600,
    })
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    bm_body = '<html>bm-verify_xxx</html>'
    fake = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=200, text=bm_body, headers={"content-type": "text/html"}),
        "https://www.freepik.com/api/profile/me": _FakeResponse(
            status_code=200, text=bm_body, headers={"content-type": "text/html"}),
        "https://www.freepik.com/api/regular/users/v1/me": _FakeResponse(
            status_code=200, text=bm_body, headers={"content-type": "text/html"}),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake)

    result = adapter.scan()
    assert result.alive is False
    assert result.is_dead is False
    info = result.info
    assert info["email"] == "fotosartdesign@gmail.com"
    assert info["username"] == "user5967936"
    assert info["accounts_user_id"] == 5967936
    assert info["plan_hint"] == "Premium"
    assert info["sign_in_provider"] == "google"
    assert info["email_verified"] is True


def test_scan_429_rate_limit_does_not_mark_dead(monkeypatch) -> None:
    """429 is environmental (rate limit), not a dead-cookie verdict."""
    token = _make_jwt({"sub": "u", "exp": int(time.time()) + 3600})
    jar = _jar_with(token)
    adapter = FreepikAdapter(jar)

    fake = _FakeClient({
        "https://www.freepik.com/api/profile/v2/me": _FakeResponse(
            status_code=429, text="", headers={"content-type": "text/plain"}),
        "https://www.freepik.com/api/profile/me": _FakeResponse(
            status_code=429, text="", headers={"content-type": "text/plain"}),
        "https://www.freepik.com/api/regular/users/v1/me": _FakeResponse(
            status_code=429, text="", headers={"content-type": "text/plain"}),
    })
    monkeypatch.setattr(adapter, "make_client", lambda **kw: fake)

    result = adapter.scan()
    assert result.alive is False
    # 429 = rate limited != dead cookie. The user can retry.
    assert result.is_dead is False
    assert "429" in (result.error or "")


def test_absorb_jwt_claims_classifies_free_premium_team() -> None:
    """Plan-hint derivation from JWT scopes."""
    out: dict[str, Any] = {}
    _absorb_jwt_claims({"raw": {"scopes": "freepik/images"}}, out)
    assert out["plan_hint"] == "Free"

    out2: dict[str, Any] = {}
    _absorb_jwt_claims(
        {"raw": {"scopes": "freepik/images freepik/videos flaticon/png"}},
        out2,
    )
    assert out2["plan_hint"] == "Premium"

    out3: dict[str, Any] = {}
    _absorb_jwt_claims({"raw": {"team_id": "team_42", "scopes": "freepik/images"}}, out3)
    assert out3["plan_hint"] == "Team"
    assert out3["team_id"] == "team_42"


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


def test_freepik_checker_registered_in_legacy_dispatch() -> None:
    """``CHECKERS["freepik.com"]`` must exist so the standalone CLI
    doesn't dead-end on "no checker for freepik.com" once
    ``detect_site()`` starts returning the new site."""
    import cookie_checker

    assert "freepik.com" in cookie_checker.CHECKERS
    assert callable(cookie_checker.CHECKERS["freepik.com"])


def test_check_freepik_bridges_to_cookiescanner_adapter() -> None:
    """The legacy ``check_freepik`` envelope mirrors ScanResult fields."""
    import cookie_checker

    # Missing GR_TOKEN → adapter sets is_dead and an error string;
    # the legacy envelope should mirror that without any HTTP traffic.
    legacy_cookies = [{"domain": ".freepik.com", "name": "junk", "value": "x"}]
    result = cookie_checker.check_freepik(legacy_cookies)
    assert result["alive"] is False
    assert result["is_dead"] is True
    assert "GR_TOKEN" in (result["error"] or "")
    assert isinstance(result["info"], dict)
