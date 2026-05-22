"""primevideo.com adapter (Amazon Prime Video).

Auth model: Amazon's web auth cookies (``session-token``, ``at-main``,
``x-main``, ``ubid-main``) are set on ``.amazon.com``. Prime Video's
storefront on ``www.primevideo.com`` actually re-uses those Amazon
cookies plus its own ``at-acbin`` / ``ubid-acbin`` regional twins.

Because the cookies live on a different host than the API we hit,
``host_cookies`` is overridden here to merge cookies from both
``.amazon.com`` and ``.primevideo.com``.

Endpoints we use:

    GET https://www.primevideo.com/region/eu/storefront
        - 302 to ap/signin                 → cookie dead
        - 200                              → alive; final URL gives region

    GET https://atv-ps.primevideo.com/acm/GetConfiguration/WebClient
        - JSON {customerID, recordTerritory}  → reliable region + customer id
"""

from __future__ import annotations

import re
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class PrimeVideoAdapter(SiteAdapter):
    SITE = "primevideo.com"
    HOST = "www.primevideo.com"
    BASE_URL = "https://www.primevideo.com"
    KNOWN_COOKIES = (
        "session-token",
        "at-main",
        "x-main",
        "ubid-main",
        "at-acbin",
        "ubid-acbin",
    )

    # Cookies that come from amazon.com but Prime Video accepts too.
    AMAZON_HOSTS = (
        "www.primevideo.com",
        ".primevideo.com",
        "www.amazon.com",
        ".amazon.com",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for host in self.AMAZON_HOSTS:
            merged.update(self.jar.for_host(host))
        return merged

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        warning = self.cookies_warning()
        if warning:
            result.error = warning
            result.is_dead = True
            return result

        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        )
        headers = {
            **self.common_headers(),
            "User-Agent": ua,
            "Accept": (
                "text/html,application/xhtml+xml,application/xml;"
                "q=0.9,*/*;q=0.8"
            ),
        }
        with self.make_client(extra_headers=headers) as http:
            # 1) Storefront — login redirect on dead cookies.
            url = self.BASE_URL + "/region/eu/storefront"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            location = r.headers.get("location") or r.headers.get("Location") or ""
            if r.status_code in (301, 302, 303, 307, 308) and "signin" in location.lower():
                result.error = f"storefront redirected to {location} (cookie dead)"
                result.is_dead = True
                return result
            if r.status_code == 401 or r.status_code == 403:
                result.error = f"storefront returned HTTP {r.status_code}"
                result.is_dead = True
                return result
            if r.status_code != 200:
                result.error = f"storefront returned HTTP {r.status_code}"
                return result

            text = r.text or ""
            if "ap/signin" in text and len(text) < 2048:
                result.error = "storefront body contained signin redirect (cookie dead)"
                result.is_dead = True
                return result

            _absorb_storefront(text, result.info)

            # 2) GetConfiguration — JSON with customerID + recordTerritory.
            url = (
                "https://atv-ps.primevideo.com/acm/GetConfiguration/WebClient"
                "?deviceTypeID=AOAGZA014O5RE&deviceID=Web"
            )
            r = http.get(
                url,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Referer": self.BASE_URL + "/region/eu/storefront",
                },
            )
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            data = self.try_json(r)
            if isinstance(data, dict):
                _absorb_config(data, result.info)

        if not (result.info.get("region") or result.info.get("customerID")):
            result.error = "storefront responded but no region/customer id surfaced"
            result.is_dead = True
            return result

        result.alive = True
        _finalise(result.info)
        return result


# ----- helpers ---------------------------------------------------------


_TERRITORY_PATTERN = re.compile(r'"recordTerritory"\s*:\s*"([A-Z]{2})"')
_CUSTOMER_PATTERN = re.compile(r'"customerID"\s*:\s*"([A-Z0-9]+)"')
_USERNAME_PATTERN = re.compile(r'"userName"\s*:\s*"([^"]+)"')
_PROFILE_PATTERN = re.compile(r'"profileName"\s*:\s*"([^"]+)"')


def _absorb_storefront(html: str, info: dict[str, Any]) -> None:
    m = _TERRITORY_PATTERN.search(html)
    if m:
        info["region"] = m.group(1)
    m = _CUSTOMER_PATTERN.search(html)
    if m:
        info["customerID"] = m.group(1)
    m = _USERNAME_PATTERN.search(html)
    if m:
        info["name"] = m.group(1)
    m = _PROFILE_PATTERN.search(html)
    if m:
        info["profile"] = m.group(1)


def _absorb_config(payload: dict[str, Any], info: dict[str, Any]) -> None:
    for k_src, k_dst in (
        ("recordTerritory", "region"),
        ("customerID", "customerID"),
        ("preferredMarketplace", "marketplace"),
    ):
        v = payload.get(k_src)
        if v and not info.get(k_dst):
            info[k_dst] = v

    # Prime Video sometimes nests this under ``customer``.
    customer = payload.get("customer")
    if isinstance(customer, dict):
        if customer.get("customerID") and not info.get("customerID"):
            info["customerID"] = customer["customerID"]
        if customer.get("displayName") and not info.get("name"):
            info["name"] = customer["displayName"]


def _finalise(info: dict[str, Any]) -> None:
    # Prime Video doesn't expose plan tiers to the storefront; the only
    # binary signal is "has Prime" vs "not signed in", which we already
    # use as the alive check. Surface plan as "Prime" so the formatter
    # has something useful to show.
    info.setdefault("plan", "Prime")
    info["is_pro"] = True
