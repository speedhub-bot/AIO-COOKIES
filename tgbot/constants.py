"""Shared constants used by both dashboard.py and formatting.py.

Kept in a separate module to avoid the circular import that arises when
dashboard imports BOT_CREDIT from formatting and formatting imports
PLAN_EMOJI/PLAN_ORDER/_ordered_plans from dashboard.
"""

from __future__ import annotations

# ── Bot credit line ───────────────────────────────────────────────────────────

BOT_CREDIT = '🤖 Bot by <a href="https://t.me/akaza_isnt">@akaza_isnt</a>'


# ── Plan tier ordering per site ───────────────────────────────────────────────

PLAN_ORDER: dict[str, tuple[str, ...]] = {
    "claude.ai":       ("Max", "Team/Enterprise", "Team", "Enterprise", "Pro", "Free", "Trial", "Unknown"),
    "chatgpt.com":     ("Team", "Pro", "Plus", "Paid", "Free", "Unknown"),
    "cursor.com":      ("Team", "Pro", "Premium", "Free", "Unknown"),
    "devin.ai":        ("Team", "Pro", "Free", "Unknown"),
    "crunchyroll.com": ("Ultimate Fan", "Mega Fan", "Fan", "Premium", "Free", "Unknown"),
    "netflix.com":     ("Premium", "Standard", "Basic", "Free", "Unknown"),
    "primevideo.com":  ("Prime", "Premium", "Free", "Unknown"),
    "spotify.com":     ("Premium", "Family", "Duo", "Student", "Free", "Unknown"),
    "roblox.com":      ("Premium", "Free", "Unknown"),
    "shopify.com":     ("Plus", "Advanced", "Shopify", "Basic", "Free", "Unknown"),
    "facebook.com":    ("Free", "Unknown"),
    "blackbox.ai":     ("Team", "Premium", "Trial", "Free", "Unknown"),
    "manus.im":        ("Max", "Team", "Pro", "Plus", "Premium", "Free", "Unknown"),
    "perplexity.ai":   ("Enterprise", "Team", "Max", "Pro", "Free", "Unknown"),
}


# ── Emoji badge per plan label ────────────────────────────────────────────────

PLAN_EMOJI: dict[str, str] = {
    "Max":            "💎",
    "Ultra":          "💎",
    "Enterprise":     "🏢",
    "Team/Enterprise":"🏢",
    "Team":           "👥",
    "Pro":            "⭐",
    "Plus":           "✨",
    "Paid":           "💳",
    "Premium":        "🌟",
    "Prime":          "🎬",
    "Family":         "👨‍👩‍👧",
    "Duo":            "👫",
    "Student":        "🎓",
    "Ultimate Fan":   "🔥",
    "Mega Fan":       "🎌",
    "Fan":            "🎏",
    "Advanced":       "🚀",
    "Basic":          "📦",
    "Shopify":        "🛍️",
    "Trial":          "⏳",
    "Free":           "🆓",
    "Unknown":        "❓",
}


# ── Helper ────────────────────────────────────────────────────────────────────

def _ordered_plans(site_id: str, plans: dict[str, int]) -> list[tuple[str, int]]:
    """Return (plan_label, count) pairs sorted by site-specific tier order."""
    order = PLAN_ORDER.get(
        site_id,
        ("Max", "Team", "Pro", "Plus", "Premium", "Free", "Unknown"),
    )
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for label in order:
        count = int(plans.get(label, 0) or 0)
        if count > 0:
            out.append((label, count))
            seen.add(label)
    # append any extra labels not in the predefined order
    for label in sorted(plans):
        if label in seen:
            continue
        count = int(plans.get(label, 0) or 0)
        if count > 0:
            out.append((label, count))
    return out
