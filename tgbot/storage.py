"""Persistent JSON-backed store for settings, user registry, and dashboard.

Layout under DATA_DIR:
  settings.json   — per-user settings (hit_notifications, etc.)
  users.json      — user registry (role, ban, join date, scan counts, history)
  dashboard.json  — global + per-site scan stats
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config

# ── Module-level state ────────────────────────────────────────
_LOCK            = asyncio.Lock()
_SETTINGS_CACHE: dict[str, dict[str, Any]] | None = None
_USERS_CACHE:    dict[str, dict[str, Any]] | None = None
_STATS_CACHE:    dict[str, Any]            | None = None


# ═══════════════════════════════════════════════════════════════
#  Path helpers
# ═══════════════════════════════════════════════════════════════

def _settings_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "settings.json"

def _users_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "users.json"

def _dashboard_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "dashboard.json"


# ═══════════════════════════════════════════════════════════════
#  Generic JSON load / save helpers
# ═══════════════════════════════════════════════════════════════

def _load_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}

def _save_json(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True, ensure_ascii=False)
    os.replace(tmp, path)


# ═══════════════════════════════════════════════════════════════
#  User registry
# ═══════════════════════════════════════════════════════════════

def _default_user(user_id: int) -> dict[str, Any]:
    return {
        "user_id":       user_id,
        "role":          config.ROLE_FREE,
        "banned":        False,
        "ban_reason":    "",
        "joined_at":     datetime.now(UTC).isoformat(timespec="seconds"),
        "premium_since": None,
        "username":      "",
        "first_name":    "",
        "total_checked": 0,
        "total_hits":    0,
        "services_used": {},      # site_id -> count
        "recent_checks": [],      # last 20 [{site, filename, alive, ts}]
    }


async def _ensure_users() -> dict[str, dict[str, Any]]:
    global _USERS_CACHE
    if _USERS_CACHE is None:
        _USERS_CACHE = await asyncio.to_thread(_load_json, _users_path())
    return _USERS_CACHE


async def _save_users(data: dict) -> None:
    await asyncio.to_thread(_save_json, _users_path(), data)


async def get_or_create_user(
    user_id: int,
    username: str = "",
    first_name: str = "",
) -> dict[str, Any]:
    """Return the registry entry for *user_id*, creating it if absent."""
    async with _LOCK:
        users = await _ensure_users()
        key   = str(user_id)
        if key not in users:
            entry = _default_user(user_id)
            entry["username"]   = username
            entry["first_name"] = first_name
            users[key] = entry
            await _save_users(users)
        else:
            entry = users[key]
            # keep display info fresh
            changed = False
            if username   and entry.get("username")   != username:
                entry["username"]   = username;  changed = True
            if first_name and entry.get("first_name") != first_name:
                entry["first_name"] = first_name; changed = True
            if changed:
                await _save_users(users)
        return dict(entry)


async def get_user(user_id: int) -> dict[str, Any] | None:
    async with _LOCK:
        users = await _ensure_users()
        entry = users.get(str(user_id))
        return dict(entry) if entry else None


async def get_all_users() -> list[dict[str, Any]]:
    async with _LOCK:
        users = await _ensure_users()
        return [dict(v) for v in users.values()]


async def set_user_role(user_id: int, role: str) -> dict[str, Any]:
    """Set role to 'free', 'premium', or 'admin'."""
    async with _LOCK:
        users = await _ensure_users()
        key   = str(user_id)
        if key not in users:
            users[key] = _default_user(user_id)
        users[key]["role"] = role
        if role == config.ROLE_PREMIUM and not users[key].get("premium_since"):
            users[key]["premium_since"] = datetime.now(UTC).isoformat(timespec="seconds")
        if role == config.ROLE_FREE:
            users[key]["premium_since"] = None
        await _save_users(users)
        return dict(users[key])


async def ban_user(user_id: int, reason: str = "") -> dict[str, Any]:
    async with _LOCK:
        users = await _ensure_users()
        key   = str(user_id)
        if key not in users:
            users[key] = _default_user(user_id)
        users[key]["banned"]     = True
        users[key]["ban_reason"] = reason
        await _save_users(users)
        return dict(users[key])


async def unban_user(user_id: int) -> dict[str, Any]:
    async with _LOCK:
        users = await _ensure_users()
        key   = str(user_id)
        if key not in users:
            users[key] = _default_user(user_id)
        users[key]["banned"]     = False
        users[key]["ban_reason"] = ""
        await _save_users(users)
        return dict(users[key])


async def is_banned(user_id: int) -> bool:
    async with _LOCK:
        users = await _ensure_users()
        entry = users.get(str(user_id))
        return bool(entry and entry.get("banned"))


async def get_user_role(user_id: int) -> str:
    """Returns 'admin' if user_id matches ADMIN_ID, else stored role."""
    if user_id == config.ADMIN_ID:
        return config.ROLE_ADMIN
    async with _LOCK:
        users = await _ensure_users()
        entry = users.get(str(user_id))
        return str(entry["role"]) if entry else config.ROLE_FREE


async def record_user_scan(
    user_id: int,
    site_id: str,
    outcomes: list[Any],
) -> None:
    """Update per-user scan counters and recent history."""
    if not outcomes:
        return
    async with _LOCK:
        users = await _ensure_users()
        key   = str(user_id)
        if key not in users:
            users[key] = _default_user(user_id)
        entry = users[key]

        hits    = sum(1 for o in outcomes if getattr(o, "alive", False))
        checked = len(outcomes)

        entry["total_checked"] = int(entry.get("total_checked", 0)) + checked
        entry["total_hits"]    = int(entry.get("total_hits",    0)) + hits

        svcs = entry.setdefault("services_used", {})
        svcs[site_id] = int(svcs.get(site_id, 0)) + checked

        recent: list[dict] = entry.setdefault("recent_checks", [])
        now_ts = datetime.now(UTC).isoformat(timespec="seconds")
        for o in outcomes:
            recent.append({
                "site":     site_id,
                "filename": getattr(o, "filename", ""),
                "alive":    bool(getattr(o, "alive", False)),
                "ts":       now_ts,
            })
        entry["recent_checks"] = recent[-20:]   # keep last 20

        await _save_users(users)


# ═══════════════════════════════════════════════════════════════
#  Settings (hit notifications, etc.)
# ═══════════════════════════════════════════════════════════════

async def _ensure_settings() -> dict[str, dict[str, Any]]:
    global _SETTINGS_CACHE
    if _SETTINGS_CACHE is None:
        _SETTINGS_CACHE = await asyncio.to_thread(_load_json, _settings_path())
    return _SETTINGS_CACHE


async def get_user_settings(user_id: int) -> dict[str, Any]:
    async with _LOCK:
        cache = await _ensure_settings()
        key   = str(user_id)
        entry = cache.get(key)
        if entry is None:
            entry = {"hit_notifications": config.HIT_NOTIFICATIONS_DEFAULT}
            cache[key] = entry
            await asyncio.to_thread(_save_json, _settings_path(), cache)
        else:
            if "hit_notifications" not in entry:
                entry["hit_notifications"] = config.HIT_NOTIFICATIONS_DEFAULT
        return dict(entry)


async def set_setting(user_id: int, key: str, value: Any) -> dict[str, Any]:
    async with _LOCK:
        cache = await _ensure_settings()
        entry = cache.setdefault(str(user_id), {})
        entry[key] = value
        await asyncio.to_thread(_save_json, _settings_path(), cache)
        return dict(entry)


async def toggle_bool(user_id: int, key: str, default: bool = False) -> bool:
    async with _LOCK:
        cache = await _ensure_settings()
        entry = cache.setdefault(str(user_id), {})
        current     = bool(entry.get(key, default))
        entry[key]  = not current
        await asyncio.to_thread(_save_json, _settings_path(), cache)
        return not current


async def get_hit_notifications(user_id: int) -> bool:
    settings = await get_user_settings(user_id)
    return bool(settings.get("hit_notifications", config.HIT_NOTIFICATIONS_DEFAULT))


# ═══════════════════════════════════════════════════════════════
#  Dashboard / scan stats
# ═══════════════════════════════════════════════════════════════

def _empty_dashboard() -> dict[str, Any]:
    return {
        "total": 0, "alive": 0, "dead": 0, "errored": 0,
        "last_updated": None,
        "sites": {
            site["id"]: {
                "total": 0, "alive": 0, "dead": 0, "errored": 0,
                "plans": {}, "premium": 0, "free": 0,
            }
            for site in config.SUPPORTED_SITES
        },
    }


def _normalise_dashboard(data: dict[str, Any]) -> dict[str, Any]:
    db = _empty_dashboard()
    for k in ("total", "alive", "dead", "errored"):
        try:
            db[k] = int(data.get(k, 0) or 0)
        except (TypeError, ValueError):
            db[k] = 0
    db["last_updated"] = data.get("last_updated") or None
    raw_sites = data.get("sites") if isinstance(data.get("sites"), dict) else {}
    for sid, raw in raw_sites.items():
        if not isinstance(raw, dict):
            continue
        site = db["sites"].setdefault(
            str(sid),
            {"total": 0, "alive": 0, "dead": 0, "errored": 0,
             "plans": {}, "premium": 0, "free": 0},
        )
        for k in ("total", "alive", "dead", "errored", "premium", "free"):
            try:
                site[k] = int(raw.get(k, 0) or 0)
            except (TypeError, ValueError):
                site[k] = 0
        plans = raw.get("plans") if isinstance(raw.get("plans"), dict) else {}
        site["plans"] = {}
        for plan, cnt in plans.items():
            try:
                v = int(cnt or 0)
            except (TypeError, ValueError):
                continue
            if v > 0:
                site["plans"][str(plan)] = v
    return db


def _load_dashboard_sync() -> dict[str, Any]:
    path = _dashboard_path()
    if path.exists():
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return _normalise_dashboard(data)
        except (OSError, json.JSONDecodeError):
            pass
    return _empty_dashboard()


def _save_dashboard_sync(data: dict[str, Any]) -> None:
    _save_json(_dashboard_path(), data)


async def _ensure_dashboard() -> dict[str, Any]:
    global _STATS_CACHE
    if _STATS_CACHE is None:
        _STATS_CACHE = await asyncio.to_thread(_load_dashboard_sync)
    return _STATS_CACHE


def plan_label(site_id: str, info: dict[str, Any], alive: bool) -> str | None:
    if not alive:
        return None
    raw = None
    for key in ("plan", "plan_name", "subscription_tier", "payment_tier",
                "membership_status", "tier", "subscription_status"):
        v = info.get(key)
        if v not in (None, ""):
            raw = v; break
    text    = str(raw).strip() if raw is not None else ""
    lowered = text.lower()
    if not text or lowered in {"n/a", "na", "none", "null", "unknown", "false"}:
        if info.get("is_pro") is True  or info.get("is_premium") is True:
            return _paid_fallback(site_id)
        if info.get("is_pro") is False or info.get("is_premium") is False:
            return "Free"
        return "Unknown"

    # ── Site-specific overrides ──────────────────────────────
    # Crunchyroll: SKU codes like `crunchyroll.fanpack.monthly` need
    # to map to friendly tier names BEFORE the generic substring matcher
    # runs (otherwise `fanpack` gets caught by the bare `pack`/`fan` rules
    # and ends up labelled as "Fan" even when it's a Mega/Ultimate sub).
    if site_id == "crunchyroll.com":
        sku = str(info.get("plan_sku") or "").lower()
        haystack = f"{lowered} {sku}"
        if "ultimate" in haystack:
            return "Ultimate Fan"
        if "megafan" in haystack or "mega fan" in haystack:
            return "Mega Fan"
        if "fanpack" in haystack or lowered == "fan" or "fan pack" in haystack or "fan" in haystack:
            return "Fan"
        if "premium" in haystack:
            return "Premium"

    for needle, label in (
        ("team/enterprise", "Team/Enterprise"), ("enterprise", "Enterprise"),
        ("team", "Team"), ("max", "Max"), ("ultra", "Max"), ("plus", "Plus"),
        ("prime", "Prime"), ("premium", "Premium"),
        ("pro", "Pro"), ("paid", "Pro"),
        ("trial", "Trial"), ("free", "Free"), ("basic", "Free"),
    ):
        if needle in lowered:
            return label
    if lowered == "active":
        return "Pro"
    return text[:80]


def _paid_fallback(site_id: str) -> str:
    if site_id in {"chatgpt.com", "claude.ai", "cursor.com",
                   "devin.ai", "perplexity.ai", "manus.im"}:
        return "Pro"
    if site_id == "primevideo.com":
        return "Prime"
    return "Premium"


async def record_scan_outcomes(outcomes: list[Any]) -> None:
    if not outcomes:
        return
    async with _LOCK:
        db = await _ensure_dashboard()
        for o in outcomes:
            sid   = str(getattr(o, "site", "") or "unknown")
            alive = bool(getattr(o, "alive", False))
            err   = getattr(o, "error", None)
            site  = db["sites"].setdefault(
                sid,
                {"total": 0, "alive": 0, "dead": 0, "errored": 0,
                 "plans": {}, "premium": 0, "free": 0},
            )
            db["total"]  += 1; site["total"] += 1
            if alive:
                db["alive"]  += 1; site["alive"] += 1
                info  = getattr(o, "info", {}) or {}
                plan  = plan_label(sid, dict(info), alive=True)
                if plan:
                    plans = site.setdefault("plans", {})
                    plans[plan] = int(plans.get(plan, 0)) + 1
            elif err:
                # An exception/HTTP error blocked the check — don't conflate
                # with a confirmed dead session, account for it separately.
                db["errored"] = int(db.get("errored", 0)) + 1
                site["errored"] = int(site.get("errored", 0)) + 1
            else:
                db["dead"]   += 1; site["dead"]  += 1
        db["last_updated"] = datetime.now(UTC).isoformat(timespec="seconds")
        await asyncio.to_thread(_save_dashboard_sync, db)


async def get_dashboard_stats() -> dict[str, Any]:
    async with _LOCK:
        db = await _ensure_dashboard()
        return json.loads(json.dumps(db))


async def record_tier_counts(site_id: str, premium: int, free: int) -> None:
    if premium == 0 and free == 0:
        return
    async with _LOCK:
        db   = await _ensure_dashboard()
        site = db["sites"].setdefault(
            str(site_id),
            {"total": 0, "alive": 0, "dead": 0,
             "plans": {}, "premium": 0, "free": 0},
        )
        site["premium"] = int(site.get("premium", 0) or 0) + premium
        site["free"]    = int(site.get("free",    0) or 0) + free
        await asyncio.to_thread(_save_dashboard_sync, db)


# ═══════════════════════════════════════════════════════════════
#  Proxy management
#
#  Proxies are stored as plain text, one per line:
#    http://host:port
#    http://user:pass@host:port
#    socks5://host:port
#
#  One proxy is marked "active" (rotated in round-robin by the
#  scanner).  The full list + active index live in
#  bot_data/proxies.txt  (one proxy per line) and
#  bot_data/proxy_index.txt  (single integer, the current index).
# ═══════════════════════════════════════════════════════════════

_PROXY_LOCK = asyncio.Lock()


def _proxy_list_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.PROXY_FILE


def _proxy_index_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "proxy_index.txt"


def _read_proxy_list_sync() -> list[str]:
    path = _proxy_list_path()
    if not path.exists():
        return []
    try:
        lines = path.read_text("utf-8").splitlines()
        return [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
    except OSError:
        return []


def _write_proxy_list_sync(proxies: list[str]) -> None:
    path = _proxy_list_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(proxies) + ("\n" if proxies else ""), encoding="utf-8")


def _read_proxy_index_sync() -> int:
    path = _proxy_index_path()
    try:
        return max(0, int(path.read_text("utf-8").strip()))
    except Exception:
        return 0


def _write_proxy_index_sync(idx: int) -> None:
    _proxy_index_path().write_text(str(idx), encoding="utf-8")


async def get_proxies() -> list[str]:
    """Return the full proxy list (may be empty)."""
    async with _PROXY_LOCK:
        return await asyncio.to_thread(_read_proxy_list_sync)


async def get_proxy_count() -> int:
    return len(await get_proxies())


async def set_proxies(proxies: list[str]) -> None:
    """Replace the entire proxy list and reset the rotation index."""
    clean = [p.strip() for p in proxies if p.strip() and not p.strip().startswith("#")]
    async with _PROXY_LOCK:
        await asyncio.to_thread(_write_proxy_list_sync, clean)
        await asyncio.to_thread(_write_proxy_index_sync, 0)


async def add_proxies(proxies: list[str]) -> int:
    """Append new proxies (deduped). Returns new total count."""
    async with _PROXY_LOCK:
        existing = await asyncio.to_thread(_read_proxy_list_sync)
        existing_set = set(existing)
        added = [p.strip() for p in proxies
                 if p.strip() and not p.strip().startswith("#")
                 and p.strip() not in existing_set]
        merged = existing + added
        await asyncio.to_thread(_write_proxy_list_sync, merged)
        return len(merged)


async def delete_proxy(index: int) -> bool:
    """Delete proxy at *index* (0-based). Returns True if deleted."""
    async with _PROXY_LOCK:
        proxies = await asyncio.to_thread(_read_proxy_list_sync)
        if index < 0 or index >= len(proxies):
            return False
        proxies.pop(index)
        await asyncio.to_thread(_write_proxy_list_sync, proxies)
        # reset rotation so we don't land out-of-bounds
        await asyncio.to_thread(_write_proxy_index_sync, 0)
        return True


async def clear_proxies() -> None:
    """Remove all proxies."""
    async with _PROXY_LOCK:
        await asyncio.to_thread(_write_proxy_list_sync, [])
        await asyncio.to_thread(_write_proxy_index_sync, 0)


async def get_next_proxy() -> str | None:
    """Round-robin: return the next proxy and advance the index.
    Returns None if the list is empty (direct connection used)."""
    async with _PROXY_LOCK:
        proxies = await asyncio.to_thread(_read_proxy_list_sync)
        if not proxies:
            return None
        idx = await asyncio.to_thread(_read_proxy_index_sync)
        idx = idx % len(proxies)
        proxy = proxies[idx]
        await asyncio.to_thread(_write_proxy_index_sync, (idx + 1) % len(proxies))
        return proxy
