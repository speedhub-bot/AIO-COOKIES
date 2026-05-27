"""Format ScanOutcome objects into Telegram-friendly HTML messages."""

from __future__ import annotations

import html
import json
from typing import Any

from . import config
from .scanner import ScanOutcome
from .constants import BOT_CREDIT, PLAN_EMOJI, PLAN_ORDER, _ordered_plans

# Per-message length budget. Telegram's hard cap is 4096; we stop a
# little early so we can safely append truncation notes.
MAX_MESSAGE_LEN = 3800
MAX_VALUE_LEN = 400


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=False)


def _stringify(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        try:
            return json.dumps(value, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            return repr(value)
    return str(value)


def _truncate(text: str, limit: int = MAX_VALUE_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


# ── Plan detection helper ─────────────────────────────────────────────────────

def _detect_plan_label(site_id: str, info: dict[str, Any]) -> str | None:
    """Pull the best human-readable plan label from an alive account's info."""
    for key in ("plan_name", "plan", "subscription_tier", "payment_tier",
                "membership_status", "tier", "subscription_status"):
        raw = info.get(key)
        if raw in (None, "", "N/A", "n/a", "none", "null", "unknown"):
            continue
        text = str(raw).strip()
        low = text.lower()
        if "team/enterprise" in low:
            return "Team/Enterprise"
        if "enterprise" in low:
            return "Enterprise"
        if "team" in low:
            return "Team"
        if "max" in low or "ultra" in low:
            return "Max"
        if "premium+" in low or "premium plus" in low or "premiumplus" in low:
            return "Premium+"
        if "plus" in low:
            return "Plus"
        if "prime" in low:
            return "Prime"
        if "premium" in low:
            return "Premium"
        if "education" in low or low == "edu":
            return "Education"
        if "pro" in low or "paid" in low or low == "active":
            return "Pro"
        if "trial" in low:
            return "Trial"
        if "free" in low or low == "basic":
            return "Free"
        return text[:60]
    # fall back to boolean flags
    if info.get("is_pro") is True or info.get("is_premium") is True:
        return "Pro"
    if info.get("is_pro") is False or info.get("is_premium") is False:
        return "Free"
    return None


def _plan_banner(site_id: str, info: dict[str, Any]) -> str:
    """Return a one-line plan banner for ALIVE accounts, e.g.  💎 Max Plan"""
    plan = _detect_plan_label(site_id, info)
    if not plan:
        return ""
    emoji = PLAN_EMOJI.get(plan, "🔹")
    return f"{emoji} <b>{_esc(plan)} Plan</b>"


# ── Single outcome ─────────────────────────────────────────────────────────────

def format_outcome(outcome: ScanOutcome) -> str:
    """Render a single ScanOutcome as an HTML message body."""
    emoji  = config.site_emoji(outcome.site)
    alive  = outcome.alive

    if alive:
        status = "✅ <b>ALIVE</b>"
    elif outcome.is_dead:
        # Authoritative dead-cookie verdict from the adapter (401,
        # expired JWT, login redirect…). Show DEAD even if the adapter
        # also attached an ``error`` string for diagnostics.
        status = "❌ <b>DEAD</b>"
    elif outcome.error:
        status = "⚠️ <b>ERROR</b>"
    else:
        status = "❌ <b>DEAD</b>"

    lines: list[str] = [
        f"{emoji} <b>{_esc(outcome.site)}</b>  ·  {status}",
        f"<i>{_esc(outcome.filename)}</i>  ·  <code>{outcome.elapsed_s:.1f}s</code>",
    ]

    # Plan banner for alive hits
    if alive and outcome.info:
        banner = _plan_banner(outcome.site, outcome.info)
        if banner:
            lines.append("")
            lines.append(banner)

    # Error note
    if outcome.error:
        lines.append("")
        lines.append(f"⚠️ {_esc(_truncate(outcome.error, 800))}")

    # Account info table
    if outcome.info:
        lines.append("")
        lines.append("<pre>")
        priority = (
            "email", "name", "username",
            "plan", "plan_name", "subscription_tier", "payment_tier",
            "is_pro", "is_premium",
            "credits", "credits_total", "credits_used",
            "downloads_left", "downloads_quota",
            "id", "user_id",
            "subscription_status", "membership_status",
            "renewal", "renewal_date", "renewal_timestamp",
            "expires_at", "next_payment_date",
            "billing_period_start", "billing_period_end",
            "session_expires", "session_issued",
            "country", "token_expires",
            "usage_5h", "usage_7d",
            "organization", "billing_type", "rate_limit_tier",
        )
        seen: set[str] = set()
        ordered: list[tuple[str, Any]] = []
        for key in priority:
            if key in outcome.info:
                ordered.append((key, outcome.info[key]))
                seen.add(key)
        for key in sorted(outcome.info.keys()):
            if key not in seen:
                ordered.append((key, outcome.info[key]))

        for key, value in ordered:
            text_val = _truncate(_stringify(value))
            lines.append(f"{_esc(key)}: {_esc(text_val)}")
        lines.append("</pre>")

    # Refreshed cookies
    if outcome.refreshed_cookies:
        lines.append("")
        lines.append("🔄 <b>Refreshed cookies</b> (update your jar):")
        for name, value in outcome.refreshed_cookies.items():
            lines.append(f"<code>{_esc(name)}</code>")
            lines.append(f"<pre>{_esc(value)}</pre>")

    body = "\n".join(lines)
    body_budget = MAX_MESSAGE_LEN - len(BOT_CREDIT) - 2
    if len(body) > body_budget:
        body = body[: body_budget - 40] + "\n… (truncated)"
    return body + "\n\n" + BOT_CREDIT


# ── Scan summary ───────────────────────────────────────────────────────────────

def format_summary(outcomes: list[ScanOutcome], site_id: str = "") -> str:
    """Rich summary card shown after a multi-file / zip scan."""
    total  = len(outcomes)
    alive  = sum(1 for o in outcomes if o.alive)
    # is_dead wins over error for bucketing: an adapter that has confirmed
    # the cookie is dead may still attach an error message for debugging.
    dead   = sum(1 for o in outcomes if (not o.alive) and o.is_dead)
    errd   = sum(
        1 for o in outcomes
        if (not o.alive) and (not o.is_dead) and o.error
    )
    # Anything left over (alive=False, no is_dead, no error) is also dead
    # — old adapters that haven't been updated still land here cleanly.
    dead  += total - alive - dead - errd
    rate   = f"{alive / total * 100:.1f}%" if total > 0 else "—"

    emoji  = config.site_emoji(site_id) if site_id else "🍪"
    label  = config.site_label(site_id) if site_id else "All sites"

    # Build plan breakdown
    plan_counts: dict[str, int] = {}
    for o in outcomes:
        if not o.alive:
            continue
        plan = _detect_plan_label(o.site, o.info or {})
        if plan:
            plan_counts[plan] = plan_counts.get(plan, 0) + 1

    lines: list[str] = [
        f"{emoji} <b>{_esc(label)}  —  Scan Complete</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  🧮 Scanned : <b>{total}</b> file(s)",
        f"  ✅ Alive   : <b>{alive}</b>  ({rate})",
        f"  ❌ Dead    : <b>{dead}</b>",
    ]
    if errd > 0:
        lines.append(f"  ⚠️ Errored : <b>{errd}</b>")

    if plan_counts:
        ordered = _ordered_plans(site_id, plan_counts) if site_id else sorted(plan_counts.items(), key=lambda x: -x[1])
        lines.append("")
        lines.append("  <b>Plan breakdown:</b>")
        for plan, count in ordered:
            pe = PLAN_EMOJI.get(plan, "🔹")
            lines.append(f"    {pe} {_esc(plan)} : <b>{count}</b>")

    # List alive accounts (up to 10) with email + plan
    alive_outcomes = [o for o in outcomes if o.alive]
    if alive_outcomes:
        lines.append("")
        lines.append("  <b>Alive accounts:</b>")
        shown = alive_outcomes[:10]
        for i, o in enumerate(shown, 1):
            info = o.info or {}
            email = (info.get("email") or info.get("username") or info.get("user_id") or "—")
            plan  = _detect_plan_label(o.site, info) or "?"
            pe    = PLAN_EMOJI.get(plan, "🔹")
            lines.append(
                f"    {i}. <code>{_esc(_truncate(str(email), 40))}</code>  {pe} {_esc(plan)}"
            )
        if len(alive_outcomes) > 10:
            lines.append(f"    … and <b>{len(alive_outcomes) - 10}</b> more")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        BOT_CREDIT,
    ])

    body = "\n".join(lines)
    if len(body) > MAX_MESSAGE_LEN:
        body = body[: MAX_MESSAGE_LEN - 40] + "\n… (truncated)\n\n" + BOT_CREDIT
    return body


# ── Hit notification ───────────────────────────────────────────────────────────

def format_hit(outcome: ScanOutcome) -> str:
    """Caption for the ALIVE cookie file sent as a hit notification."""
    emoji = config.site_emoji(outcome.site)
    info  = outcome.info or {}

    email   = (info.get("email") or info.get("username") or info.get("user_id") or "n/a")
    plan    = _detect_plan_label(outcome.site, info) or "n/a"
    pe      = PLAN_EMOJI.get(plan, "🔹")
    renewal = (
        info.get("renewal") or info.get("renewal_date") or
        info.get("expires_at") or info.get("next_payment_date") or
        info.get("session_expires") or "n/a"
    )

    lines = [
        f"🚨 <b>HIT</b>  ·  {emoji} <b>{_esc(outcome.site)}</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  📧 Email   : <code>{_esc(_truncate(str(email), 200))}</code>",
        f"  {pe} Plan    : <code>{_esc(_truncate(str(plan), 200))}</code>",
    ]

    # Credits / quota — surfaced for any site that exposes them (Freepik
    # primarily, but the field set is generic so future sites pick this
    # up for free).
    credits_line = _credits_line(info)
    if credits_line:
        lines.append(credits_line)

    lines.extend([
        f"  📅 Renewal : <code>{_esc(_truncate(str(renewal), 200))}</code>",
        f"  📎 File    : <i>{_esc(outcome.filename)}</i>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        BOT_CREDIT,
    ])
    return "\n".join(lines)


def _credits_line(info: dict[str, Any]) -> str | None:
    """Build a HIT-card credits line, e.g.  💰 Credits : 142 / 200"""
    credits = info.get("credits")
    if credits is None:
        credits = info.get("downloads_left")
    if credits is None:
        return None
    total = info.get("credits_total") or info.get("downloads_quota")
    if total is not None:
        body = f"{credits} / {total}"
    else:
        body = str(credits)
    return f"  💰 Credits : <code>{_esc(_truncate(body, 200))}</code>"


# ── Final delivery summary (sent at very end) ──────────────────────────────────

def format_delivery_summary(
    site_id: str,
    outcomes: list[ScanOutcome],
    premium_sent: bool,
    free_available: int,
) -> str:
    """Full recap card sent at the end of a scan batch."""
    total  = len(outcomes)
    alive  = sum(1 for o in outcomes if o.alive)
    dead   = sum(1 for o in outcomes if (not o.alive) and o.is_dead)
    errd   = sum(
        1 for o in outcomes
        if (not o.alive) and (not o.is_dead) and o.error
    )
    dead  += total - alive - dead - errd
    emoji  = config.site_emoji(site_id)
    label  = config.site_label(site_id)

    # Collect plan counts
    plan_counts: dict[str, int] = {}
    for o in outcomes:
        if not o.alive:
            continue
        plan = _detect_plan_label(o.site, o.info or {})
        if plan:
            plan_counts[plan] = plan_counts.get(plan, 0) + 1

    lines: list[str] = [
        f"{emoji} <b>{_esc(label)}  —  Results Summary</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"  🧮 Checked  : <b>{total}</b>",
        f"  ✅ Alive    : <b>{alive}</b>",
        f"  ❌ Dead     : <b>{dead}</b>",
    ]
    if errd > 0:
        lines.append(f"  ⚠️ Errored  : <b>{errd}</b>")

    if plan_counts:
        ordered = _ordered_plans(site_id, plan_counts)
        lines.append("")
        lines.append("  <b>What you got:</b>")
        for plan, count in ordered:
            pe = PLAN_EMOJI.get(plan, "🔹")
            lines.append(f"    {pe} {_esc(plan)} : <b>{count}</b>")

    lines.append("")
    if premium_sent:
        lines.append("  📦 Premium cookie <code>.zip</code> sent above ☝️")
    if free_available > 0:
        lines.append(f"  🆓 {free_available} free account cookie(s) available — reply <b>yes</b> to get them")

    lines.extend([
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        BOT_CREDIT,
    ])
    return "\n".join(lines)
