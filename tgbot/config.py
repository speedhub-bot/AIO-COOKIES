"""Bot configuration sourced from environment variables.

Defaults mirror the patterns used in the speedhub-bot/Log repo so a new
deployer only has to provide a ``BOT_TOKEN`` to get a working bot — the
``API_ID`` / ``API_HASH`` fall back to Telegram Desktop's public values
which work for everyone.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _str(key: str, default: str) -> str:
    raw = os.getenv(key)
    return raw if raw is not None and raw != "" else default


def _bool(key: str, default: bool) -> bool:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ── Telegram credentials ─────────────────────────────────────
BOT_TOKEN: str = _str("BOT_TOKEN", "")

# Telegram Desktop's well-known public api_id / api_hash. Anyone can
# use these — they're shipped with the Desktop client. Override via env
# if you have your own from https://my.telegram.org.
API_ID: int = _int("API_ID", 2040)
API_HASH: str = _str("API_HASH", "b18441a1ff607e10a989891a5462e627")

# Optional. If set, critical errors get forwarded here. Default mirrors
# the ADMIN value used in the sibling speedhub-bot/Log deployment so the
# bot owner gets the same notifications across both bots without extra
# config.
ADMIN_ID: int = _int("ADMIN_ID", 5944410248)


# ── Bot behaviour ────────────────────────────────────────────
# Maximum file size we'll accept from a user (Telegram bot API caps
# downloads at 20 MB through the HTTP API, so 20 MB is the practical
# ceiling unless you wire pyrofork in front).
MAX_FILE_BYTES: int = _int("MAX_FILE_BYTES", 20 * 1024 * 1024)

# Thread pool size used by the synchronous scanners. Bigger = faster
# zip processing but more outbound concurrency to each target.
MAX_THREADS: int = _int("MAX_THREADS", 10)

# Hard cap on how long a single scan job is allowed to run before we
# bail out. Keeps a runaway job from blocking the bot forever.
JOB_TIMEOUT_SECONDS: int = _int("JOB_TIMEOUT_SECONDS", 600)

# Forwarded to the underlying scanner libraries via ``--proxy``.
DEFAULT_PROXY: str = _str("DEFAULT_PROXY", "")


# ── Paths ────────────────────────────────────────────────────
DATA_DIR: Path = Path(_str("DATA_DIR", "bot_data"))
LOG_FILE: str = _str("LOG_FILE", "bot.log")
TEMP_DIR: Path = Path(_str("TEMP_DIR", "/tmp/aiocookies-bot"))


# ── Channel gate ─────────────────────────────────────────────
# Public username OR numeric id of the channel users must join.
# e.g.  REQUIRED_CHANNEL=@akazacheck  or  REQUIRED_CHANNEL=-1001234567890
# ⚠️  The bot MUST be added to this channel as administrator for the
#    membership check to work — otherwise every new user will see the
#    "Access Denied" join prompt and never get past it.
REQUIRED_CHANNEL: str = _str("REQUIRED_CHANNEL", "@akazacheck")
# Invite link shown to users who haven't joined yet.
REQUIRED_CHANNEL_INVITE: str = _str(
    "REQUIRED_CHANNEL_INVITE", "https://t.me/akazacheck"
)
# Display name shown on the join button
CHANNEL_DISPLAY_NAME: str = _str("CHANNEL_DISPLAY_NAME", "@akazacheck 👑")

# Per-task sleep (in seconds) inserted *after* each cookie check
# completes inside a parallel zip scan. The scan is fan-out with up to
# ``MAX_THREADS`` concurrent workers, so the previous 0.6 s default
# bottlenecked CPM to ~100/min regardless of thread count. Set to 0
# by default; bump only if you start tripping per-IP rate limits.
SCAN_DELAY_SECONDS: float = _float("SCAN_DELAY_SECONDS", 0.0)

# ── Proxy ─────────────────────────────────────────────────────
# Flat file where the active proxy list is persisted.
# Admin can overwrite this at runtime via the bot panel.
PROXY_FILE: Path = Path(_str("PROXY_FILE", "bot_data/proxies.txt"))

# ── Roles ─────────────────────────────────────────────────────
ROLE_FREE    = "free"
ROLE_PREMIUM = "premium"
ROLE_ADMIN   = "admin"

# ── Hit notification defaults ────────────────────────────────
# Default ON/OFF state for hit notifications when a user first opens
# the bot. Per-user override lives in ``bot_data/settings.json``.
HIT_NOTIFICATIONS_DEFAULT: bool = _bool("HIT_NOTIFICATIONS_DEFAULT", False)


# ── Supported sites ──────────────────────────────────────────
# Display order in the /start keyboard. The key is what we pass to the
# scanner dispatcher; the label and emoji are what the user sees.
SUPPORTED_SITES: list[dict[str, str]] = [
    {"id": "claude.ai",       "label": "Claude.ai",       "emoji": "\U0001f7e3"},  # 🟣
    {"id": "chatgpt.com",     "label": "ChatGPT",         "emoji": "\U0001f7e2"},  # 🟢
    {"id": "cursor.com",      "label": "Cursor",          "emoji": "\U0001f535"},  # 🔵
    {"id": "devin.ai",        "label": "Devin",           "emoji": "\U0001f7e1"},  # 🟡
    {"id": "crunchyroll.com", "label": "Crunchyroll",     "emoji": "\U0001f7e0"},  # 🟠
    {"id": "netflix.com",     "label": "Netflix",         "emoji": "\U0001f534"},  # 🔴
    {"id": "primevideo.com",  "label": "Prime Video",     "emoji": "\U0001f4fa"},  # 📺
    {"id": "spotify.com",     "label": "Spotify",         "emoji": "\U0001f3b5"},  # 🎵
    {"id": "roblox.com",      "label": "Roblox",          "emoji": "\U0001f3ae"},  # 🎮
    {"id": "shopify.com",     "label": "Shopify",         "emoji": "\U0001f6d2"},  # 🛒
    {"id": "facebook.com",    "label": "Facebook",        "emoji": "\U0001f465"},  # 👥
    {"id": "blackbox.ai",     "label": "Blackbox",        "emoji": "\u2b1b\ufe0f"},  # ⬛️
    {"id": "manus.im",        "label": "Manus",           "emoji": "\U0001f9e0"},  # 🧠
    {"id": "perplexity.ai",   "label": "Perplexity",      "emoji": "\U0001f50d"},  # 🔍
    {"id": "freepik.com",     "label": "Freepik",         "emoji": "\U0001f3a8"},  # 🎨
]

SUPPORTED_SITE_IDS: set[str] = {s["id"] for s in SUPPORTED_SITES}


def site_label(site_id: str) -> str:
    for s in SUPPORTED_SITES:
        if s["id"] == site_id:
            return f"{s['emoji']} {s['label']}"
    return site_id


def site_emoji(site_id: str) -> str:
    for s in SUPPORTED_SITES:
        if s["id"] == site_id:
            return s["emoji"]
    return "\u26aa"  # ⚪
