"""facebook.com adapter.

Auth model: ``c_user`` + ``xs`` cookies on ``.facebook.com``.
``c_user`` is literally the numeric user id; ``xs`` is the signed
session token. Tracker-only cookies (``sb``, ``datr``, ``fr``,
``wd``, ``ps_l`` / ``ps_n``) are NOT enough to log a user in — many
exports in the wild only contain those and should be reported dead.

Endpoints we use:

    GET https://www.facebook.com/me/
        - 302 to /<vanity-name>             → alive (vanity URL)
        - 302 to /?... or to the root       → alive (no vanity set;
                                              Facebook now sends new
                                              accounts straight to /)
        - 302 to /login/... or /checkpoint/ → cookie dead / 2FA gate
        - 200 with logged-in body markers   → alive

    GET https://mbasic.facebook.com/profile.php?v=info
        - Lightweight HTML profile page that tends to render even
          when the desktop site is gating behind security checks.

    GET https://www.facebook.com/settings/?tab=account
        - Best-effort: pulls email when account-center hasn't
          required a 2FA challenge.
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class FacebookAdapter(SiteAdapter):
    SITE = "facebook.com"
    HOST = "www.facebook.com"
    BASE_URL = "https://www.facebook.com"
    KNOWN_COOKIES = ("c_user", "xs")

    SUBDOMAIN_HOSTS = (
        "www.facebook.com",
        "m.facebook.com",
        "mbasic.facebook.com",
        "accountscenter.facebook.com",
        ".facebook.com",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for host in self.SUBDOMAIN_HOSTS:
            merged.update(self.jar.for_host(host))
        return merged

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        cookies = self.host_cookies()
        # ``c_user`` and ``xs`` are mandatory for a logged-in session.
        # Anything less and Facebook will redirect to /login.
        if "c_user" not in cookies or "xs" not in cookies:
            present = [c for c in self.KNOWN_COOKIES if c in cookies]
            result.error = (
                "Facebook session requires both c_user + xs cookies; "
                f"found only {present or 'tracker-only cookies'}."
            )
            result.is_dead = True
            return result

        user_id = cookies["c_user"]
        result.info["user_id"] = user_id

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
        with self.make_client(extra_headers=headers) as http:
            # 1) /me/ — redirect tells us alive vs dead.
            url = self.BASE_URL + "/me/"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            location = (
                r.headers.get("location")
                or r.headers.get("Location")
                or ""
            )
            if r.status_code in (301, 302, 303, 307, 308):
                loc_lower = location.lower()
                if (
                    "/login" in loc_lower
                    or "checkpoint" in loc_lower
                    or "/recover" in loc_lower
                ):
                    result.error = (
                        f"/me/ redirected to {location} (cookie dead "
                        "or 2FA / checkpoint required)"
                    )
                    result.is_dead = True
                    return result
                # Anything else — vanity URL, profile.php?id=X, or
                # the root — counts as a logged-in redirect.
                result.alive = True
                vanity = _extract_vanity(location)
                if vanity:
                    result.info["username"] = vanity
                    result.info["profile_url"] = location
            elif r.status_code == 200:
                # Some sessions don't redirect — body has the profile.
                if _looks_logged_in(r.text):
                    result.alive = True
                    _absorb_profile_html(r.text, result.info)
                else:
                    result.error = "/me/ returned 200 but body looks logged-out"
                    result.is_dead = True
                    return result
            else:
                result.error = f"/me/ returned HTTP {r.status_code}"
                if r.status_code in (401, 403):
                    result.is_dead = True
                return result

            # 2) mbasic profile page — best-effort for name + dob.
            url = "https://mbasic.facebook.com/profile.php?v=info"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            if r.status_code == 200 and r.text:
                _absorb_profile_html(r.text, result.info)

            # 3) Settings — has email behind a soft 2FA gate. Best-effort.
            url = self.BASE_URL + "/settings/?tab=account"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            if r.status_code == 200 and r.text:
                _absorb_profile_html(r.text, result.info)

        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


_LOGIN_MARKERS = (
    'name="login"',
    'id="loginbutton"',
    'action="/login/',
)


def _looks_logged_in(html: str) -> bool:
    if any(m in html for m in _LOGIN_MARKERS):
        return False
    # Two cheap positive signals: presence of ``__user`` token or the
    # logged-in app shell's actorID.
    return ('"USER_ID":"' in html) or ('"actorID":"' in html) or ('"__user":"' in html)


def _extract_vanity(location: str) -> str | None:
    """Return the user vanity slug from a /me/ redirect Location.

    We deliberately reject:
      * the bare scheme://host root (no slug)
      * URLs containing ``profile.php`` (id-based, not vanity)
      * any path that matches the domain itself (parser sanity check)
    """
    if not location:
        return None
    parsed = urllib.parse.urlsplit(location)
    path = (parsed.path or "").strip("/")
    if not path:
        # Redirect to https://www.facebook.com/ — alive but no vanity.
        return None
    if path.lower() == parsed.netloc.lower():
        return None
    if "profile.php" in path:
        # /profile.php?id=<c_user> — already covered by user_id.
        return None
    # Take the first segment only; FB vanity URLs are single-segment.
    slug = path.split("/", 1)[0]
    if not re.fullmatch(r"[A-Za-z0-9._\-]{2,80}", slug):
        return None
    return slug


_NAME_PATTERNS = (
    re.compile(r'"NAME"\s*:\s*"([^"\\]+)"'),
    re.compile(r'"name"\s*:\s*"([^"\\]{1,80})"\s*,\s*"vanity"'),
    re.compile(r'<title[^>]*>([^<|]+)\s*\|\s*Facebook</title>'),
)
_EMAIL_PATTERN = re.compile(
    r'"emailAddress"\s*:\s*"([^"]+)"|"email_address"\s*:\s*"([^"]+)"'
)
_VANITY_PATTERN = re.compile(r'"vanity"\s*:\s*"([a-zA-Z0-9.\-]+)"')


def _absorb_profile_html(html: str, info: dict[str, Any]) -> None:
    if not info.get("name"):
        for pat in _NAME_PATTERNS:
            m = pat.search(html)
            if m:
                info["name"] = m.group(1).strip()
                break
    if not info.get("email"):
        m = _EMAIL_PATTERN.search(html)
        if m:
            info["email"] = m.group(1) or m.group(2)
    if not info.get("username"):
        m = _VANITY_PATTERN.search(html)
        if m:
            info["username"] = m.group(1)


def _finalise(info: dict[str, Any]) -> None:
    # Facebook is binary — logged in or not. Nothing tiered to expose.
    info["plan"] = "Free"
    info["is_pro"] = False
