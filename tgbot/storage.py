"""Tiny JSON-backed bot state store.

Keeps the dependency surface small — JSON files under ``DATA_DIR`` are
enough for per-user settings and the live scan dashboard. Reads are
in-process and writes are atomic via a rename.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import config


_LOCK = asyncio.Lock()
_CACHE: dict[str, dict[str, Any]] | None = None
_STATS_CACHE: dict[str, Any] | None = None


def _settings_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "settings.json"


def _dashboard_path() -> Path:
    config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return config.DATA_DIR / "dashboard.json"


def _empty_dashboard() -> dict[str, Any]:
    return {
        "total": 0,
        "alive": 0,
        "dead": 0,
        "last_updated": None,
        "sites": {
            site["id"]: {
                "total": 0,
                "alive": 0,
                "dead": 0,
                "plans": {},
                "premium": 0,
                "free": 0,
            }
            for site in config.SUPPORTED_SITES
        },
    }


def _load_sync() -> dict[str, dict[str, Any]]:
    path = _settings_path()
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return {}


def _save_sync(data: dict[str, dict[str, Any]]) -> None:
    path = _settings_path()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


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
    path = _dashboard_path()
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _normalise_dashboard(data: dict[str, Any]) -> dict[str, Any]:
    dashboard = _empty_dashboard()
    for key in ("total", "alive", "dead"):
        try:
            dashboard[key] = int(data.get(key, 0) or 0)
        except (TypeError, ValueError):
            dashboard[key] = 0
    dashboard["last_updated"] = data.get("last_updated") or None

    raw_sites = data.get("sites") if isinstance(data.get("sites"), dict) else {}
    for site_id, raw in raw_sites.items():
        if not isinstance(raw, dict):
            continue
        site = dashboard["sites"].setdefault(
            str(site_id), {"total": 0, "alive": 0, "dead": 0, "plans": {}, "premium": 0, "free": 0}
        )
        for key in ("total", "alive", "dead"):
            try:
                site[key] = int(raw.get(key, 0) or 0)
            except (TypeError, ValueError):
                site[key] = 0
        plans = raw.get("plans") if isinstance(raw.get("plans"), dict) else {}
        site["plans"] = {}
        for plan, count in plans.items():
            try:
                value = int(count or 0)
            except (TypeError, ValueError):
                continue
            if value > 0:
                site["plans"][str(plan)] = value
    return dashboard


async def _ensure_loaded() -> dict[str, dict[str, Any]]:
    global _CACHE
    if _CACHE is None:
        _CACHE = await asyncio.to_thread(_load_sync)
    return _CACHE


async def _ensure_dashboard_loaded() -> dict[str, Any]:
    global _STATS_CACHE
    if _STATS_CACHE is None:
        _STATS_CACHE = await asyncio.to_thread(_load_dashboard_sync)
    return _STATS_CACHE


async def get_user_settings(user_id: int) -> dict[str, Any]:
    """Return the full settings dict for a user (creates defaults)."""
    async with _LOCK:
        cache = await _ensure_loaded()
        key = str(user_id)
        entry = cache.get(key)
        if entry is None:
            entry = {
                "hit_notifications": config.HIT_NOTIFICATIONS_DEFAULT,
            }
            cache[key] = entry
            await asyncio.to_thread(_save_sync, cache)
        else:
            # Backfill any newly-added keys without erasing existing ones.
            if "hit_notifications" not in entry:
                entry["hit_notifications"] = config.HIT_NOTIFICATIONS_DEFAULT
        return dict(entry)


async def set_setting(user_id: int, key: str, value: Any) -> dict[str, Any]:
    async with _LOCK:
        cache = await _ensure_loaded()
        ukey = str(user_id)
        entry = cache.setdefault(ukey, {})
        entry[key] = value
        await asyncio.to_thread(_save_sync, cache)
        return dict(entry)


async def toggle_bool(user_id: int, key: str, default: bool = False) -> bool:
    async with _LOCK:
        cache = await _ensure_loaded()
        ukey = str(user_id)
        entry = cache.setdefault(ukey, {})
        current = bool(entry.get(key, default))
        entry[key] = not current
        await asyncio.to_thread(_save_sync, cache)
        return not current


async def get_hit_notifications(user_id: int) -> bool:
    settings = await get_user_settings(user_id)
    return bool(settings.get("hit_notifications", config.HIT_NOTIFICATIONS_DEFAULT))


def plan_label(site_id: str, info: dict[str, Any], alive: bool) -> str | None:
    """Return the dashboard bucket for an ALIVE account's plan."""
    if not alive:
        return None

    raw = _first_plan_value(info)
    text = str(raw).strip() if raw is not None else ""
    lowered = text.lower()

    if not text or lowered in {"n/a", "na", "none", "null", "unknown", "false"}:
        if info.get("is_pro") is True or info.get("is_premium") is True:
            return _paid_fallback(site_id)
        if info.get("is_pro") is False or info.get("is_premium") is False:
            return "Free"
        return "Unknown"

    if "team/enterprise" in lowered:
        return "Team/Enterprise"
    if "enterprise" in lowered:
        return "Enterprise"
    if "team" in lowered:
        return "Team"
    if "max" in lowered or "ultra" in lowered:
        return "Max"
    if "plus" in lowered:
        return "Plus"
    if "prime" in lowered:
        return "Prime"
    if "premium" in lowered:
        return "Premium"
    if "pro" in lowered or "paid" in lowered or lowered == "active":
        return "Pro"
    if "trial" in lowered:
        return "Trial"
    if "free" in lowered or lowered == "basic":
        return "Free"

    return text[:80]


