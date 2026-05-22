"""shopify.com adapter (merchant identity).

Auth model: two scopes for ``.shopify.com`` cookies:
    * ``_identity_session`` on ``accounts.shopify.com`` — the SSO
      session token that Shopify Identity issues after login.
    * ``_shopify_essential`` / ``_shopify_essential_`` / ``_shopify_y``
      / ``master_device_id`` / ``is_shopify_merchant`` on
      ``.shopify.com`` — the merchant cookie set Shopify drops once
      the user has been recognised as a store owner.

Real-world limitation: every ``accounts.shopify.com`` endpoint is
Cloudflare-protected with a JS challenge that ``curl_cffi`` cannot
defeat from a non-browser context. We therefore validate the session
in two layers and report which layers passed:

    GET https://www.shopify.com/admin
        - 301 to admin.shopify.com           → routing OK

    GET https://admin.shopify.com/
        - 200 + body redirects to /login?
          errorHint=no_cookie_auth_token     → identity might be valid
                                              but no admin auth token
                                              (the store-level token
                                              expires on its own clock)
        - 200 + body redirects to /login?
          errorHint=invalid_credentials      → identity dead
        - 200 + body redirects to /store/<x> → fully alive (rare from
                                              CLI: admin usually
                                              needs the store token)

When the only available signal is ``no_cookie_auth_token`` we trust
the **presence** of both ``_identity_session`` and the
``is_shopify_merchant`` flag as a proxy for "identity is alive,
admin auth token has rotated" and report ALIVE with a clear caveat
in ``info``.
"""

from __future__ import annotations

import re
from typing import Any

from ..types import ScanResult
from .base import SiteAdapter


class ShopifyAdapter(SiteAdapter):
    SITE = "shopify.com"
    HOST = "accounts.shopify.com"
    BASE_URL = "https://accounts.shopify.com"
    KNOWN_COOKIES = (
        "_identity_session",
        "__Host-_identity_session_same_site",
        "_shopify_essential",
        "_shopify_essential_",
        "_shopify_y",
        "master_device_id",
        "is_shopify_merchant",
    )

    SUBDOMAIN_HOSTS = (
        "accounts.shopify.com",
        "www.shopify.com",
        "admin.shopify.com",
        "apps.shopify.com",
        ".apps.shopify.com",
        ".shopify.com",
    )

    def host_cookies(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for host in self.SUBDOMAIN_HOSTS:
            merged.update(self.jar.for_host(host))
        return merged

    def scan(self) -> ScanResult:
        result = ScanResult(site=self.SITE, alive=False)

        warning = self.cookies_warning()
        if warning:
            result.error = warning
            result.is_dead = True
            return result

        cookies = self.host_cookies()
        has_identity = "_identity_session" in cookies
        has_merchant_flag = cookies.get("is_shopify_merchant") == "1"

        result.info["has_identity_session"] = has_identity
        result.info["is_merchant"] = has_merchant_flag

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
            # 1) /admin redirector — not CF-challenged. Confirms routing
            #    to admin.shopify.com.
            url = "https://www.shopify.com/admin"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )

            # 2) admin home — body always 200 but contains an embedded
            #    redirect-to-login when the per-store auth token is
            #    missing or the identity has been revoked.
            url = "https://admin.shopify.com/"
            r = http.get(url)
            result.endpoints_tried.append(
                {"url": url, "status": r.status_code, "len": len(r.text)}
            )
            admin_signal: str | None = None
            store_handle: str | None = None
            if r.status_code == 200 and r.text:
                m_login = re.search(
                    r'data-url="https://admin\.shopify\.com/login\?'
                    r'([^"]+)"',
                    r.text,
                )
                m_store = re.search(
                    r'data-url="https://admin\.shopify\.com/store/'
                    r'([A-Za-z0-9_-]+)',
                    r.text,
                )
                if m_store:
                    store_handle = m_store.group(1)
                    admin_signal = f"store-redirect:{store_handle}"
                elif m_login:
                    qs = m_login.group(1)
                    if "errorHint=invalid_credentials" in qs:
                        admin_signal = "invalid_credentials"
                    elif "errorHint=no_cookie_auth_token" in qs:
                        admin_signal = "no_cookie_auth_token"
                    else:
                        admin_signal = f"login:{qs[:40]}"

            result.info["admin_signal"] = admin_signal or "unknown"

            # 3) Decision tree.
            if store_handle:
                result.info["primary_shop"] = store_handle
                result.alive = True
            elif admin_signal == "invalid_credentials":
                result.error = (
                    "admin.shopify.com rejected the cookies "
                    "(invalid_credentials)"
                )
                result.alive = False
                result.is_dead = True
            elif admin_signal == "no_cookie_auth_token":
                # The admin scope token has expired; the identity scope
                # *may* still be alive. Use cookie presence as a proxy
                # because accounts.shopify.com sits behind a Cloudflare
                # JS challenge we can't solve headless-only.
                if has_identity and has_merchant_flag:
                    result.alive = True
                    result.info["caveat"] = (
                        "identity cookies present; admin auth token "
                        "rotated. Open admin.shopify.com in a browser "
                        "to refresh."
                    )
                else:
                    result.error = (
                        "admin requires a fresh auth token and no "
                        "identity_session cookie was supplied"
                    )
                    result.is_dead = True
            elif admin_signal is None:
                result.error = (
                    "admin.shopify.com returned no recognisable signal "
                    f"(HTTP {r.status_code})"
                )

        if result.alive:
            _finalise(result.info)

        return result


def _finalise(info: dict[str, Any]) -> None:
    info.setdefault(
        "plan",
        "Merchant" if info.get("is_merchant") else "Account",
    )
    info["is_pro"] = bool(info.get("primary_shop") or info.get("is_merchant"))
