"""manus.im adapter.

Manus's web app at ``manus.im`` calls a private API at ``api.manus.im``
through an APISIX edge that returns generic 503s on undocumented paths
and rejects clients lacking the ``x-client-type: web`` header.

Auth lives in the ``session_id`` cookie (sometimes
``__Secure-session_id``). That cookie value **is** a JWT — Manus signs
it themselves and the payload has the basics:

    {
      "email": "...",
      "name": "...",
      "user_id": "...",
      "team_uid": "..." | null,
      "type": "user",
      "iat": <issued_at>,
      "exp": <expiry>
    }

Because the JWT alone never carries plan / credit data, the adapter
runs three layers:

    1. **JWT decode** — sanity-check the payload and read off
       email / user_id / team_uid / exp. An expired ``exp`` is a
       definitive dead-cookie signal (``is_dead=True``).

    2. **Server-side alive check** — POST to a known-good API gateway
       path with the cookie. ``api.manus.im`` returns:
         * 401 / 403            → cookie revoked → ``is_dead=True``
         * 200 / 4xx (non-auth) → cookie still accepted by APISIX

    3. **Plan probe** — best-effort GET against a list of subscription
       / membership / billing endpoints. Whichever one returns JSON
       gets harvested for plan / credit / renewal fields. The list is
       deliberately broad because Manus rotates these paths often.

If layer 3 finds nothing, we still expose a useful plan inference
from the JWT: a ``team_uid`` means the cookie belongs to a paid Team
seat, otherwise we fall back to "Personal" rather than the unhelpful
"unknown" the adapter used to print.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class ManusAdapter(SiteAdapter):
    SITE = "manus.im"
    HOST = "manus.im"
    BASE_URL = "https://manus.im"
    API_BASE = "https://api.manus.im"
    KNOWN_COOKIES = (
        "session_id",
        "__Secure-session_id",
    )

    # Required custom headers — without these APISIX returns 503.
    EXTRA_HEADERS = {
        "x-client-type": "web",
        "x-client-locale": "en",
        "x-client-timezone": "UTC",
        "x-client-version": "web",
    }

    # Authoritative alive-check paths. We hit these *first* — a 401/403
    # response from any of them is a definitive dead-cookie signal,
    # regardless of whether the JWT looks valid locally.
    ALIVE_PATHS: tuple[str, ...] = (
        "/api/user/info",
        "/api/user/get_info",
        "/api/user/profile",
    )

    # Candidate paths probed for subscription / plan / credit data.
    # Manus rotates these frequently — keep the list broad. Anything
    # that returns parseable JSON gets harvested via ``_harvest``.
    PROBE_PATHS: tuple[str, ...] = (
        "/api/user/info",
        "/api/user/get_info",
        "/api/user/profile",
        "/api/user/me",
        "/api/users/me",
        "/api/me",
        "/api/account/info",
        "/api/account/profile",
        "/api/subscription/info",
        "/api/subscription/current",
        "/api/subscription/get",
        "/api/billing/info",
        "/api/billing/subscription",
        "/api/credit/balance",
        "/api/credit/get",
        "/api/credits/get",
        "/api/credits/balance",
        "/api/membership/info",
        "/api/membership/current",
        "/api/team/info",
        "/api/v1/user/info",
        "/api/v1/user/profile",
        "/api/v1/subscription/info",
        "/api/v1/credit/balance",
    )

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        host_cookies = self.host_cookies()
        token = (
            host_cookies.get("session_id")
            or host_cookies.get("__Secure-session_id")
        )
        if not token:
            # No auth cookie at all — definitively dead.
            result.is_dead = True
            result.error = (
                f"No expected cookies for {self.SITE} were found "
                f"(looked for: {', '.join(self.KNOWN_COOKIES)})."
            )
            return result

        # 1) Decode the JWT payload locally. Manus's exp claim is
        #    enforced server-side too, so an expired exp is final.
        claims = _decode_jwt_payload(token)
        if not isinstance(claims, dict):
            # Not a JWT we can decode — could still be a valid opaque
            # session token. Don't mark dead; fall through to server check.
            claims = {}

        exp = claims.get("exp")
        if isinstance(exp, (int, float)) and exp < time.time():
            iso = datetime.fromtimestamp(int(exp), tz=timezone.utc).isoformat()
            result.is_dead = True
            result.error = f"session_id JWT expired at {iso}"
            return result

        # Surface what we know from the JWT regardless of API outcome.
        for src, dst in (
            ("email", "email"),
            ("name", "name"),
            ("user_id", "user_id"),
            ("team_uid", "team_uid"),
            ("type", "user_type"),
        ):
            v = claims.get(src)
            if v:
                result.info[dst] = v
        if isinstance(exp, (int, float)):
            result.info["session_expires"] = datetime.fromtimestamp(
                int(exp), tz=timezone.utc
            ).isoformat()
        iat = claims.get("iat")
        if isinstance(iat, (int, float)):
            result.info["session_issued"] = datetime.fromtimestamp(
                int(iat), tz=timezone.utc
            ).isoformat()

        headers = {
            **self.common_headers(),
            **self.EXTRA_HEADERS,
            "Origin": "https://manus.im",
            "Referer": "https://manus.im/app",
        }

        # 2) Server-side alive check. We need at least one signal that
        #    isn't 401/403 to call the cookie alive. A 401/403 anywhere
        #    is a definitive dead verdict.
        with self.make_client(extra_headers=headers) as http:
            authed_signal = False  # any non-auth-failing response
            saw_auth_failure = False  # explicit 401/403

            for path in self.ALIVE_PATHS:
                url = self.API_BASE + path
                try:
                    resp = http.get(url)
                except Exception as e:
                    result.endpoints_tried.append(
                        {"url": url, "status": "ERR",
                         "error": f"{type(e).__name__}: {e}"}
                    )
                    continue
                status = resp.status_code
                result.endpoints_tried.append(
                    {"url": url, "status": status, "len": len(resp.text)}
                )
                if status in (401, 403):
                    saw_auth_failure = True
                    continue
                if 200 <= status < 500:
                    authed_signal = True
                    payload = self.try_json(resp)
                    if isinstance(payload, dict) and _looks_like_data(payload):
                        _harvest(payload, result.info)
                # 5xx / network errors don't tell us anything authoritative

            if saw_auth_failure and not authed_signal:
                # APISIX gateway told us the cookie is unauthenticated.
                result.is_dead = True
                result.error = (
                    "api.manus.im returned 401/403 for the session_id "
                    "cookie (revoked or expired server-side)"
                )
                return result

            # If the JWT decoded cleanly *and* APISIX accepted the cookie
            # at least once, treat the session as alive. If neither
            # signal is there, fall back to the JWT alone — a
            # locally-valid JWT with no auth-failure response is the
            # weakest "alive" we can return.
            if authed_signal or claims:
                result.alive = True
            else:
                # No JWT, no API acceptance — call it dead.
                result.is_dead = True
                result.error = "session_id is not a JWT and api.manus.im did not accept it"
                return result

            # 3) Plan / credit probes — best-effort, harvest anything useful.
            for path in self.PROBE_PATHS:
                url = self.API_BASE + path
                try:
                    resp = http.get(url)
                except Exception as e:
                    result.endpoints_tried.append(
                        {"url": url, "status": "ERR",
                         "error": f"{type(e).__name__}: {e}"}
                    )
                    continue
                entry: dict[str, Any] = {
                    "url": url,
                    "status": resp.status_code,
                    "len": len(resp.text),
                }
                if resp.status_code in (401, 403):
                    # Should not happen after a successful alive check,
                    # but if it does, downgrade — don't overwrite alive.
                    result.endpoints_tried.append(entry)
                    continue
                payload = self.try_json(resp)
                if isinstance(payload, dict):
                    # APISIX 404 envelope.
                    status_msg = str(payload.get("status") or "").lower()
                    if status_msg in {"not found", "404"} and "data" not in payload:
                        entry["note"] = "endpoint not found"
                    else:
                        entry["json_keys"] = sorted(list(payload.keys()))[:25]
                        _harvest(payload, result.info)
                result.endpoints_tried.append(entry)

        _finalise(result.info, claims)
        return result


# ----- JWT decode ------------------------------------------------------


def _b64decode_url(seg: str) -> bytes:
    pad = "=" * (-len(seg) % 4)
    return base64.urlsafe_b64decode(seg + pad)


def _decode_jwt_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT's payload segment without verifying the signature."""
    parts = token.split(".")
    if len(parts) < 2:
        return None
    try:
        raw = _b64decode_url(parts[1])
        return json.loads(raw)
    except Exception:
        return None


