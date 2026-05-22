"""spotify.com adapter.

Auth model: ``sp_dc`` (+ ``sp_key`` on legacy sessions) on
``.spotify.com``. Trackers (``sp_t``, ``sp_landingref``,
``OptanonAlertBoxClosed`` etc.) are not enough to log in.

Spotify deprecated ``/api/account-settings/v1/profile`` (now 400s
with ``oops_something_went_wrong`` even for valid sessions). Newer
account pages are served via geo-prefixed paths
(``/<lang>/account/...``). The cheapest reliable alive check is
visiting the account overview and observing whether Spotify keeps
you there or 302s you to ``accounts.spotify.com/login``.

Endpoints we use:

    GET https://www.spotify.com/account/overview/
        - 302 to /<lang>/account/overview/    → geo redirect (follow)
        - 302 to accounts.spotify.com/login   → cookie dead

    GET https://www.spotify.com/<lang>/account/overview/
        - 200 + dashboard HTML  → alive (regex pulls email, plan,
                                  next_payment, country, displayName,
                                  invite token)
        - 302 to login          → cookie dead

    GET https://www.spotify.com/<lang>/account/family/home/
        - Best-effort family-plan roster + invite link.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit

from ..types import ScanResult
from .base import SiteAdapter


class SpotifyAdapter(SiteAdapter):
    SITE = "spotify.com"
    HOST = "www.spotify.com"
    BASE_URL = "https://www.spotify.com"
    KNOWN_COOKIES = ("sp_dc", "sp_key")

    SUBDOMAIN_HOSTS = (
        "www.spotify.com",
        "accounts.spotify.com",
        "open.spotify.com",
        ".spotify.com",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for host in self.SUBDOMAIN_HOSTS:
            merged.update(self.jar.for_host(host))
        return merged

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        cookies = self.host_cookies()
        if "sp_dc" not in cookies:
            present = [c for c in ("sp_dc", "sp_key", "sp_t") if c in cookies]
            result.error = (
                "Spotify session requires the sp_dc auth cookie; "
                f"found only {present or 'tracker-only cookies'}."
            )
            result.is_dead = True
            return result

        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        headers = {
            "User-Agent": ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }
        overview_path = "/account/overview/"
        with self.make_client(extra_headers=headers) as http:
            # 1) Hit /account/overview/ and follow the geo redirect.
            url = self.BASE_URL + overview_path
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            location = (
                r.headers.get("location") or r.headers.get("Location") or ""
            )
            if r.status_code in (301, 302, 303, 307, 308):
                if "accounts.spotify.com/login" in location.lower():
                    result.error = (
                        f"/account/overview/ redirected to login "
                        f"({location[:90]}); cookie dead"
                    )
                    result.is_dead = True
                    return result
                # Geo redirect: /<lang>/account/overview/ — follow it.
                if location.startswith("/"):
                    location = self.BASE_URL + location
                if not location:
                    result.error = "/account/overview/ returned empty Location"
                    return result
                r = http.get(location)
                result.endpoints_tried.append(
                    {"url": location, "status": r.status_code, "len": len(r.text)}
                )
                if r.status_code != 200:
                    loc2 = (
                        r.headers.get("location")
                        or r.headers.get("Location")
                        or ""
                    )
                    if "login" in loc2.lower():
                        result.error = (
                            f"geo-prefixed overview also redirected to "
                            f"login ({loc2[:90]}); cookie dead"
                        )
                        result.is_dead = True
                    else:
                        result.error = (
                            f"geo-prefixed overview returned HTTP "
                            f"{r.status_code}"
                        )
                    return result
            elif r.status_code != 200:
                result.error = (
                    f"/account/overview/ returned HTTP {r.status_code}"
                )
                if r.status_code in (401, 403):
                    result.is_dead = True
                return result

            # At this point r is a 200 HTML page from /<lang>/account/overview/.
            result.alive = True
            _absorb_overview_html(r.text, result.info)
            # Remember the lang prefix for follow-up calls.
            lang_prefix = ""
            m = (
                re.match(r"^/([a-z]{2}(?:-[a-z]{2})?)/", urlsplit(location).path)
                if location
                else None
            )
            if m:
                lang_prefix = "/" + m.group(1)

            # 2) Family-plan roster (best-effort).
            plan = (result.info.get("plan") or "").lower()
            if plan.startswith("family") or result.info.get("plan_family"):
                fam_url = self.BASE_URL + lang_prefix + "/account/family/home/"
                fr = http.get(fam_url)
                result.endpoints_tried.append(
                    {"url": fam_url, "status": fr.status_code, "len": len(fr.text)}
                )
                if fr.status_code == 200 and fr.text:
                    _absorb_family_html(fr.text, result.info)

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


_EMAIL_PATTERN = re.compile(
    r'"email"\s*:\s*"([^"\\]+@[^"\\]+)"|data-email="([^"@]+@[^"]+)"'
)
_NAME_PATTERN = re.compile(
    r'"displayName"\s*:\s*"([^"\\]{1,80})"|"firstName"\s*:\s*"([^"\\]{1,80})"'
)
_USERNAME_PATTERN = re.compile(r'"username"\s*:\s*"([A-Za-z0-9._-]{2,80})"')
_LOGGED_IN_PATTERN = re.compile(r'"loggedIn"\s*:\s*true')
_COUNTRY_PATTERN = re.compile(r'"country"\s*:\s*"([A-Z]{2})"')
_PLAN_PATTERN = re.compile(
    r'"currentPlan"\s*:\s*"([^"]+)"'
    r'|"planName"\s*:\s*"([^"]+)"'
    r'|"plan_name"\s*:\s*"([^"]+)"'
)
_NEXT_PAYMENT_PATTERN = re.compile(
    r'"nextPaymentDate"\s*:\s*"([^"]+)"|"renewal_date"\s*:\s*"([^"]+)"'
)
_INVITE_PATTERN = re.compile(r'"inviteToken"\s*:\s*"([^"]+)"')


def _first_group(m: re.Match[str] | None) -> str | None:
    if not m:
        return None
    for g in m.groups():
        if g:
            return g
    return None


def _absorb_overview_html(html: str, info: dict[str, Any]) -> None:
    # Logged-in marker — embedded by the React account shell when the
    # session resolved successfully on the server side.
    if _LOGGED_IN_PATTERN.search(html):
        info["logged_in"] = True
    if not info.get("email"):
        v = _first_group(_EMAIL_PATTERN.search(html))
        if v:
            info["email"] = v
    if not info.get("name"):
        v = _first_group(_NAME_PATTERN.search(html))
        if v:
            info["name"] = v.strip()
    if not info.get("username"):
        m = _USERNAME_PATTERN.search(html)
        if m:
            info["username"] = m.group(1)
    if not info.get("country"):
        v = _first_group(_COUNTRY_PATTERN.search(html))
        if v:
            info["country"] = v
    if not info.get("plan"):
        v = _first_group(_PLAN_PATTERN.search(html))
        if v:
            info["plan"] = v
    if not info.get("next_payment_date"):
        v = _first_group(_NEXT_PAYMENT_PATTERN.search(html))
        if v:
            info["next_payment_date"] = v
    if not info.get("invite_token"):
        m = _INVITE_PATTERN.search(html)
        if m:
            info["invite_token"] = m.group(1)


_FAMILY_MEMBER_PATTERN = re.compile(
    r'"members"\s*:\s*\[(.*?)\]', re.DOTALL
)
_FAMILY_NAME_PATTERN = re.compile(
    r'"firstName"\s*:\s*"([^"\\]{1,80})"'
)


def _absorb_family_html(html: str, info: dict[str, Any]) -> None:
    block = _FAMILY_MEMBER_PATTERN.search(html)
    if not block:
        return
    members = _FAMILY_NAME_PATTERN.findall(block.group(1))
    if members:
        info["family_members"] = members[:10]
    m = _INVITE_PATTERN.search(html)
    if m and not info.get("invite_token"):
        info["invite_token"] = m.group(1)


def _finalise(info: dict[str, Any]) -> None:
    plan = (info.get("plan") or "").lower()
    # ``Spotify Free`` is the unpaid tier label — treat anything that
    # mentions the word ``free`` as not-pro.
    info["is_pro"] = bool(plan) and "free" not in plan
