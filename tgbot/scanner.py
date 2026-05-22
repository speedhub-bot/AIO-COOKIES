"""Per-site scan dispatcher + Netscape cookie export.

The repo already ships two standalone scanners:

  * ``cookie_checker.py``       — claude.ai, chatgpt.com, cursor.com,
                                  devin.ai, crunchyroll.com
  * ``cookiescanner/`` package  — blackbox.ai, manus.im, perplexity.ai

This module hides both behind one async ``scan_site`` API so the
Telegram handlers can stay agnostic about which scanner backs a given
site. It also handles loading cookies from raw bytes / file paths and
optionally walking a ``.zip`` archive of multiple cookie files.
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Make the repo root importable so we can pull in cookie_checker.py
# regardless of the working directory the bot was launched from.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import cookie_checker as legacy  # noqa: E402  (post-sys.path insert)
from cookiescanner.cookies import Cookie, CookieJar  # noqa: E402
from cookiescanner.scanner import scan_all as cs_scan_all  # noqa: E402

from . import config


LEGACY_SITES: set[str] = {
    "claude.ai",
    "chatgpt.com",
    "cursor.com",
    "devin.ai",
    "crunchyroll.com",
}

CS_SITES: set[str] = {
    "blackbox.ai",
    "manus.im",
    "perplexity.ai",
    "netflix.com",
    "primevideo.com",
    "spotify.com",
    "roblox.com",
    "shopify.com",
    "facebook.com",
}


@dataclass
class ScanOutcome:
    """Unified per-file scan result."""

    site: str
    filename: str
    alive: bool
    info: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    cookies: list[dict[str, Any]] = field(default_factory=list)
    elapsed_s: float = 0.0
    # Cookies the underlying adapter *rotated* during the scan, e.g.
    # roblox.com's ``.ROBLOSECURITY`` after a successful auth-ticket
    # refresh. Maps cookie name -> fresh value. The bot reply surfaces
    # these so the user can keep them in sync with their on-disk jar
    # and the account stays alive across future scans.
    refreshed_cookies: dict[str, str] = field(default_factory=dict)


# ── Cookie loading ───────────────────────────────────────────


def load_cookies_from_path(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    """Load cookies from a file using the legacy loader (auto-detect)."""
    return legacy.load_cookie_file(str(path))


def jar_from_cookies(cookies: Iterable[dict[str, Any]]) -> CookieJar:
    """Convert legacy cookie dicts into a ``cookiescanner`` CookieJar."""
    out: list[Cookie] = []
    for c in cookies:
        name = c.get("name")
        value = c.get("value")
        if not name or value is None:
            continue
        out.append(
            Cookie(
                name=str(name),
                value=str(value),
                domain=str(c.get("domain", "") or ""),
            )
        )
    return CookieJar(out)


# ── Per-site scan ────────────────────────────────────────────


def _scan_legacy(
    site_id: str, cookies: list[dict[str, Any]], proxy: str | None
) -> tuple[bool, dict[str, Any], str | None, dict[str, str]]:
    checker = legacy.CHECKERS.get(site_id)
    if checker is None:
        return False, {}, f"no legacy checker for {site_id}", {}
    result = checker(cookies, proxy=proxy)
    return (
        bool(result.get("alive")),
        dict(result.get("info") or {}),
        result.get("error"),
        dict(result.get("refreshed_cookies") or {}),
    )


def _scan_cookiescanner(
    site_id: str, cookies: list[dict[str, Any]], proxy: str | None
) -> tuple[bool, dict[str, Any], str | None, dict[str, str]]:
    jar = jar_from_cookies(cookies)
    results = cs_scan_all(jar, proxy=proxy, only=[site_id])
    if not results:
        return False, {}, f"cookiescanner returned no result for {site_id}", {}
    r = results[0]
    return (
        bool(r.alive),
        dict(r.info or {}),
        r.error,
        dict(r.refreshed_cookies or {}),
    )


def scan_one_sync(
    site_id: str,
    cookies: list[dict[str, Any]],
    filename: str,
    proxy: str | None = None,
) -> ScanOutcome:
    """Run the right checker for *site_id* against *cookies* (synchronous)."""
    start = time.monotonic()
    if not cookies:
        return ScanOutcome(
            site=site_id,
            filename=filename,
            alive=False,
            error="no cookies parsed from file",
            elapsed_s=0.0,
        )

    # proxy precedence: explicit arg → DEFAULT_PROXY env var
    # (storage.get_next_proxy() is async and cannot be called here;
    #  callers that want round-robin must pass the proxy in explicitly)
    proxy = proxy or (config.DEFAULT_PROXY or None)

    if site_id in LEGACY_SITES:
        alive, info, err, refreshed = _scan_legacy(site_id, cookies, proxy)
    elif site_id in CS_SITES:
        alive, info, err, refreshed = _scan_cookiescanner(site_id, cookies, proxy)
    else:
        return ScanOutcome(
            site=site_id,
            filename=filename,
            alive=False,
            error=f"unsupported site: {site_id}",
            elapsed_s=time.monotonic() - start,
        )

    return ScanOutcome(
        site=site_id,
        filename=filename,
        alive=alive,
        info=info,
        error=err,
        cookies=cookies,
        elapsed_s=time.monotonic() - start,
        refreshed_cookies=refreshed,
    )


async def scan_site(
    site_id: str,
    file_path: str | os.PathLike[str],
    filename: str | None = None,
    proxy: str | None = None,
) -> list[ScanOutcome]:
    """Async wrapper. Returns one outcome per cookie file processed.

    A non-zip input yields exactly one outcome. A ``.zip`` input yields
    one outcome per cookie file discovered inside it.

    Proxy resolution order:
      1. *proxy* argument (explicit override)
      2. Round-robin from the admin-managed proxy list in storage
      3. DEFAULT_PROXY env var
      4. Direct connection (None)
    """
    from . import storage as _storage  # local import avoids circular dep

    if proxy is None:
        proxy = await _storage.get_next_proxy()          # may return None
    if proxy is None:
        proxy = config.DEFAULT_PROXY or None

    fp = str(file_path)
    display = filename or os.path.basename(fp)

    if fp.lower().endswith(".zip") and zipfile.is_zipfile(fp):
        return await asyncio.to_thread(_scan_zip_sync, site_id, fp, display, proxy)

    cookies = await asyncio.to_thread(load_cookies_from_path, fp)
    outcome = await asyncio.to_thread(scan_one_sync, site_id, cookies, display, proxy)
    return [outcome]


def _scan_zip_sync(site_id: str, zip_path: str, display: str, proxy: str | None) -> list[ScanOutcome]:
    """Walk every plausible cookie file in *zip_path* and scan it."""
    outcomes: list[ScanOutcome] = []
    with tempfile.TemporaryDirectory(dir=str(config.TEMP_DIR) if config.TEMP_DIR.exists() else None) as extract_dir:
        try:
            cookie_files = legacy.extract_zip(zip_path, extract_dir)
        except (zipfile.BadZipFile, OSError) as exc:
            return [
                ScanOutcome(
                    site=site_id,
                    filename=display,
                    alive=False,
                    error=f"failed to read zip: {exc}",
                )
            ]
        if not cookie_files:
            return [
                ScanOutcome(
                    site=site_id,
                    filename=display,
                    alive=False,
                    error="no cookie files found inside zip",
                )
            ]
        for fp in cookie_files:
            try:
                cookies = load_cookies_from_path(fp)
            except Exception as exc:  # noqa: BLE001
                outcomes.append(
                    ScanOutcome(
                        site=site_id,
                        filename=os.path.basename(fp),
                        alive=False,
                        error=f"load error: {exc}",
                    )
                )
                continue
            outcomes.append(
                scan_one_sync(site_id, cookies, os.path.basename(fp), proxy)
            )
    return outcomes


# ── Netscape export ──────────────────────────────────────────


def dump_netscape(cookies: list[dict[str, Any]], default_domain: str = "") -> str:
    """Serialise *cookies* to a Netscape ``cookies.txt`` body.

    Used to ship back cookies on a hit notification in the same format
    yt-dlp / curl / browser extensions consume. The header banner credits
    the bot owner (@akaza_isnt) so the file stays attributable when it
    ends up shared / forwarded.
    """
    lines = [
        "# Netscape HTTP Cookie File",
        "# https://curl.se/docs/http-cookies.html",
        "# ============================================================",
        "# Cookies exported by @akaza_isnt's AIO Cookies Bot",
        "# Telegram: https://t.me/akaza_isnt",
        "# ============================================================",
        "",
    ]
    for c in cookies:
        domain = (c.get("domain") or default_domain or "").lstrip(".")
        if not domain:
            # No domain known — skip rather than emit a malformed entry.
            continue
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        expires = c.get("expires") or 0
        try:
            expires = int(expires)
        except (TypeError, ValueError):
            expires = 0
        name = str(c.get("name") or "")
        value = str(c.get("value") or "")
        if not name:
            continue
        include_subdomains = "TRUE" if domain.startswith(".") or "." in domain else "FALSE"
        # Most browsers expect the domain prefix dotted so a wildcard match
        # against subdomains succeeds.
        dom_field = domain if domain.startswith(".") else f".{domain}"
        lines.append(
            "\t".join([dom_field, include_subdomains, path, secure, str(expires), name, value])
        )
    return "\n".join(lines) + "\n"
