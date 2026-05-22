"""blackbox.ai adapter.

Auth model: Next-auth (JWT in cookie ``next-auth.session-token``).
The web app lives on ``app.blackbox.ai``; the auth cookie is set on
``.blackbox.ai`` so it matches every subdomain.

Endpoints we use (all verified live against a real session):

    GET  /api/auth/session       -> {user:{email,id}, expires}
    GET  /api/account/current    -> {currentAccount, userEmail,
                                      availableAccounts[],
                                      isUsingOwnAccount}
    POST /api/check-subscription {email}
                                 -> {hasActiveSubscription, customerId,
                                      expiryTimestamp, isTeam, numSeats,
                                      previouslySubscribed, provider,
                                      activeInsuffientCredits, isTrialSubscription}
    GET  /api/v0/credits / /api/credits/get  (best-effort for credit balance)

``/api/auth/session`` returning ``{user: null}`` (or a JSON-encoded
``null``) means the cookie is dead.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class BlackboxAdapter(SiteAdapter):
    SITE = "blackbox.ai"
    # Use the *real* request host so per-host cookies (sessionId, csrf, etc.)
    # are also forwarded — cookies set on ``.blackbox.ai`` still match.
    HOST = "app.blackbox.ai"
    BASE_URL = "https://app.blackbox.ai"
    KNOWN_COOKIES = (
        "next-auth.session-token",
        "__Secure-next-auth.session-token",
        "__Secure-authjs.session-token",
        "sessionId",
    )

    # Real-world Blackbox exports often live on ``www.blackbox.ai``
    # (sessionId) or ``.www.blackbox.ai`` (intercom/stripe). Suffix
    # matching against the request host ``app.blackbox.ai`` would miss
    # those, so we explicitly enumerate every plausible host here.
    SUBDOMAIN_HOSTS = (
        "app.blackbox.ai",
        "www.blackbox.ai",
        "blackbox.ai",
        ".blackbox.ai",
        ".www.blackbox.ai",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for h in self.SUBDOMAIN_HOSTS:
            merged.update(self.jar.for_host(h))
        return merged

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        warning = self.cookies_warning()
        if warning:
            result.error = warning
            result.is_dead = True
            return result

        headers = {
            **self.common_headers(),
            "Content-Type": "application/json",
        }
        with self.make_client(extra_headers=headers) as http:
            # 1) Session = alive check.
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
            data = self.try_json(r)
            user = data.get("user") if isinstance(data, dict) else None
            if not user:
                result.error = "/api/auth/session returned null/no user (cookie dead)"
                result.is_dead = True
                return result

            result.alive = True
            email = user.get("email")
            if email:
                result.info["email"] = email
            if user.get("id"):
                result.info["id"] = user["id"]
            if user.get("name"):
                result.info["name"] = user["name"]
            if isinstance(data, dict) and data.get("expires"):
                result.info["session_expires"] = data["expires"]

            # 2) Account info — confirms email, surfaces team membership.
            url = self.BASE_URL + "/api/account/current"
            r = http.get(url)
            result.endpoints_tried.append({"url": url, "status": r.status_code, "len": len(r.text)})
            payload = self.try_json(r)
            if isinstance(payload, dict):
                if payload.get("userEmail") and not result.info.get("email"):
                    result.info["email"] = payload["userEmail"]
                if payload.get("isUsingOwnAccount") is False and payload.get("currentAccount"):
                    result.info["currentAccount"] = payload["currentAccount"]
                accounts = payload.get("availableAccounts") or []
                if accounts:
                    result.info["availableAccounts"] = accounts

            # 3) Subscription — full plan / renewal info.
            #    Requires POST {email} or returns 400 "Email is required".
            if email:
                url = self.BASE_URL + "/api/check-subscription"
                r = http.post(url, json={"email": email})
                result.endpoints_tried.append({"url": url, "status": r.status_code, "len": len(r.text)})
                sub = self.try_json(r)
                if isinstance(sub, dict):
                    _absorb_subscription(sub, result.info)

            # 4) Credits — best-effort; may 400/500 for free accounts.
            for path in ("/api/v0/credits", "/api/credits/get"):
                url = self.BASE_URL + path
                r = http.get(url)
                result.endpoints_tried.append({"url": url, "status": r.status_code, "len": len(r.text)})
                cdata = self.try_json(r)
                if isinstance(cdata, dict) and "error" not in cdata:
                    for k in ("credits", "remaining", "balance", "monthly_limit", "limit", "used"):
                        if cdata.get(k) is not None and result.info.get(k) is None:
                            result.info[k] = cdata[k]

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


def _absorb_subscription(sub: dict[str, Any], out: dict[str, Any]) -> None:
    """Map Blackbox's /api/check-subscription response into the result."""

    active = bool(sub.get("hasActiveSubscription"))
    trial = bool(sub.get("isTrialSubscription"))
    out["hasActiveSubscription"] = active
    if trial:
        out["isTrialSubscription"] = True
    if sub.get("isTeam"):
        out["isTeam"] = True
        if sub.get("numSeats") is not None:
            out["numSeats"] = sub["numSeats"]
    if sub.get("previouslySubscribed"):
        out["previouslySubscribed"] = True
    if sub.get("customerId"):
        out["stripe_customer_id"] = sub["customerId"]
    if sub.get("provider"):
        out["provider"] = sub["provider"]
    if sub.get("activeInsuffientCredits"):
        out["activeInsuffientCredits"] = True

    expiry = sub.get("expiryTimestamp")
    if expiry:
        # Blackbox returns seconds since epoch.
        try:
            out["renewal"] = (
                datetime.fromtimestamp(int(expiry), tz=timezone.utc).isoformat()
            )
            out["renewal_timestamp"] = int(expiry)
        except Exception:
            out["renewal_timestamp"] = expiry


def _finalise(info: dict[str, Any]) -> None:
    """Promote a friendly ``plan`` / ``is_pro`` summary."""
    if info.get("hasActiveSubscription"):
        info["is_pro"] = True
        if info.get("isTeam"):
            info["plan"] = "team"
        elif info.get("isTrialSubscription"):
            info["plan"] = "trial"
        else:
            info["plan"] = "premium"
    else:
        info["is_pro"] = False
        info["plan"] = "free"
