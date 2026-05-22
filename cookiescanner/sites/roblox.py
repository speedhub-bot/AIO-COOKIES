"""roblox.com adapter.

Auth model: single session cookie ``.ROBLOSECURITY`` set on
``.roblox.com``. Roblox ships a clean public web-API which only needs
that cookie (plus a CSRF token, which is only required for POSTs).

Roblox actively *rotates* ``.ROBLOSECURITY`` server-side whenever it
sees the same token used from an IP that doesn't match the one the
token was minted on — which is exactly what happens when the bot
imports a browser-exported cookie. To keep accounts alive across
scans, we proactively run the public ``authentication-ticket`` flow
to swap the cookie for a fresh one *before* doing any read-only
probes. The fresh cookie is captured in
``ScanResult.refreshed_cookies`` so callers (the TG bot) can hand it
back to the user.

Endpoints we use:

    POST https://auth.roblox.com/v1/authentication-ticket
        -> 403 + ``x-csrf-token`` header on the first call,
           then 200 + ``rbx-authentication-ticket`` header on the retry.

    POST https://auth.roblox.com/v1/authentication-ticket/redeem
        -> 200 + ``Set-Cookie: .ROBLOSECURITY=<fresh value>``.
           Requires the ``RBXAuthenticationNegotiation: 1`` header.

    GET https://users.roblox.com/v1/users/authenticated
        -> {id, name, displayName}  — primary alive check

    GET https://accountinformation.roblox.com/v1/email
        -> {email, verified, emailAddress}

    GET https://accountinformation.roblox.com/v1/phone
        -> {countryCode, prefix, phone, isVerified}

    GET https://accountinformation.roblox.com/v1/birthdate
        -> {birthMonth, birthDay, birthYear}

    GET https://economy.roblox.com/v1/users/{id}/currency
        -> {robux}

    GET https://premiumfeatures.roblox.com/v1/users/{id}/validate-membership
        -> raw boolean ``true`` / ``false``

    GET https://accountsettings.roblox.com/v1/email
        -> {verified, emailAddress}   (used as a fallback)
"""

from __future__ import annotations

from typing import Any

from ..http import HttpClient
from ..types import ScanResult
from .base import SiteAdapter


