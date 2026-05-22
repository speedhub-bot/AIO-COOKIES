"""perplexity.ai adapter.

Auth model: Next-auth (database sessions) with cookie
``__Secure-next-auth.session-token``. The web app is at
``www.perplexity.ai`` so we set ``HOST = www.perplexity.ai`` —
cookies set on ``.perplexity.ai`` (cf_clearance, edge-vid, etc.)
still match and get forwarded.

Endpoints we use (verified against a real session):

    GET /api/auth/session  -> {user: {email, id, username, ...}, expires}
    GET /api/user          -> {email, id, payment_tier, subscription_status,
                                subscription_tier, subscription_source,
                                username, is_in_organization, ...}

The ``/rest/*`` endpoints are tightly bound to the browser's cf_clearance
fingerprint and return Cloudflare 403 challenges from non-browser clients
even with the user's cookies. The two endpoints above are enough to
report plan + email + identifiers reliably.
"""

from __future__ import annotations

from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class PerplexityAdapter(SiteAdapter):
    SITE = "perplexity.ai"
    HOST = "www.perplexity.ai"
    BASE_URL = "https://www.perplexity.ai"
    KNOWN_COOKIES = (
        "__Secure-next-auth.session-token",
        "next-auth.session-token",
    )

    PROBE_ENDPOINTS: tuple[str, ...] = (
        "/api/auth/session",
        "/api/user",
    )

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        warning = self.cookies_warning()
        if warning:
            result.error = warning
            result.is_dead = True
            return result

        with self.make_client(extra_headers=self.common_headers()) as http:
            # 1) Alive check via /api/auth/session.
            url = self.BASE_URL + "/api/auth/session"
            r = http.get(url)
            result.endpoints_tried.append({"url": url, "status": r.status_code, "len": len(r.text)})
            if r.status_code in (401, 403):
                result.error = (
                    f"/api/auth/session returned HTTP {r.status_code} "
                    "(cookie unauthorized)"
                )
                result.is_dead = True
                return result
            data = self.try_json(r) or {}
            user = data.get("user") if isinstance(data, dict) else None
            if not user:
                result.error = "/api/auth/session returned no user (cookie expired or wrong jar)"
                result.is_dead = True
                return result

            result.alive = True
            if user.get("email"):
                result.info["email"] = user["email"]
            if user.get("id"):
                result.info["id"] = user["id"]
            if user.get("username"):
                result.info["username"] = user["username"]
            if user.get("org_role") and user["org_role"] != "none":
                result.info["org_role"] = user["org_role"]
            if user.get("org_uuid") and user["org_uuid"] != "none":
                result.info["org_uuid"] = user["org_uuid"]
            if data.get("expires"):
                result.info["session_expires"] = data["expires"]

            # 2) /api/user has the full plan + subscription state.
            url = self.BASE_URL + "/api/user"
            r = http.get(url)
            result.endpoints_tried.append({"url": url, "status": r.status_code, "len": len(r.text)})
            payload = self.try_json(r)
            if isinstance(payload, dict):
                for k in (
                    "email",
                    "id",
                    "username",
                    "payment_tier",
                    "subscription_source",
                    "subscription_status",
                    "subscription_tier",
                    "is_in_organization",
                ):
                    if payload.get(k) is not None and result.info.get(k) is None:
                        result.info[k] = payload[k]

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


def _finalise(info: dict[str, Any]) -> None:
    """Surface a friendly ``plan`` / ``is_pro`` summary."""
    # plan: prefer subscription_tier > payment_tier > subscription_status
    plan_raw: str | None = None
    for k in ("subscription_tier", "payment_tier", "subscription_status"):
        v = info.get(k)
        if v and isinstance(v, str):
            plan_raw = v
            break

    if plan_raw:
        info["plan"] = plan_raw if plan_raw != "none" else "free"
    else:
        info["plan"] = "free"

    pro_markers = {"pro", "premium", "max", "enterprise", "active", "team"}
    pl = info["plan"].lower()
    info["is_pro"] = (
        pl in pro_markers
        or "pro" in pl
        or "max" in pl
        or "premium" in pl
        or info.get("subscription_status") in pro_markers
    )