# ----- harvest + finalise ---------------------------------------------


# Keys we slurp out of any JSON the API returns. Wider than the old
# list because Manus's actual response shapes have shifted (current
# subscription endpoints answer with ``planType`` / ``planLevel`` /
# ``creditBalance`` / ``periodEnd`` rather than the snake_case names).
_KEYS = {
    # plan / tier
    "plan",
    "plan_name",
    "planName",
    "planType",
    "plan_type",
    "planLevel",
    "plan_level",
    "tier",
    "subscription",
    "subscription_status",
    "subscriptionStatus",
    "subscription_type",
    "subscriptionType",
    "subscription_plan",
    "subscriptionPlan",
    "membership",
    "membership_type",
    "membershipType",
    "membership_level",
    "membershipLevel",
    "isPro",
    "is_pro",
    "is_premium",
    "isPremium",
    "isTeam",
    "is_team",
    "isPaid",
    "is_paid",
    "isFree",
    "is_free",
    # credits / balance
    "credits",
    "credit",
    "credit_balance",
    "creditBalance",
    "credits_balance",
    "creditsBalance",
    "remaining_credits",
    "remainingCredits",
    "monthly_credits",
    "monthlyCredits",
    "monthly_credit",
    "monthlyCredit",
    "free_credits",
    "freeCredits",
    "addon_credits",
    "addonCredits",
    "total_credits",
    "totalCredits",
    "available_credits",
    "availableCredits",
    "used_credits",
    "usedCredits",
    "balance",
    # billing / dates
    "renewal_date",
    "renewalDate",
    "renews_at",
    "renewsAt",
    "current_period_end",
    "currentPeriodEnd",
    "period_end",
    "periodEnd",
    "expires_at",
    "expiresAt",
    "expire_at",
    "expireAt",
    "cancel_at",
    "canceled_at",
    "cancelAt",
    "auto_renew",
    "autoRenew",
    # account
    "email",
    "name",
    "username",
    "user_id",
    "userId",
    "team_uid",
    "teamUid",
}


