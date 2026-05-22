"""Telegram HTML dashboard for live scan stats."""

from __future__ import annotations

import html
from typing import Any

from . import config
from .constants import BOT_CREDIT, PLAN_EMOJI, PLAN_ORDER, _ordered_plans

# Site-level emoji header rows (shown even when 0 checks)
_SITE_INTRO: dict[str, str] = {
    "claude.ai":      "AI assistant (Free / Pro / Max / Team)",
    "chatgpt.com":    "AI assistant (Free / Plus / Pro / Team)",
    "cursor.com":     "AI code editor (Free / Pro / Team)",
    "devin.ai":       "AI developer (Free / Pro / Team)",
    "crunchyroll.com":"Anime streaming (Fan / Mega Fan / Ultimate Fan)",
    "netflix.com":    "Video streaming (Basic / Standard / Premium)",
    "primevideo.com": "Video streaming (Free / Prime / Premium)",
    "spotify.com":    "Music streaming (Free / Premium / Family / Duo / Student)",
    "roblox.com":     "Gaming platform (Free / Premium)",
    "shopify.com":    "E-commerce (Basic / Shopify / Advanced / Plus)",
    "facebook.com":   "Social network",
    "blackbox.ai":    "AI assistant (Free / Premium / Team)",
    "manus.im":       "AI agent (Free / Plus / Pro / Max / Team)",
    "perplexity.ai":  "AI search (Free / Pro / Max / Team / Enterprise)",
}


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _plan_badge(plan: str, count: int) -> str:
    emoji = PLAN_EMOJI.get(plan, "🔹")
    return f"{emoji} {_esc(plan)} <b>{count}</b>"


def _bar(alive: int, total: int, width: int = 8) -> str:
    """Return a tiny Unicode progress bar."""
    if total == 0:
        return "░" * width
    filled = round(alive / total * width)
    return "█" * filled + "░" * (width - filled)


def format_start_dashboard(stats: dict[str, Any]) -> str:
    """Rich main-menu dashboard with live stats for every supported site."""
    sites = stats.get("sites") if isinstance(stats.get("sites"), dict) else {}
    total = int(stats.get("total", 0) or 0)
    alive = int(stats.get("alive", 0) or 0)
    dead  = int(stats.get("dead",  0) or 0)
    rate  = f"{alive / total * 100:.1f}%" if total > 0 else "—"
    last  = stats.get("last_updated") or "never"

    lines: list[str] = [
        "🍪 <b>AIO Cookies Bot</b>  ·  Live Dashboard",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        (
            f"📊 <b>All-time totals</b>\n"
            f"  🧮 Scanned : <b>{total}</b>\n"
            f"  ✅ Alive   : <b>{alive}</b>  ({rate})\n"
            f"  ❌ Dead    : <b>{dead}</b>\n"
            f"  🕒 Updated : <code>{_esc(last)}</code>"
        ),
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]

    # ── Per-site rows ──────────────────────────────────────
    for site in config.SUPPORTED_SITES:
        site_id   = site["id"]
        raw       = sites.get(site_id)
        ss        = raw if isinstance(raw, dict) else {}
        s_total   = int(ss.get("total", 0) or 0)
        s_alive   = int(ss.get("alive", 0) or 0)
        s_dead    = int(ss.get("dead",  0) or 0)
        intro     = _SITE_INTRO.get(site_id, "")
        label     = f"{site['emoji']} <b>{_esc(site['label'])}</b>"

        if s_total == 0:
            lines.append(
                f"{label}\n"
                f"  <i>{_esc(intro)}</i>\n"
                f"  <code>No checks yet</code>\n"
            )
            continue

        bar       = _bar(s_alive, s_total)
        s_rate    = f"{s_alive / s_total * 100:.1f}%"
        plan_data = _ordered_plans(site_id, dict(ss.get("plans") or {}))

        lines.append(
            f"{label}\n"
            f"  <i>{_esc(intro)}</i>\n"
            f"  [{bar}] ✅ <b>{s_alive}</b> alive · ❌ <b>{s_dead}</b> dead · {s_rate}"
        )

        if plan_data:
            badges = "  ".join(_plan_badge(p, c) for p, c in plan_data)
            lines.append(f"  {badges}")

        lines.append("")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "👇 Pick a service — then send your cookie file",
        "   <code>.json</code> · <code>.txt</code> · <code>.zip</code>",
        "",
        BOT_CREDIT,
    ])
    return "\n".join(lines)


def format_scan_dashboard(
    site_id: str,
    done: int,
    total_files: int,
    alive: int,
    dead: int,
    plan_counts: dict[str, int],
    current_file: str = "",
) -> str:
    """Live progress dashboard sent/edited while a zip is being checked."""
    emoji   = config.site_emoji(site_id)
    label   = config.site_label(site_id)
    pct     = f"{done / total_files * 100:.0f}%" if total_files > 0 else "0%"
    bar     = _bar(done, total_files, width=10)

    lines: list[str] = [
        f"{emoji} <b>Scanning {_esc(label)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  ⚙️  Progress : [{bar}] <b>{done}/{total_files}</b> ({pct})",
        f"  ✅ Alive    : <b>{alive}</b>",
        f"  ❌ Dead     : <b>{dead}</b>",
    ]

    if current_file:
        lines.append(f"  📄 Checking : <code>{_esc(current_file)}</code>")

    if plan_counts:
        ordered = _ordered_plans(site_id, plan_counts)
        if ordered:
            lines.append("")
            lines.append("  <b>Plans found so far:</b>")
            for plan, count in ordered:
                lines.append(f"    {_plan_badge(plan, count)}")

    lines.extend(["━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", "⏳ <i>Please wait…</i>"])
    return "\n".join(lines)