class RobloxAdapter(SiteAdapter):
    SITE = "roblox.com"
    HOST = "www.roblox.com"
    BASE_URL = "https://www.roblox.com"
    KNOWN_COOKIES = (".ROBLOSECURITY",)

    AUTH_API = "https://auth.roblox.com"
    USERS_API = "https://users.roblox.com"
    ACCOUNT_INFO_API = "https://accountinformation.roblox.com"
    ACCOUNT_SETTINGS_API = "https://accountsettings.roblox.com"
    ECONOMY_API = "https://economy.roblox.com"
    PREMIUM_API = "https://premiumfeatures.roblox.com"

    # Cookies live on ``.roblox.com``; explicitly merge across subdomains
    # so the api.* hosts get the cookie too.
    SUBDOMAIN_HOSTS = (
        "www.roblox.com",
        "auth.roblox.com",
        "users.roblox.com",
        "accountinformation.roblox.com",
        "accountsettings.roblox.com",
        "economy.roblox.com",
        "premiumfeatures.roblox.com",
        ".roblox.com",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for host in self.SUBDOMAIN_HOSTS:
            merged.update(self.jar.for_host(host))
        return merged

    # ----- ``.ROBLOSECURITY`` refresh flow ------------------------------

    def _refresh_roblosecurity(self, http: HttpClient, result: ScanResult) -> str | None:
        """Rotate ``.ROBLOSECURITY`` via the public authentication-ticket flow.

        Returns the fresh cookie value on success, ``None`` if the cookie
        was already dead (the flow then surfaces a clear error in
        ``result.error``), or ``None`` with no error if the refresh
        endpoint simply didn't behave as expected — callers should fall
        back to the regular read-only alive check in that case.

        Side effect: on success, the new ``.ROBLOSECURITY`` is added to
        ``result.refreshed_cookies`` and the underlying session jar is
        already updated by curl_cffi.
        """
        ticket_url = self.AUTH_API + "/v1/authentication-ticket"
        redeem_url = self.AUTH_API + "/v1/authentication-ticket/redeem"
        ticket_headers = {
            "Referer": "https://www.roblox.com/my/account",
            "Origin": "https://www.roblox.com",
            "Content-Type": "application/x-www-form-urlencoded",
        }

        # First POST without CSRF: expected to 403 and return ``x-csrf-token``.
        r = http.post(ticket_url, headers=ticket_headers, data="")
        result.endpoints_tried.append(
            {"url": ticket_url, "status": r.status_code, "len": len(r.text or "")}
        )
        if r.status_code == 401:
            result.error = "/v1/authentication-ticket returned 401 (cookie dead)"
            return None
        csrf = r.headers.get("x-csrf-token") or r.headers.get("X-CSRF-TOKEN")
        if not csrf:
            # Endpoint didn't hand back a token — can't proceed. Fall back
            # to the read-only alive check; not a fatal error.
            return None

        # Retry with the CSRF token; expect 200 and an ``rbx-authentication-ticket`` header.
        r2 = http.post(
            ticket_url,
            headers={**ticket_headers, "X-CSRF-TOKEN": csrf},
            data="",
        )
        result.endpoints_tried.append(
            {"url": ticket_url, "status": r2.status_code, "len": len(r2.text or "")}
        )
        if r2.status_code == 401:
            result.error = "/v1/authentication-ticket returned 401 (cookie dead)"
            return None
        if r2.status_code != 200:
            return None
        ticket = (
            r2.headers.get("rbx-authentication-ticket")
            or r2.headers.get("Rbx-Authentication-Ticket")
        )
        if not ticket:
            return None

        # Redeem the ticket — response carries the fresh ``.ROBLOSECURITY``.
        old_value = http.session_cookies().get(".ROBLOSECURITY")
        r3 = http.post(
            redeem_url,
            headers={
                **ticket_headers,
                "RBXAuthenticationNegotiation": "1",
            },
            data=f"authenticationTicket={ticket}",
        )
        result.endpoints_tried.append(
            {"url": redeem_url, "status": r3.status_code, "len": len(r3.text or "")}
        )
        if r3.status_code != 200:
            return None
        new_value = http.session_cookies().get(".ROBLOSECURITY")
        if not new_value or new_value == old_value:
            return None
        result.refreshed_cookies[".ROBLOSECURITY"] = new_value
        result.info["cookie_refreshed"] = True
        return new_value

    # ----- main scan ----------------------------------------------------

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        warning = self.cookies_warning()
        if warning:
            result.error = warning
            result.is_dead = True
            return result

        headers = self.common_headers()
        with self.make_client(extra_headers=headers) as http:
            # 0) Rotate ``.ROBLOSECURITY`` first. The act of redeeming a
            # fresh ticket also confirms the cookie is alive, so a
            # successful refresh doubles as the alive check — but we
            # still call ``/v1/users/authenticated`` afterwards to fetch
            # ``id`` / ``name`` / ``displayName``.
            self._refresh_roblosecurity(http, result)
            if result.error and "cookie dead" in result.error:
                result.is_dead = True
                return result

            # 1) Authenticated user — alive check (now using the fresh cookie).
            url = self.USERS_API + "/v1/users/authenticated"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            if r.status_code == 401:
                result.error = "/v1/users/authenticated returned 401 (cookie dead)"
                result.is_dead = True
                return result
            if r.status_code != 200:
                result.error = f"/v1/users/authenticated returned HTTP {r.status_code}"
                if r.status_code == 403:
                    result.is_dead = True
                return result
            data = self.try_json(r)
            if not isinstance(data, dict) or not data.get("id"):
                result.error = "authenticated endpoint returned no user id"
                result.is_dead = True
                return result

            user_id = data.get("id")
            result.alive = True
            result.info["user_id"] = user_id
            if data.get("name"):
                result.info["username"] = data["name"]
            if data.get("displayName"):
                result.info["name"] = data["displayName"]

            # 2) Email + verification.
            for url in (
                self.ACCOUNT_INFO_API + "/v1/email",
                self.ACCOUNT_SETTINGS_API + "/v1/email",
            ):
                r = http.get(url)
                result.endpoints_tried.append(
                    {"url": url, "status": r.status_code, "len": len(r.text)}
                )
                payload = self.try_json(r)
                if isinstance(payload, dict):
                    email = payload.get("emailAddress") or payload.get("email")
                    if email and not result.info.get("email"):
                        result.info["email"] = email
                    if payload.get("verified") is not None and result.info.get("email_verified") is None:
                        result.info["email_verified"] = bool(payload["verified"])

            # 3) Phone number.
            url = self.ACCOUNT_INFO_API + "/v1/phone"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            phone_payload = self.try_json(r)
            if isinstance(phone_payload, dict) and phone_payload.get("phone"):
                cc = phone_payload.get("countryCode") or ""
                prefix = phone_payload.get("prefix") or ""
                phone = phone_payload.get("phone")
                result.info["phone"] = f"{cc}{prefix}{phone}".strip()
                if phone_payload.get("isVerified") is not None:
                    result.info["phone_verified"] = bool(phone_payload["isVerified"])

            # 4) Birthdate.
            url = self.ACCOUNT_INFO_API + "/v1/birthdate"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            bd = self.try_json(r)
            if isinstance(bd, dict) and bd.get("birthYear"):
                year = bd["birthYear"]
                month = bd.get("birthMonth") or 1
                day = bd.get("birthDay") or 1
                result.info["birthdate"] = f"{year:04d}-{int(month):02d}-{int(day):02d}"

            # 5) Robux balance.
            url = f"{self.ECONOMY_API}/v1/users/{user_id}/currency"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            cur = self.try_json(r)
            if isinstance(cur, dict) and cur.get("robux") is not None:
                result.info["robux"] = cur["robux"]

            # 6) Premium membership.
            url = f"{self.PREMIUM_API}/v1/users/{user_id}/validate-membership"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            try:
                is_premium = r.text.strip().lower() == "true"
            except Exception:  # noqa: BLE001
                is_premium = False
            result.info["is_premium"] = is_premium

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


def _finalise(info: dict[str, Any]) -> None:
    if info.get("is_premium"):
        info["plan"] = "Premium"
        info["is_pro"] = True
    else:
        info["plan"] = "Free"
        info["is_pro"] = False