def _first_plan_value(info: dict[str, Any]) -> Any:
    for key in (
        "plan",
        "plan_name",
        "subscription_tier",
        "payment_tier",
        "membership_status",
        "tier",
        "subscription_status",
    ):
        value = info.get(key)
        if value not in (None, ""):
            return value
    return None


def _paid_fallback(site_id: str) -> str:
    if site_id in {
        "chatgpt.com",
        "claude.ai",
        "cursor.com",
        "devin.ai",
        "perplexity.ai",
        "manus.im",
    }:
        return "Pro"
    if site_id == "primevideo.com":
        return "Prime"
    return "Premium"


async def record_scan_outcomes(outcomes: list[Any]) -> None:
    if not outcomes:
        return
    async with _LOCK:
        dashboard = await _ensure_dashboard_loaded()
        for outcome in outcomes:
            site_id = str(getattr(outcome, "site", "") or "unknown")
            alive = bool(getattr(outcome, "alive", False))
            site = dashboard["sites"].setdefault(
                site_id, {"total": 0, "alive": 0, "dead": 0, "plans": {}, "premium": 0, "free": 0}
            )
            dashboard["total"] += 1
            site["total"] += 1
            if alive:
                dashboard["alive"] += 1
                site["alive"] += 1
                info = getattr(outcome, "info", {}) or {}
                plan = plan_label(site_id, dict(info), alive=True)
                if plan:
                    plans = site.setdefault("plans", {})
                    plans[plan] = int(plans.get(plan, 0)) + 1
            else:
                dashboard["dead"] += 1
                site["dead"] += 1
        dashboard["last_updated"] = datetime.now(UTC).isoformat(timespec="seconds")
        await asyncio.to_thread(_save_dashboard_sync, dashboard)


async def get_dashboard_stats() -> dict[str, Any]:
    async with _LOCK:
        dashboard = await _ensure_dashboard_loaded()
        return json.loads(json.dumps(dashboard))



async def record_tier_counts(site_id: str, premium: int, free: int) -> None:
    """Increment the premium / free alive counters for *site_id*."""
    if premium == 0 and free == 0:
        return
    async with _LOCK:
        dashboard = await _ensure_dashboard_loaded()
        site = dashboard["sites"].setdefault(
            str(site_id),
            {"total": 0, "alive": 0, "dead": 0, "plans": {}, "premium": 0, "free": 0},
        )
        site["premium"] = int(site.get("premium", 0) or 0) + premium
        site["free"]    = int(site.get("free",    0) or 0) + free
        await asyncio.to_thread(_save_dashboard_sync, dashboard)
