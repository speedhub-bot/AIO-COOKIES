"""freepik.com adapter.

Auth model: a JWT in the ``GR_TOKEN`` HttpOnly cookie set on
``.freepik.com``. ``GR_REFRESH_TOKEN`` is the refresh-token companion.
The web SPA also relies on ``OptanonAlertBoxClosed`` / ``_freepik_*``
trackers and a CF-issued ``cf_clearance``, but ``GR_TOKEN`` alone is
enough to authenticate every ``/api/*`` endpoint the bot needs.

Strategy — keep it cheap and resilient. Freepik rotates endpoint
paths every few months ("v1" → "v2" → "regular/users/v1"), so we
probe a small list of "me" endpoints and stop on the first that
returns a fully-shaped user payload. Premium / credits are usually
embedded in that same response. If we get a 401 / 403 / login HTML
redirect we mark the cookie dead; if every probe returns a CF
challenge or 5xx we surface that as an environmental error so the
bot's dashboard can bucket it correctly.

Endpoints probed (in order, first success wins):

    GET https://www.freepik.com/api/profile/v2/me
    GET https://www.freepik.com/api/profile/me
    GET https://www.freepik.com/api/regular/users/v1/me

Credits endpoints probed (if the first didn't include credits):

    GET https://www.freepik.com/api/profile/v2/credits
    GET https://www.freepik.com/api/profile/credits
    GET https://www.freepik.com/api/regular/users/v1/credits
"""

from __future__ import annotations

