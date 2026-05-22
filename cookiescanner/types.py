"""Shared result types (kept here to avoid circular imports)."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class ScanResult:
    site: str
    alive: bool
    # Authoritative "this cookie is definitely dead" flag, separate from
    # ``alive=False``. The legacy code lumped every failure (timeouts,
    # Cloudflare blocks, dead 401s) into the same bucket via the ``error``
    # field, which made the bot's dashboard always show "alive or errored"
    # and never "dead". Adapters now set ``is_dead=True`` for unambiguous
    # cookie-revoked / cookie-expired / cookie-missing cases (401, login
    # redirect, JWT expired, no auth cookie present) and leave it False
    # for transient/environmental failures (timeouts, 5xx, CF challenges).
    # When ``is_dead`` is True the adapter MAY also set ``error`` for
    # diagnostics, but the dashboard only uses ``is_dead`` to bucket.
    is_dead: bool = False
    info: dict[str, Any] = field(default_factory=dict)
    error: str | None = None
    endpoints_tried: list[dict[str, Any]] = field(default_factory=list)
    # Cookies the adapter *rotated* during the scan (e.g. Roblox's
    # ``.ROBLOSECURITY`` after a successful auth-ticket refresh).
    # Maps cookie name to its fresh value. Callers can write these back
    # to their on-disk jar so the next scan uses the rotated token.
    refreshed_cookies: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