def _looks_like_data(payload: dict[str, Any]) -> bool:
    """Heuristic: does this JSON look like a real account-data response?

    APISIX wraps "endpoint not found" as ``{"status": "Not Found", "code": 404}``,
    which we want to ignore. Any payload with at least one key we
    recognise (or a ``data`` envelope) is treated as real data.
    """
    if not isinstance(payload, dict):
        return False
    status_msg = str(payload.get("status") or "").lower()
    if status_msg in {"not found", "404", "unauthorized", "401"}:
        return False
    # Typical Manus envelopes are ``{code, msg, data}`` or ``{result}``.
    if "data" in payload or "result" in payload:
        return True
    return any(k in _KEYS for k in payload.keys())


def _harvest(payload: dict[str, Any], out: dict[str, Any]) -> None:
    def visit(node: Any) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in _KEYS and out.get(k) in (None, "", [], {}):
                    if isinstance(v, (str, int, float, bool)):
                        out[k] = v
                visit(v)
        elif isinstance(node, list):
            for item in node:
                visit(item)

    visit(payload)


def _finalise(info: dict[str, Any], claims: dict[str, Any]) -> None:
    """Promote a friendly ``plan`` / ``is_pro`` summary."""

    # Pick the strongest plan signal available across the various
    # response shapes Manus returns.
    if "plan" not in info:
        for k in (
            "plan_name", "planName",
            "plan_type", "planType",
            "plan_level", "planLevel",
            "subscription_plan", "subscriptionPlan",
            "subscription_type", "subscriptionType",
            "tier",
            "membership_type", "membershipType",
            "membership_level", "membershipLevel",
            "membership",
            "subscription_status", "subscriptionStatus",
        ):
            if info.get(k):
                info["plan"] = info[k]
                break

    # Boolean flags (any truthy is_team / is_pro / is_premium / is_paid).
    is_team = any(
        bool(info.get(k))
        for k in ("isTeam", "is_team")
    ) or bool(info.get("team_uid") or claims.get("team_uid"))
    is_paid_flag = any(
        bool(info.get(k))
        for k in ("isPro", "is_pro", "isPremium", "is_premium",
                  "isPaid", "is_paid")
    )

    plan_str = str(info.get("plan") or "").lower()
    if not info.get("plan"):
        # No API-reported plan. Best inference from the JWT claims:
        # a ``team_uid`` means this seat belongs to a Team workspace,
        # otherwise default to "Personal" (Manus's free / pro tier
        # split is invisible without API data).
        if is_team:
            info["plan"] = "Team"
        elif is_paid_flag:
            info["plan"] = "Pro"
        else:
            info["plan"] = "Personal"
        plan_str = info["plan"].lower()

    pro_markers = {
        "pro", "plus", "premium", "team", "starter", "max", "ultra",
        "enterprise", "active", "paid",
    }
    info["is_pro"] = (
        plan_str in pro_markers
        or any(m in plan_str for m in pro_markers)
        or is_team
        or is_paid_flag
    )
    info["is_team"] = is_team

    # Pick a renewal date from whichever field surfaced.
    if "renewal" not in info:
        for k in (
            "renewal_date", "renewalDate",
            "renews_at", "renewsAt",
            "current_period_end", "currentPeriodEnd",
            "period_end", "periodEnd",
            "expires_at", "expiresAt",
            "expire_at", "expireAt",
        ):
            if info.get(k):
                info["renewal"] = info[k]
                break

    # Normalise credit balance into ``credits``.
    if "credits" not in info:
        for k in (
            "credit_balance", "creditBalance",
            "credits_balance", "creditsBalance",
            "remaining_credits", "remainingCredits",
            "available_credits", "availableCredits",
            "balance",
            "credit",
        ):
            if info.get(k) is not None:
                info["credits"] = info[k]
                break