import base64
import json
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class FreepikAdapter(SiteAdapter):
    SITE = "freepik.com"
    HOST = "www.freepik.com"
    BASE_URL = "https://www.freepik.com"
    KNOWN_COOKIES = ("GR_TOKEN", "GR_REFRESH_TOKEN")

    SUBDOMAIN_HOSTS = (
        "www.freepik.com",
        "freepik.com",
        ".freepik.com",
        "id.freepik.com",
        "api.freepik.com",
    )

    PROFILE_ENDPOINTS: tuple[str, ...] = (
        "/api/profile/v2/me",
        "/api/profile/me",
        "/api/regular/users/v1/me",
    )

    CREDITS_ENDPOINTS: tuple[str, ...] = (
        "/api/profile/v2/credits",
        "/api/profile/credits",
        "/api/regular/users/v1/credits",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for host in self.SUBDOMAIN_HOSTS:
            merged.update(self.jar.for_host(host))
        return merged

    # ----- entrypoint --------------------------------------------------

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        cookies = self.host_cookies()
        token = cookies.get("GR_TOKEN")
        if not token:
            result.error = (
                "freepik.com session requires the GR_TOKEN auth cookie; "
                "none found in the jar."
            )
            result.is_dead = True
            return result

        # Decode the JWT payload for an early dead-cookie verdict so we
        # can skip the HTTP probe altogether for expired tokens.
        jwt_info = _decode_jwt_payload(token)
        if jwt_info:
            if jwt_info.get("user_id"):
                result.info["user_id"] = jwt_info["user_id"]
            if jwt_info.get("email"):
                result.info["email"] = jwt_info["email"]
            if jwt_info.get("exp_iso"):
                result.info["token_expires"] = jwt_info["exp_iso"]
            if jwt_info.get("expired"):
                result.error = (
                    f"GR_TOKEN JWT expired at "
                    f"{jwt_info.get('exp_iso', 'unknown')}"
                )
                result.is_dead = True
                return result

        headers = self.common_headers()
        # Some Freepik endpoints require the Authorization header even
        # when GR_TOKEN is present in the cookie jar; send both.
        headers["Authorization"] = f"Bearer {token}"

        with self.make_client(extra_headers=headers) as http:
            profile_payload: dict[str, Any] | None = None
            last_error: str | None = None
            cf_or_5xx_only = True
            for path in self.PROFILE_ENDPOINTS:
                url = self.BASE_URL + path
                r = http.get(url)
                result.endpoints_tried.append(
                    {"url": url, "status": r.status_code, "len": len(r.text)}
                )
                if r.status_code in (401, 403):
                    text = r.text or ""
                    if _looks_like_cf_challenge(text):
                        last_error = (
                            f"{path} returned a Cloudflare challenge "
                            f"(403); IP not trusted, use a residential proxy."
                        )
                        continue
                    # Real 401/403 — Freepik refused the token.
                    result.error = (
                        f"{path} returned HTTP {r.status_code} "
                        "(cookie unauthorized — session dead)."
                    )
                    result.is_dead = True
                    return result
                if r.status_code in (301, 302, 303, 307, 308):
                    location = (
                        r.headers.get("location")
                        or r.headers.get("Location")
                        or ""
                    )
                    if "/login" in location.lower():
                        result.error = (
                            f"{path} redirected to login ({location[:90]}); "
                            "cookie dead."
                        )
                        result.is_dead = True
                        return result
                    last_error = (
                        f"{path} redirected to {location[:90]}"
                    )
                    continue
                if r.status_code >= 500:
                    last_error = f"{path} returned HTTP {r.status_code}"
                    continue
                if r.status_code != 200:
                    cf_or_5xx_only = False
                    last_error = f"{path} returned HTTP {r.status_code}"
                    continue
                cf_or_5xx_only = False
                payload = self.try_json(r)
                if isinstance(payload, dict) and _looks_like_user(payload):
                    profile_payload = payload
                    break
                last_error = f"{path} returned 200 with no user payload"

            if profile_payload is None:
                # Differentiate transient (CF / 5xx) from authoritative
                # "no user data anywhere" — the latter is a dead cookie.
                result.error = last_error or "no profile endpoint succeeded"
                if not cf_or_5xx_only:
                    result.is_dead = True
                return result

            result.alive = True
            _absorb_profile(profile_payload, result.info)

            # If credits weren't embedded in the profile response, fetch
            # them from the dedicated endpoint.
            if "credits" not in result.info:
                for path in self.CREDITS_ENDPOINTS:
                    url = self.BASE_URL + path
                    r = http.get(url)
                    result.endpoints_tried.append(
                        {"url": url, "status": r.status_code, "len": len(r.text)}
                    )
                    if r.status_code != 200:
                        continue
                    payload = self.try_json(r)
                    if isinstance(payload, dict):
                        _absorb_credits(payload, result.info)
                        if "credits" in result.info:
                            break

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


_CF_MARKERS = (
    "Just a moment...",
    "cf-chl-bypass",
    "challenge-platform",
    "_cf_chl_opt",
    "cf-mitigated",
)


def _looks_like_cf_challenge(text: str) -> bool:
    return any(m in text for m in _CF_MARKERS)


def _looks_like_user(payload: dict[str, Any]) -> bool:
    """True if the response shape resembles a populated user profile.

    Freepik wraps the user under different keys depending on which API
    revision serves the request, so we accept any of the common shapes.
    """
    if not isinstance(payload, dict):
        return False
    if any(k in payload for k in ("email", "username", "user_id", "id")):
        return True
    nested = payload.get("data") or payload.get("user") or payload.get("profile")
    if isinstance(nested, dict) and any(
        k in nested for k in ("email", "username", "user_id", "id")
    ):
        return True
    return False


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Best-effort decode of the JWT payload segment for early checks."""
    import time
    from datetime import datetime, timezone

    parts = token.split(".")
    if len(parts) < 2:
        return None
    payload_b64 = parts[1]
    payload_b64 += "=" * (-len(payload_b64) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        data = json.loads(raw)
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    out: dict[str, Any] = {}
    exp = data.get("exp")
    if isinstance(exp, (int, float)):
        out["exp"] = int(exp)
        out["exp_iso"] = datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
        out["expired"] = time.time() > int(exp)
    for k in ("user_id", "userId", "uid", "sub"):
        if data.get(k):
            out["user_id"] = str(data[k])
            break
    if data.get("email"):
        out["email"] = data["email"]
    return out


_PROFILE_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "email": ("email",),
    "name": ("name", "firstName", "first_name", "displayName", "display_name"),
    "username": ("username", "userName", "nickname"),
    "user_id": ("id", "userId", "user_id", "uid"),
    "country": ("country", "countryCode", "country_code"),
    "plan": ("subscription_type", "subscription", "plan", "userType", "user_type"),
    "premium": ("isPremium", "is_premium", "premium"),
    "premium_plus": ("isPremiumPlus", "is_premium_plus", "premiumPlus"),
    "renewal_date": ("subscription_renewal", "subscriptionRenewal", "renewal_date", "renewalDate", "expires_at", "expiresAt"),
    "registered_at": ("registered_at", "registeredAt", "createdAt", "created_at"),
}


def _absorb_profile(payload: dict[str, Any], info: dict[str, Any]) -> None:
    """Pull fields we care about out of *payload* into ``info``."""
    # Freepik nests under "data" sometimes; merge both layers.
    layers: list[dict[str, Any]] = [payload]
    for nested_key in ("data", "user", "profile"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            layers.append(nested)

    flat: dict[str, Any] = {}
    for layer in layers:
        for k, v in layer.items():
            if k not in flat:
                flat[k] = v

    for canonical, candidates in _PROFILE_FIELD_MAP.items():
        if canonical in info:
            continue
        for c in candidates:
            v = flat.get(c)
            if v not in (None, "", "N/A"):
                info[canonical] = v
                break

    _absorb_credits(flat, info)


_CREDIT_FIELD_MAP: dict[str, tuple[str, ...]] = {
    "credits": (
        "credits",
        "credits_left",
        "creditsLeft",
        "remaining_credits",
        "remainingCredits",
        "available_credits",
        "availableCredits",
        "balance",
    ),
    "credits_total": (
        "credits_total",
        "creditsTotal",
        "total_credits",
        "totalCredits",
        "credits_quota",
        "quota",
    ),
    "credits_used": (
        "credits_used",
        "creditsUsed",
        "used_credits",
        "usedCredits",
        "consumed",
    ),
    "downloads_left": (
        "downloads_left",
        "downloadsLeft",
        "downloads_remaining",
        "downloadsRemaining",
        "remaining_downloads",
    ),
    "downloads_quota": (
        "downloads_quota",
        "downloadsQuota",
        "max_downloads",
        "maxDownloads",
    ),
}


def _absorb_credits(payload: dict[str, Any], info: dict[str, Any]) -> None:
    # First flatten one level of nesting (Freepik wraps credits under
    # ``data`` / ``credits`` / ``quota`` on different revisions).
    layers: list[dict[str, Any]] = [payload]
    for nested_key in ("data", "credits", "quota", "usage"):
        nested = payload.get(nested_key) if isinstance(payload, dict) else None
        if isinstance(nested, dict):
            layers.append(nested)

    flat: dict[str, Any] = {}
    for layer in layers:
        for k, v in layer.items():
            if k not in flat:
                flat[k] = v

    for canonical, candidates in _CREDIT_FIELD_MAP.items():
        if canonical in info:
            continue
        for c in candidates:
            v = flat.get(c)
            if isinstance(v, (int, float)):
                info[canonical] = int(v) if float(v).is_integer() else v
                break
            if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                info[canonical] = int(v.strip())
                break


def _finalise(info: dict[str, Any]) -> None:
    """Normalise the plan label and the boolean ``is_pro`` flag."""
    plan_raw = info.get("plan")
    if isinstance(plan_raw, str):
        plan = plan_raw.strip()
    else:
        plan = ""

    # Boolean premium flags trump anything else.
    if info.get("premium_plus") is True:
        info["plan"] = "Premium+"
    elif plan:
        low = plan.lower()
        if "premium+" in low or "premium plus" in low or "premiumplus" in low:
            info["plan"] = "Premium+"
        elif "premium" in low or "pro" in low:
            info["plan"] = "Premium"
        elif "education" in low or "edu" in low:
            info["plan"] = "Education"
        elif "team" in low or "enterprise" in low or "business" in low:
            info["plan"] = "Team"
        elif "trial" in low:
            info["plan"] = "Trial"
        elif "free" in low or low == "basic" or low == "regular":
            info["plan"] = "Free"
        else:
            info["plan"] = plan[:40]
    elif info.get("premium") is True:
        info["plan"] = "Premium"
    else:
        info["plan"] = "Free"

    pl = info["plan"].lower()
    info["is_pro"] = (
        info.get("premium") is True
        or info.get("premium_plus") is True
        or "premium" in pl
        or "pro" in pl
        or "team" in pl
        or "education" in pl
    )
