"""netflix.com adapter.

Auth model: long-lived session cookies (``NetflixId`` + ``SecureNetflixId``)
set on ``.netflix.com``. The membership page at
``www.netflix.com/account/membership`` returns a fully-rendered React
HTML containing a JSON blob (``netflix.reactContext``) with the user's
plan, billing details, country, registration date, member id, and email.

Strategy:
    GET /account/membership
        - 302 to /login         → cookie dead
        - 200 + reactContext    → alive; parse plan / email / country / billing
    GET /YourAccount            → secondary HTML, used to fill gaps
"""

from __future__ import annotations

import re
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class NetflixAdapter(SiteAdapter):
    SITE = "netflix.com"
    HOST = "www.netflix.com"
    BASE_URL = "https://www.netflix.com"
    KNOWN_COOKIES = (
        "NetflixId",
        "SecureNetflixId",
    )

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        warning = self.cookies_warning()
        if warning:
            result.error = warning
            result.is_dead = True
            return result

        headers = {
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept-Encoding": "identity",
        }
        with self.make_client(extra_headers=headers) as http:
            # 1) /account/membership — primary page. Netflix sometimes
            #    rewrites a still-authenticated request to /account
            #    (the new umbrella settings page) so we follow up to
            #    two redirects on netflix.com before giving up.
            url = self.BASE_URL + "/account/membership"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )

            hops = 0
            while r.status_code in (301, 302, 303, 307, 308) and hops < 2:
                location = r.headers.get("location") or r.headers.get("Location") or ""
                if not location:
                    break
                if "/login" in location.lower() or "/loginhelp" in location.lower():
                    result.error = (
                        f"membership page redirected to login ({location})"
                    )
                    result.is_dead = True
                    return result
                if location.startswith("/"):
                    location = self.BASE_URL + location
                if "netflix.com" not in location.lower():
                    result.error = (
                        f"membership page redirected off-domain ({location})"
                    )
                    return result
                r = http.get(location)
                result.endpoints_tried.append(
                    {"url": location, "status": r.status_code, "len": len(r.text)}
                )
                hops += 1

            if r.status_code != 200:
                result.error = (
                    f"membership page returned HTTP {r.status_code} "
                    f"after {hops} hop(s)"
                )
                return result

            text = r.text or ""
            if "/login" in text and "isLoggedIn" not in text and "reactContext" not in text:
                # Some accounts render a tiny redirect shim.
                result.error = "membership page rendered login shim (cookie dead)"
                result.is_dead = True
                return result

            _absorb_membership(text, result.info)
            if not (result.info.get("plan") or result.info.get("email")):
                # If we got HTTP 200 but extracted nothing, treat as dead.
                result.error = "membership page returned no account data"
                result.is_dead = True
                return result
            result.alive = True

            # 2) YourAccount — fills profile names + member_since gaps.
            url = self.BASE_URL + "/YourAccount"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            if r.status_code == 200 and r.text:
                _absorb_membership(r.text, result.info)

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "email": (
        re.compile(r'"emailAddress"\s*:\s*"([^"]+)"'),
        re.compile(r'"email"\s*:\s*"([^"\\@]+@[^"\\]+)"'),
    ),
    "plan": (
        re.compile(r'"localizedPlanName"\s*:\s*"([^"]+)"'),
        re.compile(r'"planName"\s*:\s*"([^"]+)"'),
        re.compile(r'"membershipPlan"\s*:\s*"([^"]+)"'),
    ),
    "country": (
        re.compile(r'"countryOfSignup"\s*:\s*"([A-Za-z-]+)"'),
        re.compile(r'"currentCountry"\s*:\s*"([A-Za-z-]+)"'),
    ),
    "max_streams": (
        re.compile(r'"maxStreams"\s*:\s*(\d+)'),
        re.compile(r'"maximumNumberOfStreams"\s*:\s*(\d+)'),
    ),
    "renewal": (
        re.compile(r'"nextBillingDate"\s*:\s*"([^"]+)"'),
        re.compile(r'"nextBillingDateFormatted"\s*:\s*"([^"]+)"'),
    ),
    "member_since": (
        re.compile(r'"memberSince"\s*:\s*"([^"]+)"'),
        re.compile(r'"membershipStartDate"\s*:\s*"([^"]+)"'),
    ),
    "payment_method": (
        re.compile(r'"paymentMethod"\s*:\s*"([^"]+)"'),
        re.compile(r'"paymentInstrumentType"\s*:\s*"([^"]+)"'),
    ),
    "quality": (
        re.compile(r'"videoQuality"\s*:\s*"([^"]+)"'),
        re.compile(r'"streamingQuality"\s*:\s*"([^"]+)"'),
    ),
    "phone": (
        re.compile(r'"phoneNumber"\s*:\s*"(\+?[\d\-\s]+)"'),
    ),
    "user_guid": (
        re.compile(r'"userGuid"\s*:\s*"([A-Z0-9]+)"'),
        re.compile(r'"netflixId"\s*:\s*"([A-Z0-9-]+)"'),
    ),
    "membership_status": (
        re.compile(r'"membershipStatus"\s*:\s*"([^"]+)"'),
    ),
    "hold_status": (
        re.compile(r'"onHold"\s*:\s*(true|false)', re.IGNORECASE),
    ),
    "email_verified": (
        re.compile(r'"emailVerified"\s*:\s*(true|false)', re.IGNORECASE),
    ),
    "extra_members": (
        re.compile(r'"extraMembersEnabled"\s*:\s*(true|false)', re.IGNORECASE),
    ),
}

_PROFILE_PATTERN = re.compile(r'"profileName"\s*:\s*"([^"]+)"')


def _absorb_membership(html: str, info: dict[str, Any]) -> None:
    for key, patterns in _PATTERNS.items():
        if info.get(key):
            continue
        for pat in patterns:
            m = pat.search(html)
            if m:
                info[key] = m.group(1)
                break

    profiles = _PROFILE_PATTERN.findall(html)
    if profiles and "profiles" not in info:
        # Dedup but preserve order.
        seen = []
        for p in profiles:
            if p not in seen:
                seen.append(p)
        info["profiles"] = seen


def _finalise(info: dict[str, Any]) -> None:
    plan = (info.get("plan") or "").strip()
    if not plan:
        info["plan"] = info.get("membership_status") or "Unknown"
    info["is_pro"] = bool(plan) and "free" not in plan.lower()
